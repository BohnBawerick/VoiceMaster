"""VC24 on the phone bridge: one profile resolver, a fail-closed bearer, and an inbound
call answered on the direct Hermes lane behind a pickup check.

The stream tests drive the REAL ``media_stream`` over a TestClient websocket. Only the
engine (``CascadeLiveSession``) and the network are replaced, so what is pinned is the
routing: which lane a call reaches, what direction it is recorded with, and what is left
behind when Hermes is not there.
"""
import json
import os

import pytest
import yaml
from fastapi.testclient import TestClient

from voicecore import hermes_gateway
from voicecore import lkg
from voicecore import profiles
import server
from conftest import FakeOpenAIWS
from profile_helpers import profile_doc, write_config_dir

DIRECT = {"stt": "deepgram", "llm": "hermes-agent", "tts": "elevenlabs"}
VENDOR = {"stt": "deepgram", "llm": "openrouter", "tts": "elevenlabs"}


def direct_agent(aid="robot-direct", **over):
    fields = {"id": aid, "pipeline": "cascade", "providers": dict(DIRECT),
              "knobs": {"voice": "el-voice-1"}}
    fields.update(over)
    return profile_doc(**fields)


def _point_env(monkeypatch, d):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(d))
    monkeypatch.delenv("VOICE_AGENT", raising=False)
    monkeypatch.delenv("HERMES_PROFILE_GATEWAY_URLS", raising=False)
    monkeypatch.delenv(profiles.ENV_HERMES_DIRECT, raising=False)


def _write_pointer(d, inbound=None, outbound=None):
    (d / "active.yaml").write_text(yaml.safe_dump({
        "outlets": {"phone": {"inbound": inbound, "outbound": outbound}}}))


def _write_registry(d, entries):
    (d / "gateways").mkdir(exist_ok=True)
    (d / "gateways" / "gateways.json").write_text(json.dumps({"profiles": entries}))


# ------------------------------------------------------- the one resolver (C1) --

def test_a_profile_the_supervisor_started_is_routable_with_no_env_edit(
        tmp_path, monkeypatch):
    """The point of reading gateways.json: `vega` was unreachable because only
    `scout` was in the env map, and adding a name there is a redeploy."""
    d = write_config_dir(tmp_path, [direct_agent()])
    _point_env(monkeypatch, d)
    assert server.gateway_url_for_profile("vega") is None
    _write_registry(d, {"vega": {"status": "ok", "gateway_url": "http://127.0.0.1:18791/"}})
    # Found on the next call, no restart: the registry is read each time, never cached.
    assert server.gateway_url_for_profile("vega") == "http://127.0.0.1:18791"


@pytest.mark.parametrize("entry", [
    {"status": "incomplete", "gateway_url": "http://127.0.0.1:18791"},
    {"status": "invalid", "gateway_url": "http://127.0.0.1:18791"},
    {"status": "ok"},
    {"status": "ok", "gateway_url": "  "},
    "not-a-map",
])
def test_anything_but_an_ok_entry_with_a_url_routes_nowhere(tmp_path, monkeypatch, entry):
    """Never the default backend: a half-built profile answering as another Agent is
    worse than a refused call."""
    d = write_config_dir(tmp_path, [direct_agent()])
    _point_env(monkeypatch, d)
    _write_registry(d, {"vega": entry})
    assert server.gateway_url_for_profile("vega") is None


def test_the_operators_env_map_beats_the_registry_and_default_ignores_it(
        tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [direct_agent()])
    _point_env(monkeypatch, d)
    _write_registry(d, {"vega": {"status": "ok", "gateway_url": "http://registry:1"},
                        "default": {"status": "invalid", "gateway_url": "http://wrong:1"}})
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", "vega=http://operator:2")
    monkeypatch.setattr(server, "HERMES_GATEWAY_URL", "http://hermes:18789")
    assert server.gateway_url_for_profile("vega") == "http://operator:2"
    # `default` is the container's own top-level gateway. A stray profiles/default/ the
    # supervisor marked invalid must not take the phone's default backend away.
    assert server.gateway_url_for_profile("default") == "http://hermes:18789"
    assert server.gateway_url_for_profile("") == "http://hermes:18789"


def test_a_corrupt_or_missing_registry_is_nothing_not_an_error(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [direct_agent()])
    _point_env(monkeypatch, d)
    assert hermes_gateway.read_gateway_registry() == {}
    (d / "gateways").mkdir()
    (d / "gateways" / "gateways.json").write_text("{not json")
    assert hermes_gateway.read_gateway_registry() == {}
    assert server.gateway_url_for_profile("vega") is None


def test_there_is_one_resolver_not_three():
    """The two bridge copies are gone. A reappearing local parser is how `vega` became
    routable for Mission authoring and unroutable on a call."""
    assert not hasattr(server, "_profile_gateway_map")
    assert not hasattr(server, "HERMES_PROFILE_GATEWAY_URLS")


# ------------------------------------------------ the bearer is fail-closed (F5) --

@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.mark.parametrize("token", ["", "   "])
def test_no_configured_token_refuses_every_caller(client, monkeypatch, token):
    """The scout's F5, reproduced then fixed. With the token unset the check was SKIPPED,
    so an unauthenticated POST reached the dial. It answered 500 only because the test
    env has no Twilio credentials; with them it would have placed the call."""
    monkeypatch.setattr(server, "HERMES_GATEWAY_TOKEN", token)
    dialled = []
    monkeypatch.setattr(server, "Client", lambda *a, **k: dialled.append(a))
    r = client.post("/voice/outbound", json={"brief": "hi", "to": "+61400000000"})
    assert r.status_code == 503
    assert "HERMES_GATEWAY_TOKEN is not set" in r.json()["error"]
    r = client.post("/voice/outbound", json={"brief": "hi", "to": "+61400000000"},
                    headers={"Authorization": "Bearer "})
    assert r.status_code == 503
    assert dialled == []


def test_a_configured_token_still_gates_as_before(client, monkeypatch):
    monkeypatch.setattr(server, "HERMES_GATEWAY_TOKEN", "right")
    body = {"brief": "hi", "to": "+61400000000"}
    assert client.post("/voice/outbound", json=body).status_code == 401
    assert client.post("/voice/outbound", json=body,
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.post("/voice/outbound", json=body, headers={"Authorization": "Bearer right"})
    assert ok.status_code not in (401, 503)


# ------------------------------------------------------- who may answer a call --

def test_only_the_direct_lane_activates_inbound(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [
        direct_agent(), profile_doc(id="vendor-cas", pipeline="cascade",
                                    providers=dict(VENDOR))])
    _point_env(monkeypatch, d)
    assert profiles.load_named_profile("robot-direct", "inbound").agent_id == "robot-direct"
    with pytest.raises(profiles.ProfileError, match="outbound-only unless Hermes itself"):
        profiles.load_named_profile("vendor-cas", "inbound")
    # Outbound for a vendor cascade is exactly what it was.
    assert profiles.load_named_profile("vendor-cas", "outbound").agent_id == "vendor-cas"


def test_the_capability_names_an_outlet_and_a_direction(monkeypatch):
    doc = direct_agent()
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY",
                        {"phone": frozenset({"outbound", "inbound"}),
                         "talk": frozenset({"outbound"})})
    assert profiles.activation_problem(doc, "ctx", "inbound", outlet="phone") is None
    # `or ""` so a missing refusal fails as "expected a refusal", not as a TypeError
    # from searching None: the phone hosting inbound must not vouch for Talk.
    on_talk = profiles.activation_problem(doc, "ctx", "inbound", outlet="talk") or ""
    assert "does not run inbound calls on outlet 'talk'" in on_talk
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {})
    nowhere = profiles.activation_problem(doc, "ctx", "inbound") or ""
    assert "not implemented in this bridge" in nowhere


def test_the_kill_switch_takes_the_lane_out_with_no_rebuild(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [direct_agent()])
    _point_env(monkeypatch, d)
    monkeypatch.setenv(profiles.ENV_HERMES_DIRECT, "false")
    with pytest.raises(profiles.ProfileError, match="VOICE_HERMES_DIRECT_ENABLED=false"):
        profiles.load_named_profile("robot-direct", "inbound")


def test_a_tools_off_direct_agent_runs_outbound_too(tmp_path, monkeypatch):
    """The tools setting is per Agent and Hermes enforces ``tool_choice``, so a tools-off
    direct call is a valid call in both directions. The old refusal ("cannot be
    sandboxed") rested on upstream ignoring ``tool_choice``; it does not any more, and
    tests/test_tool_toggle.py pins what goes on the wire instead."""
    d = write_config_dir(tmp_path, [
        direct_agent("sandboxed", guardrails={"on_call_tools": False}),
        direct_agent("open", guardrails={"on_call_tools": True})])
    _point_env(monkeypatch, d)
    for agent in ("sandboxed", "open"):
        for direction in ("inbound", "outbound"):
            assert profiles.load_named_profile(agent, direction).agent_id == agent
    assert profiles.activation_problem(
        direct_agent("sandboxed", guardrails={"on_call_tools": False}),
        "ctx", "outbound", outlet="phone") is None


# ------------------------------------------------------- the stream, on the wire --

class _Engine:
    """Stands in for CascadeLiveSession; remembers how the bridge built it."""
    built: list = []
    stt_lost = False

    def __init__(self, **kw):
        self.kw = kw
        self._tools_enabled = False
        self.transcript = ["Them: hello", "AI: hi"]
        _Engine.built.append(self)

    async def run(self):
        return None

    async def teardown(self, outcome="ok"):
        self.kw["recorder"].finish(outcome=outcome)


@pytest.fixture
def wire(monkeypatch, tmp_path):
    """A config dir with the direct Agent on phone.inbound, a routable gateway, a fake
    engine, and every side channel captured."""
    _Engine.built = []
    d = write_config_dir(tmp_path, [direct_agent(hermes_profile="vega")])
    _write_pointer(d, inbound="robot-direct")
    _write_registry(d, {"vega": {"status": "ok", "gateway_url": "http://127.0.0.1:18791"}})
    _point_env(monkeypatch, d)
    state = {"probe": True, "probed": [], "events": [], "delivered": [], "realtime": []}

    async def probe(url, **kw):
        state["probed"].append(url)
        return state["probe"]

    async def deliver(mission, transcript):
        state["delivered"].append(mission)

    def connect(url, *a, **kw):
        state["realtime"].append(url)
        return FakeOpenAIWS()

    monkeypatch.setattr(server.hermes_voice, "probe", probe)
    monkeypatch.setattr(server, "deliver_transcript", deliver)
    monkeypatch.setattr(server.websockets, "connect", connect)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(server.cascade_live, "CascadeLiveSession", _Engine)
    monkeypatch.setattr(server.cascade_live, "open_stt", lambda *a, **k: None)
    monkeypatch.setattr(server.eventlog, "append_event",
                        lambda obj, *a, **k: state["events"].append(obj))
    state["dir"] = d
    return state


def _ring(client, *, caller="+61400000001", stream="MZin1"):
    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": stream,
                      "start": {"streamSid": stream, "callSid": "CAx",
                                "customParameters": {
                                    "inbound_token": server._mint_inbound_token(caller)}}})
        ws.send_json({"event": "stop", "sequenceNumber": "2", "streamSid": stream})


def test_an_inbound_call_is_answered_by_the_selected_hermes_profile(client, wire):
    _ring(client)
    assert wire["probed"] == ["http://127.0.0.1:18791"]
    assert wire["realtime"] == []                    # no OpenAI Realtime model in the path
    (engine,) = _Engine.built
    assert engine.kw["direction"] == "inbound"
    assert engine.kw["mission_brief"] == ""          # an inbound Call has no Mission
    assert engine.kw["recorder"].direction == "inbound"
    conversation = engine.kw["hermes_conversation"]
    assert conversation._url == "http://127.0.0.1:18791/v1/chat/completions"
    assert conversation.session_id == "voice-MZin1"
    # The caller is the Twilio-signed `From` that passed the caller list, nothing else.
    assert "You are speaking with +61400000001." in conversation._instructions
    assert wire["delivered"] == []                   # report-back is an outbound thing
    snap = lkg.load(profiles.OUTLET_PHONE, "inbound")
    assert snap is not None and snap.profile.agent_id == "robot-direct"
    assert lkg.load(profiles.OUTLET_PHONE, "outbound") is None


def test_hermes_down_at_pickup_answers_on_realtime_and_cannot_do_it_quietly(client, wire):
    """q6-failure. Removing the fallback makes this red two ways: no Realtime dial, and
    the engine built against a gateway that is not answering."""
    wire["probe"] = False
    _ring(client)
    assert _Engine.built == []
    # The bridge's own defaults: the direct Agent's knobs are an ElevenLabs voice and a
    # Hermes route, which mean nothing to OpenAI.
    assert wire["realtime"] == [f"wss://api.openai.com/v1/realtime?model={server.OPENAI_MODEL}"]
    falls = [e for e in wire["events"] if e.get("type") == "fallback"]
    assert len(falls) == 1
    assert falls[0]["kind"] == "lane" and falls[0]["answered_on"] == "realtime"
    assert falls[0]["outlet"] == "phone" and falls[0]["assigned_agent"] == "robot-direct"
    assert "did not answer the pickup check" in falls[0]["reason"]
    # The Agent that did NOT take the call is not what just completed one, and the
    # assignment is untouched, so the next call tries Hermes again.
    assert lkg.load(profiles.OUTLET_PHONE, "inbound") is None
    assert yaml.safe_load((wire["dir"] / "active.yaml").read_text()) == {
        "outlets": {"phone": {"inbound": "robot-direct", "outbound": None}}}


def test_an_unroutable_profile_at_pickup_is_the_same_loud_fallback(client, wire):
    _write_registry(wire["dir"], {"vega": {"status": "incomplete"}})
    _ring(client)
    assert wire["probed"] == [] and _Engine.built == []
    (fall,) = [e for e in wire["events"] if e.get("type") == "fallback"]
    assert "hermes_profile 'vega' is not routable" in fall["reason"]


def test_an_outbound_mission_is_never_handed_to_a_different_being(client, wire, monkeypatch):
    """Inbound falls back because a ringing phone must be answered. An outbound Mission
    was written for THIS Agent, so with its gateway down the call is refused."""
    d = wire["dir"]
    (d / "agents" / "agent0.yaml").write_text(yaml.safe_dump(
        direct_agent(hermes_profile="vega", guardrails={"on_call_tools": True})))
    _write_pointer(d, outbound="robot-direct")
    wire["probe"] = False
    server._remember_mission("cid-out", server.OutboundMission(brief="b", to="+15550002222"))
    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZo1",
                      "start": {"streamSid": "MZo1", "callSid": "CAx",
                                "customParameters": {"call_id": "cid-out"}}})
    assert _Engine.built == [] and wire["realtime"] == []
    assert [e for e in wire["events"] if e.get("type") == "fallback"] == []
    assert "cid-out" not in server._ACTIVE_OUTBOUND


def test_a_vendor_cascade_on_an_inbound_stream_is_refused_at_the_door(
        client, wire, monkeypatch):
    """Activation already refuses it. This is the same rule held a second time, for the
    day a snapshot reaches the stream some other way (a stale last-known-good, say)."""
    vendor = profiles.ActiveProfile(
        agent_id="vendor-cas", source="<test>", registry=profiles.load_registry(),
        doc=profile_doc(id="vendor-cas", pipeline="cascade", providers=dict(VENDOR)))
    monkeypatch.setattr(server.lkg, "resolve", lambda *a, **k: vendor)
    _ring(client)
    assert _Engine.built == [] and wire["realtime"] == []


def test_the_caller_identity_is_single_use_like_the_token(wire):
    token = server._mint_inbound_token("+61400000001")
    assert server._INBOUND_CALLERS.pop(token, "") == "+61400000001"
    assert server._INBOUND_CALLERS.pop(token, "") == ""


# ------------------------------------------------ the Listening card, end to end --

def test_deepgram_options_travel_from_the_agent_to_the_listen_url():
    """The dashboard writes knobs; this is the other half: the config builder carries
    them to the stt stage and the live client puts them on the URL. An Agent that never
    touched the Listening card must dial the byte-identical URL it dialled before."""
    from voicecore import cascade_config, deepgram_live
    registry = profiles.load_registry()
    untouched = cascade_config.build_cascade_config(direct_agent(), registry, {})["stt"]
    assert untouched["smart_format"] is None and untouched["numerals"] is None
    before = deepgram_live.listen_url(untouched["model"], untouched["language"],
                                      untouched["keyterms"])
    same = deepgram_live.listen_url(
        untouched["model"], untouched["language"], untouched["keyterms"],
        smart_format=untouched["smart_format"], numerals=untouched["numerals"])
    assert before == same and "smart_format" not in same and "numerals" not in same

    tuned = cascade_config.build_cascade_config(
        direct_agent(knobs={"voice": "el-voice-1", "language": "en-GB",
                            "keyterms": ["Hermes"], "smart_format": True,
                            "numerals": False}), registry, {})["stt"]
    url = deepgram_live.listen_url(
        tuned["model"], tuned["language"], tuned["keyterms"],
        smart_format=tuned["smart_format"], numerals=tuned["numerals"])
    assert "smart_format=true" in url and "numerals=false" in url
    assert "language=en-GB" in url and "keyterm=Hermes" in url
    # Turn-taking is VoiceMaster's, so Deepgram's endpointing is never sent.
    assert "endpointing" not in url and "utterance_end_ms" not in url


def test_the_direct_agents_llm_stage_is_the_agents_own_gateway(tmp_path, monkeypatch):
    from voicecore import cascade_config
    d = write_config_dir(tmp_path, [direct_agent(hermes_profile="vega")])
    _point_env(monkeypatch, d)
    _write_registry(d, {"vega": {"status": "ok", "gateway_url": "http://127.0.0.1:18791"}})
    # The env the bridges pass: dict(os.environ). The resolver honours the env it is
    # handed throughout, so an empty one has no VOICE_CONFIG_DIR and finds no registry.
    llm = cascade_config.build_cascade_config(
        direct_agent(hermes_profile="vega"), profiles.load_registry(),
        dict(os.environ))["llm"]
    assert cascade_config.build_cascade_config(
        direct_agent(hermes_profile="vega"), profiles.load_registry(),
        {})["llm"]["endpoint"] is None
    assert llm["kind"] == "hermes" and llm["hermes_profile"] == "vega"
    assert llm["endpoint"] == "http://127.0.0.1:18791"
    assert llm["model"] is None                       # the profile's own model chain
    # A vendor stage is untouched by any of this.
    vendor = cascade_config.build_cascade_config(
        profile_doc(id="v", pipeline="cascade", providers=dict(VENDOR)),
        profiles.load_registry(), {})["llm"]
    assert vendor["kind"] == "chat" and vendor["endpoint"].startswith("https://openrouter")
