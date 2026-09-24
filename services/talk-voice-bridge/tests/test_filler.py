"""Tests for Idea 1 — dead-air filler with debounce + the response-collision gate (Mode V)."""
import asyncio
import dataclasses
import json

import pytest

import hermes
import realtime_bridge
from approval import ApprovalStore
from config import load


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _bridge(debounce_ms=1500):
    cfg = dataclasses.replace(load(), filler_debounce_ms=debounce_ms, filler_text="One sec.")
    return realtime_bridge.RealtimeBridge(cfg, "system prompt", ApprovalStore(),
                                          token_ctx={"token": "tok", "caller": "Owner"})


def _tool_ev(instruction="do it"):
    return {"type": "response.function_call_arguments.done", "name": "hermes_agent",
            "call_id": "call-1", "arguments": json.dumps({"instruction": instruction})}


def _fillers(ws):
    return [m for m in ws.sent if m.get("type") == "response.create" and m.get("response")]


def _result_creates(ws):
    return [m for m in ws.sent if m.get("type") == "response.create" and not m.get("response")]


def _outputs(ws):
    return [m for m in ws.sent if m.get("item", {}).get("type") == "function_call_output"]


@pytest.mark.asyncio
async def test_fast_tool_skips_filler(monkeypatch):
    async def fast_hermes(instruction, **kw):
        return "QUICK"
    monkeypatch.setattr(hermes, "call_hermes_agent", fast_hermes)

    bridge = _bridge(debounce_ms=1500)
    ws = FakeWS()
    await bridge._handle_tool(ws, _tool_ev())

    assert _fillers(ws) == []
    assert _outputs(ws) and _outputs(ws)[0]["item"]["output"] == "QUICK"
    assert len(_result_creates(ws)) == 1


@pytest.mark.asyncio
async def test_slow_tool_speaks_filler_then_result(monkeypatch):
    gate = asyncio.Event()

    async def slow_hermes(instruction, **kw):
        await gate.wait()
        return "RESULT"
    monkeypatch.setattr(hermes, "call_hermes_agent", slow_hermes)

    bridge = _bridge(debounce_ms=20)
    ws = FakeWS()
    task = asyncio.create_task(bridge._handle_tool(ws, _tool_ev()))

    for _ in range(500):
        if _fillers(ws):
            break
        await asyncio.sleep(0.005)
    assert len(_fillers(ws)) == 1
    assert "One sec." in _fillers(ws)[0]["response"]["instructions"]

    bridge._response_idle.set()   # simulate the receive loop: filler's response.done → idle
    gate.set()
    await task

    assert _outputs(ws) and _outputs(ws)[0]["item"]["output"] == "RESULT"
    assert len(_result_creates(ws)) == 1
