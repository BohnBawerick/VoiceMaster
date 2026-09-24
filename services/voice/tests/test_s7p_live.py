"""s7p pins — Aura live streaming TTS (c3) + hermes_profile gateway routing (c6).

The Aura pin reuses the s7 rig: a SlowStream body that yields chunks with real delays,
so ">=2 frames before the body completes" proves true pipelining, not first-byte-then-
block. The c6 pins intercept the real httpx layer (respx) — the request URL is the
observable, not the mapping dict.
"""
import asyncio
import base64
import json
import time

import httpx
import pytest
import respx

import server
from voicecore.cascade_live import CascadeLiveSession, FRAME_BYTES
from test_cascade_live import (FakeDeepgram, FakeRecorder, FakeTwilioWS,
                               SlowStream, CONFIG, ENV)
from tts_fake import http_tts_connect

AURA_CONFIG = json.loads(json.dumps(CONFIG))    # deep copy of the s7 fixture
AURA_CONFIG["tts"] = {"provider": "deepgram-aura", "secret_env": "DEEPGRAM_API_KEY",
                      "voice": "aura-2-thalia-en", "speed": None, "format": "mp3",
                      "model": None}
AURA_ENV = dict(ENV, DEEPGRAM_API_KEY="dg-key")


def make_aura_transport(*, tts_chunks, tts_delay=0.0, tts_done=None, seen=None):
    def handler(request):
        if request.url.host == "openrouter.ai":
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant",
                                         "content": "Hello caller."}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        if request.url.host == "api.deepgram.com":
            if seen is not None:
                seen.append(request)
            return httpx.Response(200, stream=SlowStream(tts_chunks, tts_delay,
                                                         tts_done))
        raise AssertionError(f"unexpected host {request.url.host} — Aura config must "
                             "never dial ElevenLabs (no silent remap)")
    return httpx.MockTransport(handler)


def test_aura_live_frames_stream_while_body_in_flight():
    """c3: Aura chunks are paced into 20ms mulaw Twilio frames while synthesis is in
    flight; oversize chunks re-framed to FRAME_BYTES; request asks Deepgram for
    mulaw/8000 with Token auth."""
    done: list = []
    seen: list = []
    ws = FakeTwilioWS()
    rec = FakeRecorder()
    transport = make_aura_transport(tts_chunks=[b"\xff" * 320] * 4, tts_delay=0.03,
                                    tts_done=done, seen=seen)
    session = CascadeLiveSession(
        twilio_ws=ws, stream_sid="MZtest", config=AURA_CONFIG, profile=None,
        recorder=rec, env=AURA_ENV, stt=FakeDeepgram(),
        hermes_call=None, tools_enabled=False, filler_debounce_s=2.0, detector=None,
        transport=transport, tts_connect=http_tts_connect(transport), retain_default=False, hindsight_url="")
    asyncio.run(session._agent_turn(opener=True))
    assert done, "TTS stream never completed"
    frames_before_done = [t for t, o in ws.events("media") if t < done[0]]
    assert len(frames_before_done) >= 2                    # true pipelining
    payloads = [base64.b64decode(o["media"]["payload"]) for _, o in ws.events("media")]
    assert all(len(p) == FRAME_BYTES for p in payloads[:-1])   # re-framed to 20ms
    # request shape: mulaw/8000, voice as the model param, Deepgram Token auth
    req = seen[0]
    assert req.url.host == "api.deepgram.com" and req.url.path == "/v1/speak"
    assert req.url.params["model"] == "aura-2-thalia-en"
    assert req.url.params["encoding"] == "mulaw"
    assert req.url.params["sample_rate"] == "8000"
    assert req.url.params["container"] == "none"
    assert req.headers["authorization"] == "Token dg-key"
    assert "xi-api-key" not in req.headers
    body = json.loads(req.content)
    assert body == {"text": "Hello caller."}               # no ElevenLabs body leak


# ------------------------------------------------------------------- c6 -----


def _snap(doc):
    class Snap:
        pass
    s = Snap()
    s.doc = doc
    return s


def test_profile_of_snapshot_defaults():
    assert server._profile_of_snapshot(_snap({})) == "default"
    assert server._profile_of_snapshot(_snap({"hermes_profile": "  "})) == "default"
    assert server._profile_of_snapshot(_snap({"hermes_profile": "scout"})) \
        == "scout"


def test_gateway_url_for_profile_mapping(monkeypatch):
    monkeypatch.setattr(server, "HERMES_GATEWAY_URL", "http://hermes:18789")
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", "scout=http://localhost:18790/, x=http://h:1")
    assert server.gateway_url_for_profile("default") == "http://hermes:18789"
    assert server.gateway_url_for_profile("") == "http://hermes:18789"
    assert server.gateway_url_for_profile(None) == "http://hermes:18789"
    assert server.gateway_url_for_profile("scout") == "http://localhost:18790"
    assert server.gateway_url_for_profile("x") == "http://h:1"
    assert server.gateway_url_for_profile("nope") is None
    # the map can also override default
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", "default=http://other:1111")
    assert server.gateway_url_for_profile("default") == "http://other:1111"


@respx.mock
def test_call_hermes_agent_routes_default_profile(monkeypatch):
    monkeypatch.setattr(server, "HERMES_GATEWAY_URL", "http://hermes:18789")
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", "")
    route = respx.post("http://hermes:18789/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": "from default"}}]}))
    out = asyncio.run(server.call_hermes_agent("ping"))
    assert out == "from default"
    assert route.called


@respx.mock
def test_call_hermes_agent_routes_mapped_profile(monkeypatch):
    monkeypatch.setattr(server, "HERMES_GATEWAY_URL", "http://hermes:18789")
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", "scout=http://localhost:18790")
    sr = respx.post("http://localhost:18790/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": "from scout"}}]}))
    default = respx.post("http://hermes:18789/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": "WRONG GATEWAY"}}]}))
    out = asyncio.run(server.call_hermes_agent("ping", profile="scout"))
    assert out == "from scout"
    assert sr.called and not default.called                # routed, not remapped


@respx.mock
def test_call_hermes_agent_unknown_profile_fails_honestly(monkeypatch):
    """Unknown profile: an honest spoken refusal, ZERO network — never a silent
    fallback to the default profile's backend."""
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", "")
    out = asyncio.run(server.call_hermes_agent("ping", profile="ghost"))
    assert "ghost" in out and "not connected" in out
    assert not respx.calls                                 # zero requests


def test_current_hermes_profile_without_a_call_context():
    assert server._current_hermes_profile() == "default"
