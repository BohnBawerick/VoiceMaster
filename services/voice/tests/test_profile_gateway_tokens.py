"""Per-profile gateway bearers on the phone bridge (HERMES_PROFILE_GATEWAY_TOKENS).

Every request this bridge makes to an Agent's Hermes gateway - the in-call hermes_agent
tool, the direct lane's conversation, the post-call summary - carries that profile's own
token when one is configured, else the shared HERMES_GATEWAY_TOKEN. The bearer this
bridge requires on /voice/outbound stays the shared token.
"""
import asyncio

import httpx
import respx
from fastapi.testclient import TestClient

import server
from voicecore import cascade_config
from voicecore import profiles
from voicecore import summary as call_summary
from voicecore import hermes_gateway

SHARED = "shared-token"
REAL_CALL = [
    "AI: Hi, this is the clinic calling about Thursday.",
    "Them: Yes, I need to move it to Friday afternoon if that works for you.",
]


def _wire(monkeypatch, tokens):
    monkeypatch.setattr(server, "HERMES_GATEWAY_URL", "http://hermes:18789")
    monkeypatch.setattr(server, "HERMES_GATEWAY_TOKEN", SHARED)
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS",
                       "scout=http://scout.test,vega=http://vega.test")
    if tokens is None:
        monkeypatch.delenv("HERMES_PROFILE_GATEWAY_TOKENS", raising=False)
    else:
        monkeypatch.setenv("HERMES_PROFILE_GATEWAY_TOKENS", tokens)


def _ok(text="done"):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


@respx.mock
def test_the_tool_sends_the_profiles_own_token_else_the_shared_one(monkeypatch):
    _wire(monkeypatch, "scout=scout-own")
    scout = respx.post("http://scout.test/v1/chat/completions").mock(return_value=_ok())
    vega = respx.post("http://vega.test/v1/chat/completions").mock(return_value=_ok())
    default = respx.post("http://hermes:18789/v1/chat/completions").mock(return_value=_ok())

    asyncio.run(server.call_hermes_agent("ping", profile="scout"))
    asyncio.run(server.call_hermes_agent("ping", profile="vega"))
    asyncio.run(server.call_hermes_agent("ping"))

    assert scout.calls.last.request.headers["Authorization"] == "Bearer scout-own"
    assert vega.calls.last.request.headers["Authorization"] == f"Bearer {SHARED}"
    assert default.calls.last.request.headers["Authorization"] == f"Bearer {SHARED}"


@respx.mock
def test_with_no_map_the_tool_sends_the_shared_token_as_before(monkeypatch):
    _wire(monkeypatch, None)
    scout = respx.post("http://scout.test/v1/chat/completions").mock(return_value=_ok())
    asyncio.run(server.call_hermes_agent("ping", profile="scout"))
    assert scout.calls.last.request.headers["Authorization"] == f"Bearer {SHARED}"


@respx.mock
def test_an_unknown_profile_with_a_token_entry_is_still_refused(monkeypatch):
    _wire(monkeypatch, "ghost=ghost-own")
    out = asyncio.run(server.call_hermes_agent("ping", profile="ghost"))
    assert "not connected" in out
    assert not respx.calls


def test_the_summary_uses_the_agents_own_token(monkeypatch):
    _wire(monkeypatch, "scout=scout-own")
    asked = []

    async def spy(prompt, *, gateway_url, token, **kw):
        asked.append((gateway_url, token))
        return "a summary"

    monkeypatch.setattr(hermes_gateway, "ask_chat", spy)
    for name in ("scout", "vega"):
        snapshot = type("S", (), {"doc": {"hermes_profile": name}})()
        assert asyncio.run(server._summariser_for(snapshot)(REAL_CALL))[1] == \
            call_summary.STATE_WRITTEN
    assert asked == [("http://scout.test", "scout-own"), ("http://vega.test", SHARED)]


def test_the_direct_lane_conversation_uses_the_agents_own_token(monkeypatch):
    from test_cascade_live import ENV, FakeDeepgram, FakeTwilioWS
    _wire(monkeypatch, "scout=scout-own")
    doc = {"id": "scout-direct", "pipeline": "cascade", "hermes_profile": "scout",
           "providers": {"stt": "deepgram", "llm": profiles.HERMES_LLM_PROVIDER,
                         "tts": "elevenlabs"},
           "knobs": {"voice": "el-voice-1"}}
    registry = profiles.load_registry(profiles.config_dir(ENV))
    snapshot = profiles.ActiveProfile(agent_id="scout-direct", source="test", doc=doc,
                                      registry=registry)
    env = {**ENV, "HERMES_PROFILE_GATEWAY_URLS": "scout=http://scout.test"}
    config = cascade_config.build_cascade_config(doc, registry, env)
    mission = type("M", (), {"to": "+61491570156", "brief": "say hello"})()
    session = server._build_cascade_session(
        FakeTwilioWS(), "MZtok", config, snapshot, None, env, mission,
        FakeDeepgram(), None, None)
    assert session._hermes._url == "http://scout.test/v1/chat/completions"
    assert session._hermes._token == "scout-own"


def test_outbound_still_requires_the_shared_token_not_a_profiles_own(monkeypatch):
    _wire(monkeypatch, "scout=scout-own")
    client = TestClient(server.app)
    body = {"brief": "hi", "to": "+61491570156"}
    assert client.post("/voice/outbound", json=body,
                       headers={"Authorization": "Bearer scout-own"}).status_code == 401
    ok = client.post("/voice/outbound", json=body,
                     headers={"Authorization": f"Bearer {SHARED}"})
    assert ok.status_code not in (401, 503)
