"""Tests for the tool round-trip on the Realtime bridge.

The regression these guard against: a slow Hermes backend turn (up to
``hermes_timeout``, and an owner-approval wait even longer) used to be awaited
*inside* the single OpenAI-WS receive loop. That stalled the one consumer of the
socket, let the inbound frame queue fill until websockets applied backpressure and
stopped reading — which also stalled pong handling, so OpenAI's keepalive ping
timed out and dropped the call (observed live: ``sent 1011 keepalive ping timeout``
mid-tool). The fix dispatches the tool as a tracked background task so the loop
keeps draining audio + control frames throughout the backend turn.

No real OpenAI/PulseAudio — a fake WS feeds events and records sends.
"""
import asyncio
import json

import pytest

import hermes
import realtime_bridge
from approval import ApprovalStore
from config import load


class FakeWS:
    """Async-iterable of pre-baked event JSON strings; records everything sent back."""

    def __init__(self, messages):
        self._messages = messages
        self.sent = []

    def __aiter__(self):
        async def _gen():
            for m in self._messages:
                yield m
        return _gen()

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _bridge():
    return realtime_bridge.RealtimeBridge(
        load(), "system prompt", ApprovalStore(),
        token_ctx={"token": "tok", "caller": "Owner"})


def _tool_done_event(instruction):
    return json.dumps({
        "type": "response.function_call_arguments.done",
        "name": "hermes_agent",
        "call_id": "call-1",
        "arguments": json.dumps({"instruction": instruction}),
    })


def _audio_delta(delta):
    return json.dumps({"type": "response.output_audio.delta", "delta": delta})


@pytest.mark.asyncio
async def test_slow_tool_call_does_not_block_receive_loop(monkeypatch):
    """A tool call that hasn't returned yet must NOT stop the loop from processing the
    audio deltas that follow it — proving the WS keeps draining during a backend turn."""
    release = asyncio.Event()
    order = []

    async def slow_hermes(instruction, **kwargs):
        order.append("hermes_start")
        await release.wait()          # never fires during the receive loop
        order.append("hermes_end")
        return "You have two new emails."

    monkeypatch.setattr(hermes, "call_hermes_agent", slow_hermes)

    bridge = _bridge()
    played = []

    async def fake_play(delta):
        order.append(f"play:{delta}")
        played.append(delta)

    bridge._play = fake_play

    ws = FakeWS([
        _tool_done_event("check my recent emails"),
        _audio_delta("AAA"),
        _audio_delta("BBB"),
    ])

    await bridge._receive_from_openai(ws)

    # Decisive assertions: both audio deltas were relayed even though the tool call
    # is still in flight (release was never set, so hermes could not have finished).
    # Under the old blocking design the loop would have parked at the tool event
    # awaiting slow_hermes and never reached the deltas — _receive_from_openai would
    # hang forever and this test would time out.
    assert played == ["AAA", "BBB"]
    assert "hermes_end" not in order        # tool has NOT completed
    assert len(bridge._tool_tasks) == 1     # tool dispatched + tracked, still in flight

    # Let the tool finish: it returns the function result + a response.create trigger.
    release.set()
    await asyncio.gather(*list(bridge._tool_tasks))

    outputs = [m for m in ws.sent if m.get("item", {}).get("type") == "function_call_output"]
    assert outputs and outputs[0]["item"]["output"] == "You have two new emails."
    assert outputs[0]["item"]["call_id"] == "call-1"
    assert any(m.get("type") == "response.create" for m in ws.sent)
    assert bridge._tool_tasks == set()      # done-callback cleaned it up


@pytest.mark.asyncio
async def test_teardown_cancels_in_flight_tool_call(monkeypatch):
    """A hangup mid-tool (stop()/teardown) cancels the outstanding backend round-trip
    instead of leaking it — a hung backend can't keep the session half-alive."""
    started = asyncio.Event()

    async def hanging_hermes(instruction, **kwargs):
        started.set()
        await asyncio.Event().wait()   # never returns
        return "unreachable"

    monkeypatch.setattr(hermes, "call_hermes_agent", hanging_hermes)

    bridge = _bridge()

    ws = FakeWS([_tool_done_event("do something slow")])
    await bridge._receive_from_openai(ws)

    assert len(bridge._tool_tasks) == 1
    tool_task = next(iter(bridge._tool_tasks))
    await started.wait()               # ensure the backend call is actually in flight

    await bridge.stop()                # simulates hangup / teardown

    assert tool_task.cancelled()
    assert bridge._tool_tasks == set()
