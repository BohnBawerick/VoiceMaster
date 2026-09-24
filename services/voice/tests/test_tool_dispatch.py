"""Tests for the A1 off-loop tool dispatch (Mode C).

Regression guarded: the Hermes backend round-trip used to be awaited INLINE on the OpenAI
receive loop (`result = await call_hermes_agent(...)`), which stalled the single WS consumer
during a slow backend turn — the inbound frame queue backed up, websockets applied backpressure,
pong handling stalled, and OpenAI dropped the call with a keepalive-ping timeout (the
`sent 1011 keepalive ping timeout` failure). The fix runs the round-trip OFF the loop as a tracked
task, serialized on a per-connection lock (mirrors the Mode V bridge).

No real OpenAI/Hermes — a fake WS records sends; call_hermes_agent is monkeypatched.
"""
import asyncio
import json

import pytest

import server


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


@pytest.mark.asyncio
async def test_run_tool_call_feeds_result_and_triggers_response(monkeypatch):
    async def fake_hermes(instruction, profile="default"):
        assert instruction == "check my email"
        return "You have two new emails."
    monkeypatch.setattr(server, "call_hermes_agent", fake_hermes)

    ws = FakeWS()
    await server._run_tool_call(ws, asyncio.Lock(), "call-1", "check my email")

    outputs = [m for m in ws.sent if m.get("item", {}).get("type") == "function_call_output"]
    assert outputs and outputs[0]["item"]["output"] == "You have two new emails."
    assert outputs[0]["item"]["call_id"] == "call-1"
    assert any(m.get("type") == "response.create" for m in ws.sent)


@pytest.mark.asyncio
async def test_dispatch_does_not_block_on_slow_backend(monkeypatch):
    """The decisive A1 property: dispatching a tool returns immediately; the slow backend runs
    in the tracked task, not on the caller (which stands in for the receive loop)."""
    release = asyncio.Event()

    async def slow_hermes(instruction, profile="default"):
        await release.wait()
        return "done"
    monkeypatch.setattr(server, "call_hermes_agent", slow_hermes)

    ws = FakeWS()
    tool_tasks: set[asyncio.Task] = set()
    task = asyncio.create_task(server._dispatch_tool_call(ws, asyncio.Lock(), "call-1", "slow one"))
    tool_tasks.add(task)
    task.add_done_callback(tool_tasks.discard)

    await asyncio.sleep(0)                 # let the task start
    assert not task.done()                 # backend still blocked → task in flight
    assert ws.sent == []                   # nothing sent yet (hermes hasn't returned)

    release.set()
    await asyncio.gather(*list(tool_tasks))
    assert any(m.get("type") == "response.create" for m in ws.sent)
    assert tool_tasks == set()             # done-callback cleaned it up


@pytest.mark.asyncio
async def test_dispatch_cancel_on_teardown(monkeypatch):
    """A hangup mid-tool cancels the outstanding round-trip instead of leaking it — the Mode C
    analogue of Mode V's _teardown tool-task cancel."""
    started = asyncio.Event()

    async def hanging_hermes(instruction, profile="default"):
        started.set()
        await asyncio.Event().wait()       # never returns
    monkeypatch.setattr(server, "call_hermes_agent", hanging_hermes)

    ws = FakeWS()
    tool_tasks: set[asyncio.Task] = set()
    task = asyncio.create_task(server._dispatch_tool_call(ws, asyncio.Lock(), "call-1", "hang"))
    tool_tasks.add(task)
    task.add_done_callback(tool_tasks.discard)

    await started.wait()                   # backend call is actually in flight
    for t in list(tool_tasks):             # the server.py finally block
        t.cancel()
    await asyncio.gather(*tool_tasks, return_exceptions=True)
    assert task.cancelled()
    assert tool_tasks == set()


@pytest.mark.asyncio
async def test_tool_lock_serializes_two_calls(monkeypatch):
    """Two concurrent dispatches must not interleave — each round-trip finishes before the next
    begins (OpenAI allows one active response at a time)."""
    order = []
    gate = asyncio.Event()

    async def hermes(instruction, profile="default"):
        order.append(f"start:{instruction}")
        if instruction == "first":
            await gate.wait()              # hold the lock until released
        order.append(f"end:{instruction}")
        return instruction
    monkeypatch.setattr(server, "call_hermes_agent", hermes)

    ws = FakeWS()
    lock = asyncio.Lock()
    t1 = asyncio.create_task(server._dispatch_tool_call(ws, lock, "c1", "first"))
    for _ in range(100):                   # ensure t1 grabs the lock + starts first
        if "start:first" in order:
            break
        await asyncio.sleep(0)
    t2 = asyncio.create_task(server._dispatch_tool_call(ws, lock, "c2", "second"))
    await asyncio.sleep(0)

    assert "start:first" in order and "start:second" not in order   # t2 blocked on the lock
    gate.set()
    await asyncio.gather(t1, t2)
    assert order == ["start:first", "end:first", "start:second", "end:second"]
