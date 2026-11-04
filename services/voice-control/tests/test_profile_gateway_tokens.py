"""Per-profile gateway bearers (HERMES_PROFILE_GATEWAY_TOKENS).

A profile whose Hermes gateway runs with its own API_SERVER_KEY gets that key sent to it;
every other profile keeps the shared HERMES_GATEWAY_TOKEN. The bearer VoiceMaster itself
requires on its dialing endpoints is not this map: it stays the shared token.

The resolver lives in voicecore; this file pins it, and the dashboard's two Mission
authoring routes that send it.
"""
import pytest
from fastapi.testclient import TestClient

from voicecore import hermes_gateway

from test_mission_authoring import AGENT_DEFAULT, _doc, _gateway_ok, _wire

SHARED = "shared-token"


def test_a_profile_with_its_own_token_gets_that_token():
    env = {"HERMES_GATEWAY_TOKEN": SHARED,
           "HERMES_PROFILE_GATEWAY_TOKENS": "scout=scout-own,vega=vega-own"}
    assert hermes_gateway.gateway_token_for_profile("scout", env) == "scout-own"
    assert hermes_gateway.gateway_token_for_profile(" vega ", env) == "vega-own"


def test_a_profile_without_an_entry_falls_back_to_the_shared_token():
    env = {"HERMES_GATEWAY_TOKEN": SHARED,
           "HERMES_PROFILE_GATEWAY_TOKENS": "scout=scout-own"}
    assert hermes_gateway.gateway_token_for_profile("vega", env) == SHARED
    assert hermes_gateway.gateway_token_for_profile("default", env) == SHARED
    assert hermes_gateway.gateway_token_for_profile("", env) == SHARED
    assert hermes_gateway.gateway_token_for_profile(None, env) == SHARED


def test_an_unknown_profile_gets_the_shared_token_and_stays_unroutable():
    """A token entry is a credential, not a route: it must not make a profile reachable."""
    env = {"HERMES_GATEWAY_TOKEN": SHARED,
           "HERMES_PROFILE_GATEWAY_TOKENS": "ghost=ghost-own",
           "VOICE_CONFIG_DIR": "/nonexistent-voice-config"}
    assert hermes_gateway.gateway_token_for_profile("nobody", env) == SHARED
    assert hermes_gateway.gateway_url_for_profile("ghost", env) is None
    assert hermes_gateway.gateway_url_for_profile("nobody", env) is None


def test_the_default_profile_can_be_given_its_own_token():
    env = {"HERMES_GATEWAY_TOKEN": SHARED,
           "HERMES_PROFILE_GATEWAY_TOKENS": "default=fake"}
    assert hermes_gateway.gateway_token_for_profile("default", env) == "fake"
    assert hermes_gateway.gateway_token_for_profile("", env) == "fake"


@pytest.mark.parametrize("raw", [None, "", " ", ",", "scout=", "=x"])
def test_with_no_usable_map_every_profile_gets_exactly_the_shared_token(raw):
    env = {"HERMES_GATEWAY_TOKEN": f" {SHARED} "}
    if raw is not None:
        env["HERMES_PROFILE_GATEWAY_TOKENS"] = raw
    for name in ("default", "scout", "ghost", ""):
        assert hermes_gateway.gateway_token_for_profile(name, env) == \
            hermes_gateway.gateway_token(env) == SHARED


def test_a_caller_held_shared_token_wins_over_the_environment():
    """The bridges read HERMES_GATEWAY_TOKEN once at start and pass it in."""
    env = {"HERMES_GATEWAY_TOKEN": "env-token",
           "HERMES_PROFILE_GATEWAY_TOKENS": "scout=scout-own"}
    assert hermes_gateway.gateway_token_for_profile(
        "vega", env, default_token="held") == "held"
    assert hermes_gateway.gateway_token_for_profile(
        "scout", env, default_token="held") == "scout-own"


def test_a_token_may_contain_an_equals_sign():
    env = {"HERMES_PROFILE_GATEWAY_TOKENS": "scout=abc==,vega=x=y"}
    assert hermes_gateway.gateway_token_for_profile("scout", env) == "abc=="
    assert hermes_gateway.gateway_token_for_profile("vega", env) == "x=y"


def test_the_dialing_bearer_does_not_accept_a_per_profile_token():
    """bearer_problem is handed the shared token by every bridge; a profile's own key
    must not open the dialing endpoints."""
    assert hermes_gateway.bearer_problem("Bearer scout-own", SHARED) == (401, "unauthorized")
    assert hermes_gateway.bearer_problem(f"Bearer {SHARED}", SHARED) is None
    assert hermes_gateway.bearer_problem("Bearer scout-own", "")[0] == 503


def test_mission_authoring_sends_that_agents_own_token(make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _doc(), _doc(AGENT_DEFAULT))
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_TOKENS", "scout=scout-own")
    transport = _gateway_ok()
    client = TestClient(make_app(transport))

    r = client.post("/api/missions/expand", json={"agent": "agent-a", "prompt": "x"})
    assert r.status_code == 200, r.text
    r = client.post("/api/missions/dictate", data={"agent": "agent-a"},
                    files={"audio": ("r.webm", b"\x1a" * 4096, "audio/webm")})
    assert r.status_code == 200, r.text
    r = client.post("/api/missions/expand", json={"agent": "agent-default", "prompt": "x"})
    assert r.status_code == 200, r.text

    auth = [(c.url.host, c.headers.get("authorization")) for c in transport.calls]
    assert auth[:-1] == [("sprint.test", "Bearer scout-own")] * (len(auth) - 1)
    assert len(auth) >= 3, "dictate made no gateway request"
    assert auth[-1] == ("hermes.test", "Bearer gw-token")
