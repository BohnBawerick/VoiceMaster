"""Bridge-level test that Mode C emits event-log records (Idea 2).

Drives media_stream through a scripted fake OpenAI socket and asserts a per-turn record (TTFB +
token usage) and a per-call summary are written. append_event is monkeypatched to capture records
in memory rather than touching the shared volume.
"""
import base64

import pytest
from fastapi.testclient import TestClient

import server
from test_bargein import ScriptedOpenAIWS   # reuse the scripted fake


@pytest.fixture
def client():
    return TestClient(server.app)


def test_call_and_turn_records_emitted(client, monkeypatch):
    events = []
    monkeypatch.setattr(server.eventlog, "append_event", lambda obj, *a, **k: events.append(obj))

    delta = base64.b64encode(b"\x00" * 160).decode()   # 160 μ-law bytes @ 8k = 20 ms
    fake = ScriptedOpenAIWS([
        {"type": "input_audio_buffer.speech_stopped"},
        {"type": "response.output_audio.delta", "item_id": "i1", "delta": delta},
        {"type": "response.done", "response": {"usage": {
            "input_tokens": 30, "output_tokens": 12,
            "input_token_details": {"audio_tokens": 25},
            "output_token_details": {"audio_tokens": 8}}}},
    ])
    monkeypatch.setattr(server.websockets, "connect", lambda *a, **kw: fake)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")
    tok = server._mint_inbound_token()

    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZlog",
                      "start": {"streamSid": "MZlog", "callSid": "CAx",
                                "customParameters": {"inbound_token": tok}}})
        ws.receive_json()   # the relayed audio delta — ensures the loop processed the events
        ws.send_json({"event": "stop", "sequenceNumber": "2", "streamSid": "MZlog"})

    turns = [e for e in events if e.get("type") == "turn"]
    calls = [e for e in events if e.get("type") == "call"]
    assert turns and turns[0]["usage"] == {"input_tokens": 30, "output_tokens": 12,
                                           "input_audio_tokens": 25, "output_audio_tokens": 8}
    assert turns[0]["ttfb_ms"] is not None and turns[0]["ttfb_ms"] >= 0
    assert calls and calls[0]["mode"] == "twilio" and calls[0]["direction"] == "inbound"
    assert calls[0]["num_turns"] == 1
