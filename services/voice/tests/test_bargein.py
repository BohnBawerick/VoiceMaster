"""Tests for A2 barge-in / interruption handling (Mode C).

When the caller talks over Robot, two things must happen: Twilio's already-buffered outbound audio
is flushed (`{"event":"clear"}`) so Robot stops mid-word, and the model's context is truncated
(`conversation.item.truncate`) to what was actually played so its transcript stays honest. Neither
existed before — turn-taking relied entirely on server-VAD while Twilio kept playing stale audio.

Unit-tests the extracted helper, plus an end-to-end drive through media_stream with a scripted
fake OpenAI socket.
"""
import asyncio
import base64
import json

import pytest
from fastapi.testclient import TestClient

import server


class FakeTwilioWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, obj):
        self.sent.append(obj)


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


# -- unit: the barge-in action ------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_barge_in_clears_twilio_and_truncates_openai():
    twilio = FakeTwilioWS()
    openai = FakeWS()
    await server._handle_barge_in(twilio, openai, "MZx", "item_7", 640.0)
    assert twilio.sent == [{"event": "clear", "streamSid": "MZx"}]
    truncs = [m for m in openai.sent if m.get("type") == "conversation.item.truncate"]
    assert truncs and truncs[0]["item_id"] == "item_7"
    assert truncs[0]["content_index"] == 0
    assert truncs[0]["audio_end_ms"] == 640   # int, not float


# -- integration: caller talks over Robot mid-reply ---------------------------------------

class ScriptedOpenAIWS:
    """Yields a scripted list of OpenAI events, then blocks until close() (like the real idle
    socket). Records everything sent. Holds the scripted events until the server sends
    session.update, so the test can't race the Twilio `start` that sets stream_sid (OpenAI
    likewise never emits audio before the session is configured)."""

    def __init__(self, events, patience=5.0):
        self._events = [json.dumps(e) for e in events]
        self.sent = []
        self.patience = patience
        self.timed_out = False
        self._closed = asyncio.Event()
        self._armed = asyncio.Event()
        self.state = type("S", (), {"name": "OPEN"})()

    async def send(self, raw):
        obj = json.loads(raw)
        self.sent.append(obj)
        if obj.get("type") == "session.update":
            self._armed.set()

    async def close(self):
        self.state = type("S", (), {"name": "CLOSED"})()
        self._closed.set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._events and not self._armed.is_set():
            try:
                await asyncio.wait_for(self._armed.wait(), self.patience)
            except asyncio.TimeoutError:
                self.timed_out = True
                raise StopAsyncIteration
        if self._events:
            return self._events.pop(0)
        try:
            await asyncio.wait_for(self._closed.wait(), self.patience)
        except asyncio.TimeoutError:
            self.timed_out = True
        raise StopAsyncIteration

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def client():
    return TestClient(server.app)


def test_barge_in_end_to_end_over_twilio(client, monkeypatch):
    """A caller talking over Robot mid-reply → Twilio gets a `clear`, OpenAI gets a truncate whose
    audio_end_ms matches the audio we relayed (80 μ-law bytes @ 8 kHz = 10 ms)."""
    delta = base64.b64encode(b"\x00" * 80).decode()
    fake = ScriptedOpenAIWS([
        {"type": "response.output_audio.delta", "item_id": "item_1", "delta": delta},
        {"type": "input_audio_buffer.speech_started"},
    ])
    monkeypatch.setattr(server.websockets, "connect", lambda *a, **kw: fake)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")
    tok = server._mint_inbound_token()

    received = []
    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZbarge",
                      "start": {"streamSid": "MZbarge", "callSid": "CAx",
                                "customParameters": {"inbound_token": tok}}})
        received.append(ws.receive_json())   # the relayed audio delta (event=media)
        received.append(ws.receive_json())   # the barge-in flush (event=clear)
        ws.send_json({"event": "stop", "sequenceNumber": "2", "streamSid": "MZbarge"})

    assert any(m.get("event") == "media" for m in received)
    assert {"event": "clear", "streamSid": "MZbarge"} in received
    truncs = [m for m in fake.sent if m.get("type") == "conversation.item.truncate"]
    assert truncs and truncs[0]["item_id"] == "item_1" and truncs[0]["audio_end_ms"] == 10
    assert not fake.timed_out
