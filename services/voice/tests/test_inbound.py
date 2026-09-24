"""Tests for the INBOUND caller lockdown (owner-only calling).

Two doors are gated:
1. /voice/webhook — the caller's `From` (part of Twilio's signed payload, so trustworthy)
   must be in VOICE_INBOUND_ALLOWED_CALLERS. FAIL-CLOSED: an empty list rejects everyone
   (deliberately the opposite default of the outbound allow-list — "only the owner can
   call the Robot" must not silently open if the env var is lost).
2. /voice/stream — a direct WebSocket connection (bypassing Twilio entirely) must never
   arm an OpenAI session: the webhook mints a single-use `inbound_token` into the TwiML
   <Parameter>, and `start` events without a valid token (or outbound call_id) are refused
   before any session.update or audio forwarding. Proven exploitable pre-fix (2026-07-13
   probe got a full-tool Robot session straight off the public wss URL).
"""
import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

import server
from conftest import FakeOpenAIWS

WEBHOOK_URL = "https://voiceh.test/voice/webhook"
OWNER = "+61491570156"
# Stands in for a regional auth token: a real signing key that is NOT the REST token.
REGION_TOKEN = "regional-token"


@pytest.fixture
def client():
    return TestClient(server.app)


def signed_post(client, params, token="twiliotest"):
    """POST /voice/webhook with a valid Twilio signature for the pinned public host."""
    sig = RequestValidator(token).compute_signature(WEBHOOK_URL, params)
    return client.post("/voice/webhook", data=params, headers={"X-Twilio-Signature": sig})


# -- door 1: the webhook caller gate ------------------------------------------------------

def test_webhook_rejects_unknown_caller(client):
    r = signed_post(client, {"CallSid": "CAx", "From": "+15551234567", "To": "+61855501234"})
    assert r.status_code == 200                    # Twilio still needs valid TwiML back
    assert "<Stream" not in r.text                 # no media stream for strangers
    assert "<Hangup" in r.text                     # polite reject + hangup
    assert "inbound_token" not in r.text


def test_webhook_allows_owner_and_mints_single_use_token(client):
    r = signed_post(client, {"CallSid": "CAx", "From": OWNER, "To": "+61855501234"})
    assert r.status_code == 200
    assert "wss://voiceh.test/voice/stream" in r.text
    assert 'name="inbound_token"' in r.text
    tok = r.text.split('value="')[1].split('"')[0]
    assert server._take_inbound_token(tok) is True     # minted and valid
    assert server._take_inbound_token(tok) is False    # single-use


def test_webhook_fails_closed_when_list_empty(client, monkeypatch):
    monkeypatch.setattr(server, "ALLOWED_CALLERS", frozenset())
    r = signed_post(client, {"CallSid": "CAx", "From": OWNER, "To": "+61855501234"})
    assert "<Stream" not in r.text and "<Hangup" in r.text


# -- door 1a: the signature gate spans Twilio's REGIONAL tokens ---------------------------

def test_webhook_accepts_a_regional_signing_token(client, monkeypatch):
    """Twilio signs with the auth token of the REGION that handled the call.

    A number handled outside the default US1 region gets webhooks signed with that region's
    token - never the account's primary/REST token. Validating against the REST token
    alone 403s every real call (2026-07-15 outage) while hand-signed tests still pass.
    """
    monkeypatch.setattr(server, "TWILIO_SIGNING_TOKENS", ("twiliotest", REGION_TOKEN))
    r = signed_post(client, {"CallSid": "CAx", "From": OWNER, "To": "+61855501234"},
                    token=REGION_TOKEN)
    assert r.status_code == 200
    assert "<Stream" in r.text and 'name="inbound_token"' in r.text


def test_webhook_still_rejects_an_unconfigured_token(client, monkeypatch):
    """Accepting several tokens must not degrade into accepting any signature."""
    monkeypatch.setattr(server, "TWILIO_SIGNING_TOKENS", ("twiliotest", REGION_TOKEN))
    r = signed_post(client, {"CallSid": "CAx", "From": OWNER, "To": "+61855501234"},
                    token="some-other-accounts-token")
    assert r.status_code == 403


# -- door 2: the media-stream token gate --------------------------------------------------

def test_stream_without_token_never_arms_openai(client, monkeypatch):
    """A direct wss connection (no Twilio, no token) must get NO session and NO audio path."""
    fake = FakeOpenAIWS()
    monkeypatch.setattr(server.websockets, "connect", lambda *a, **kw: fake)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")

    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        # media BEFORE start must not be forwarded (nothing is armed yet)
        ws.send_json({"event": "media", "streamSid": "MZx", "media": {"payload": "AAAA"}})
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZx",
                      "start": {"streamSid": "MZx", "callSid": "CAx"}})
    assert fake.state.name == "CLOSED" and not fake.timed_out
    assert fake.sent == []      # no session.update, no greeting, no forwarded audio


def test_stream_with_forged_token_refused(client, monkeypatch):
    fake = FakeOpenAIWS()
    monkeypatch.setattr(server.websockets, "connect", lambda *a, **kw: fake)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")

    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZx",
                      "start": {"streamSid": "MZx", "callSid": "CAx",
                                "customParameters": {"inbound_token": "forged-nonsense"}}})
    assert fake.state.name == "CLOSED" and fake.sent == []


def test_stream_with_minted_token_arms_full_robot(client, monkeypatch):
    fake = FakeOpenAIWS()
    monkeypatch.setattr(server.websockets, "connect", lambda *a, **kw: fake)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")
    tok = server._mint_inbound_token()

    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZx",
                      "start": {"streamSid": "MZx", "callSid": "CAx",
                                "customParameters": {"inbound_token": tok}}})
        ws.send_json({"event": "media", "streamSid": "MZx", "media": {"payload": "AAAA"}})
        ws.send_json({"event": "stop", "sequenceNumber": "3", "streamSid": "MZx"})
    session_updates = [m for m in fake.sent if m.get("type") == "session.update"]
    assert len(session_updates) == 1
    assert session_updates[0]["session"]["tools"] == server.TOOLS   # full Robot, inbound
    appended = [m for m in fake.sent if m.get("type") == "input_audio_buffer.append"]
    assert len(appended) == 1                                       # armed → audio flows
    assert fake.state.name == "CLOSED"                              # stop still tears down
