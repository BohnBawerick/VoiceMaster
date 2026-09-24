"""Tests for Idea 1 — dead-air filler with debounce + the response-collision gate (Mode C).

A slow hermes tool call used to leave ~29s of silence. Now, if the backend hasn't returned within
the debounce window, Robot speaks a short filler; a fast turn skips it. The filler and the result
each go through the response_idle gate so the two response.create calls can't collide (OpenAI
allows one active response at a time).
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


def _fillers(ws):
    return [m for m in ws.sent if m.get("type") == "response.create" and m.get("response")]


def _result_creates(ws):
    return [m for m in ws.sent if m.get("type") == "response.create" and not m.get("response")]


def _outputs(ws):
    return [m for m in ws.sent if m.get("item", {}).get("type") == "function_call_output"]


@pytest.mark.asyncio
async def test_fast_tool_skips_filler(monkeypatch):
    async def fast_hermes(instruction, profile="default"):
        return "QUICK"
    monkeypatch.setattr(server, "call_hermes_agent", fast_hermes)

    ws = FakeWS()
    idle = asyncio.Event(); idle.set()
    await server._run_tool_call(ws, asyncio.Lock(), "c1", "quick",
                                response_idle=idle, filler_debounce=1.5)

    assert _fillers(ws) == []                              # no filler on a fast turn
    assert _outputs(ws) and _outputs(ws)[0]["item"]["output"] == "QUICK"
    assert len(_result_creates(ws)) == 1                   # exactly one result trigger


@pytest.mark.asyncio
async def test_slow_tool_speaks_filler_then_result(monkeypatch):
    gate = asyncio.Event()

    async def slow_hermes(instruction, profile="default"):
        await gate.wait()
        return "RESULT"
    monkeypatch.setattr(server, "call_hermes_agent", slow_hermes)

    ws = FakeWS()
    idle = asyncio.Event(); idle.set()
    task = asyncio.create_task(server._run_tool_call(
        ws, asyncio.Lock(), "c1", "slow", response_idle=idle,
        filler_debounce=0.02, filler_text="One sec."))

    for _ in range(500):                                   # wait for the filler (debounce ~20ms)
        if _fillers(ws):
            break
        await asyncio.sleep(0.005)
    assert len(_fillers(ws)) == 1
    assert "One sec." in _fillers(ws)[0]["response"]["instructions"]
    assert _outputs(ws) == []                              # result not sent yet — hermes still running

    idle.set()          # simulate the receive loop: filler's response.done → idle
    gate.set()          # let hermes finish
    await task

    assert _outputs(ws) and _outputs(ws)[0]["item"]["output"] == "RESULT"
    assert len(_result_creates(ws)) == 1


@pytest.mark.asyncio
async def test_result_blocked_until_gate_open(monkeypatch):
    """The result's response.create must NOT be sent while a response is active (idle cleared)."""
    async def fast_hermes(instruction, profile="default"):
        return "R"
    monkeypatch.setattr(server, "call_hermes_agent", fast_hermes)

    ws = FakeWS()
    idle = asyncio.Event()                                 # NOT set → a response is 'active'
    task = asyncio.create_task(server._run_tool_call(
        ws, asyncio.Lock(), "c1", "q", response_idle=idle, filler_debounce=1.5))
    await asyncio.sleep(0.02)

    assert _result_creates(ws) == []                       # blocked on the gate
    idle.set()
    await task
    assert len(_result_creates(ws)) == 1                   # released
