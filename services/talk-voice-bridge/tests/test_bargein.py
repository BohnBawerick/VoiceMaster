"""Tests for A2 barge-in / interruption handling on the Realtime bridge.

When the caller talks over Robot, the OpenAI VAD emits ``input_audio_buffer.speech_started``.
The bridge must then (a) drop the audio already buffered in pacat/Pulse so Robot goes quiet
promptly, and (b) ``conversation.item.truncate`` the currently-playing assistant item to the
number of ms actually played, so the model's context matches what the caller heard. A
``response.done`` clears the tracking so a later speech_started is a harmless no-op.

No real OpenAI/PulseAudio — a fake WS feeds events and records sends; _flush_playback and
_play are monkeypatched so no pacat subprocess is spawned.
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


# 480 PCM16 bytes → 480 / 48 = 10 ms of played audio.
_TEN_MS_DELTA = base64.b64encode(b"\x00" * 480).decode()


def _audio_delta(item_id, delta):
    return json.dumps({
        "type": "response.output_audio.delta",
        "item_id": item_id,
        "delta": delta,
    })


def _truncates(ws):
    return [m for m in ws.sent if m.get("type") == "conversation.item.truncate"]


@pytest.mark.asyncio
async def test_barge_in_flushes_and_truncates():
    """An audio delta then caller speech_started must flush buffered playback and truncate the
    playing item to exactly the ms played (10 ms from the 480-byte delta)."""
    bridge = _bridge()
    flush_calls = []

    async def fake_flush():
        flush_calls.append(True)

    async def fake_play(delta):
        pass

    bridge._flush_playback = fake_flush
    bridge._play = fake_play

    ws = FakeWS([
        _audio_delta("item_1", _TEN_MS_DELTA),
        json.dumps({"type": "input_audio_buffer.speech_started"}),
    ])

    await bridge._receive_from_openai(ws)

    assert flush_calls == [True]                       # flushed exactly once
    truncates = _truncates(ws)
    assert len(truncates) == 1                         # one truncate sent
    assert truncates[0]["item_id"] == "item_1"
    assert truncates[0]["content_index"] == 0
    assert truncates[0]["audio_end_ms"] == 10


@pytest.mark.asyncio
async def test_speech_started_without_active_response_is_noop():
    """speech_started with no item currently playing must not flush or truncate."""
    bridge = _bridge()
    flush_calls = []

    async def fake_flush():
        flush_calls.append(True)

    async def fake_play(delta):
        pass

    bridge._flush_playback = fake_flush
    bridge._play = fake_play

    ws = FakeWS([json.dumps({"type": "input_audio_buffer.speech_started"})])

    await bridge._receive_from_openai(ws)

    assert flush_calls == []                            # nothing to flush
    assert _truncates(ws) == []                         # nothing to truncate


@pytest.mark.asyncio
async def test_response_done_resets_tracking():
    """response.done clears the playing item, so a following speech_started is a no-op —
    proving we don't truncate an item whose turn already finished."""
    bridge = _bridge()
    flush_calls = []

    async def fake_flush():
        flush_calls.append(True)

    async def fake_play(delta):
        pass

    bridge._flush_playback = fake_flush
    bridge._play = fake_play

    ws = FakeWS([
        _audio_delta("item_1", _TEN_MS_DELTA),
        json.dumps({"type": "response.done"}),
        json.dumps({"type": "input_audio_buffer.speech_started"}),
    ])

    await bridge._receive_from_openai(ws)

    assert flush_calls == []                            # response.done cleared the item
    assert _truncates(ws) == []                         # so no barge-in fired
