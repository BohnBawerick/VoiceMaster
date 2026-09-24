"""Tests for the eventlog wiring INTO the Realtime bridge (Idea 2).

test_eventlog.py already covers CallRecorder's accounting in isolation; these prove the bridge
feeds it the right events at the right points: speech_stopped starts the TTFB clock, the first
audio delta closes it, response.done emits a per-turn record with normalized token usage, and
teardown emits the per-call summary.

No real OpenAI/PulseAudio — a fake WS feeds events; append_event is monkeypatched so the shared
/app/events volume is never touched.
"""
import asyncio
import base64
import json

import pytest

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


# 480 PCM16 bytes → one audio delta; the exact size doesn't matter, only that it's a real b64 blob.
_A_DELTA = base64.b64encode(b"\x00" * 480).decode()


@pytest.mark.asyncio
async def test_turn_record_captures_ttfb_and_tokens(monkeypatch):
    """speech_stopped → first audio delta → response.done must emit exactly one turn record whose
    TTFB is a real (>=0) number and whose usage is the flattened four-field token block."""
    events = []
    monkeypatch.setattr(realtime_bridge.eventlog, "append_event",
                        lambda obj, path=None: events.append(obj))

    bridge = _bridge()

    async def fake_play(delta):
        pass

    bridge._play = fake_play

    ws = FakeWS([
        json.dumps({"type": "input_audio_buffer.speech_stopped"}),
        json.dumps({"type": "response.output_audio.delta", "item_id": "i1", "delta": _A_DELTA}),
        json.dumps({"type": "response.done", "response": {"usage": {
            "input_tokens": 50, "output_tokens": 20,
            "input_token_details": {"audio_tokens": 40},
            "output_token_details": {"audio_tokens": 15},
        }}}),
    ])

    await bridge._receive_from_openai(ws)

    turns = [e for e in events if e["type"] == "turn"]
    assert len(turns) == 1
    assert isinstance(turns[0]["ttfb_ms"], (int, float)) and turns[0]["ttfb_ms"] >= 0
    assert turns[0]["usage"] == {"input_tokens": 50, "output_tokens": 20,
                                 "input_audio_tokens": 40, "output_audio_tokens": 15}


@pytest.mark.asyncio
async def test_finish_emits_call_record(monkeypatch):
    """The teardown-time summary emits one call record carrying this surface's mode/direction."""
    events = []
    monkeypatch.setattr(realtime_bridge.eventlog, "append_event",
                        lambda obj, path=None: events.append(obj))

    bridge = _bridge()
    bridge._recorder.finish(outcome="ok")

    calls = [e for e in events if e["type"] == "call"]
    assert len(calls) == 1
    assert calls[0]["mode"] == "talk"
    assert calls[0]["direction"] == "inbound"
