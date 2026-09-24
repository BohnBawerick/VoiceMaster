"""The Agent's tools setting reaches Hermes on the wire (guardrails.on_call_tools).

Before this, the direct lane sent no ``tool_choice`` at all, so a tools-off and a tools-on
Agent produced byte-identical requests and Hermes ran the profile's full tool set on both;
and an outbound tools-off direct Agent was refused as "cannot be sandboxed". The merged
Hermes server enforces ``tool_choice``, so VoiceMaster now sends it on every turn:
``none`` for a tools-off Agent, ``auto`` for a tools-on one.

Everything here reads the SERIALISED request out of a fake transport. The Agents are
on-disk YAML through the real loader and the real registry, the config comes from the
real ``build_cascade_config`` and the client is the real ``HermesConversation``. No paid
model is called and no Hermes tool exists behind the transport.
"""
import asyncio
import copy
import json
import os

import httpx
import pytest
import respx

from voicecore import cascade_config
from voicecore import cascade_live
from voicecore import hermes_gateway
from voicecore import hermes_voice
from voicecore import profiles
from voicecore import summary as call_summary
from voicecore.cascade_live import CascadeLiveSession
from profile_helpers import write_config_dir
from test_cascade_live import ENV, FakeDeepgram, FakeRecorder, FakeTwilioWS, SlowStream
from test_hermes_voice import ok_stream
# The real stream fixtures of the phone suite: the pointer, the fake engine, the ring.
from test_direct_lane_phone import (  # noqa: F401  (fixtures)
    _Engine, _ring, _write_pointer, _write_registry, client, direct_agent, wire)
import server
from tts_fake import http_tts_connect

TOOL_CHOICE = {True: "auto", False: "none", None: "auto"}
NAMED_URL = "http://hermes.test:18791"
DEFAULT_URL = "http://hermes.test:18789"

# tools setting x direction x Hermes profile: every cell is a real, valid direct call.
MATRIX = [pytest.param(tools, direction, profile,
                       id=f"tools-{tools}-{direction}-{profile}")
          for tools in (True, False, None)
          for direction in ("inbound", "outbound")
          for profile in ("default", "vega")]


def _agent(tools, profile, aid="a"):
    doc = direct_agent(aid)
    if profile != "default":
        doc["hermes_profile"] = profile
    if tools is not None:
        doc["guardrails"] = {"on_call_tools": tools}
    return doc


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    def build(*docs):
        d = write_config_dir(tmp_path, list(docs))
        _write_registry(d, {"vega": {"status": "ok", "gateway_url": NAMED_URL}})
        monkeypatch.setenv("VOICE_CONFIG_DIR", str(d))
        monkeypatch.delenv("VOICE_AGENT", raising=False)
        monkeypatch.delenv("HERMES_PROFILE_GATEWAY_URLS", raising=False)
        monkeypatch.delenv(profiles.ENV_HERMES_DIRECT, raising=False)
        monkeypatch.setenv("HERMES_GATEWAY_URL", DEFAULT_URL)
        monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {
            "phone": frozenset({"inbound", "outbound"}),
            "talk": frozenset({"inbound", "outbound"})})
        return d
    return build


class Gateway:
    """An inert Hermes: records every request, executes nothing."""

    def __init__(self, *, fail_first=0):
        self.bodies: list = []
        self.urls: list = []
        self.fail_first = fail_first
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request):
        if request.url.host in ("hermes.test", "127.0.0.1"):
            self.bodies.append(json.loads(request.content))
            self.urls.append(str(request.url))
            if self.fail_first:
                self.fail_first -= 1
                return httpx.Response(503, json={"error": "down"})
            return httpx.Response(200, stream=SlowStream(
                list(ok_stream("Right, I can help with that today. ", "Anything else?")), 0.0),
                headers={"Content-Type": "text/event-stream"})
        if request.url.host == "api.elevenlabs.io":
            return httpx.Response(200, stream=SlowStream([b"\xff" * 480], 0.0))
        raise AssertionError(f"unexpected host {request.url.host}")


def real_config(agent_id, direction):
    """Agent id -> (profile, config) through the loader the bridges use."""
    profile = profiles.load_named_profile(agent_id, direction)
    config = cascade_config.build_cascade_config(
        profile.doc, profile.registry, dict(os.environ),
        base_prompt=cascade_live.base_prompt_for(profile.doc))
    return profile, config


def session_for(agent_id, direction, gateway, call_id="MZtoggle"):
    """A real CascadeLiveSession over the real config; only the network is fake."""
    profile, config = real_config(agent_id, direction)
    conversation = cascade_live.hermes_conversation_for(
        config, call_id=call_id, token="gw-token", transport=gateway.transport)
    return CascadeLiveSession(
        twilio_ws=FakeTwilioWS(), stream_sid=call_id, config=config, profile=profile,
        recorder=FakeRecorder(), env=ENV, stt=FakeDeepgram(),
        filler_debounce_s=2.0, transport=gateway.transport, tts_connect=http_tts_connect(gateway.transport), hindsight_url="",
        hermes_conversation=conversation, direction=direction)


def user_says(session, text):
    session.messages.append({"role": "user", "content": text})
    session.transcript.append(f"Them: {text}")


def assert_switch(bodies, expected):
    """Every serialised request carries ``tool_choice`` == expected, and never ``tools``."""
    assert bodies, "no request reached the gateway"
    for i, body in enumerate(bodies):
        assert "tool_choice" in body, f"request {i} has no tool_choice: {sorted(body)}"
        assert body["tool_choice"] == expected, (i, body["tool_choice"])
        assert "tools" not in body     # `tools: []` enforces nothing; never the mechanism


# ------------------------------------------------ the reproduction, as a regression --

@pytest.mark.parametrize("direction", ["inbound", "outbound"])
@pytest.mark.parametrize("profile", ["default", "vega"])
def test_a_tools_off_and_a_tools_on_agent_do_not_send_the_same_request(
        config_dir, direction, profile):
    """The bug: tools_on and tools_off Agents differing ONLY in guardrails.on_call_tools
    produced identical bodies (no tool_choice on either), so Hermes ran full tools on both.
    The system prompt is worded for the setting, so it is set aside to show that the
    switch itself is the difference."""
    config_dir(_agent(True, profile, "on"), _agent(False, profile, "off"))
    bodies = {}
    for aid in ("on", "off"):
        gateway = Gateway()
        conversation = cascade_live.hermes_conversation_for(
            real_config(aid, direction)[1], call_id="MZ1", token="t",
            transport=gateway.transport)

        async def go():
            return [s async for s in conversation.stream_turn("hello there")]
        asyncio.run(go())
        (bodies[aid],) = gateway.bodies
    assert bodies["on"]["tool_choice"] == "auto"
    assert bodies["off"]["tool_choice"] == "none"
    stripped = {aid: {k: v for k, v in b.items() if k not in ("tool_choice", "messages")}
                for aid, b in bodies.items()}
    assert stripped["on"] == stripped["off"]
    user = {aid: b["messages"][-1] for aid, b in bodies.items()}
    assert user["on"] == user["off"] == {"role": "user", "content": "hello there"}


@pytest.mark.parametrize("profile", ["default", "vega"])
def test_an_outbound_tools_off_direct_agent_is_no_longer_refused(config_dir, profile):
    """The old refusal, verbatim: 'the direct Hermes lane cannot be sandboxed'."""
    config_dir(_agent(False, profile, "off"), _agent(None, profile, "unset"))
    for aid in ("off", "unset"):
        assert profiles.load_named_profile(aid, "outbound").agent_id == aid
        assert profiles.activation_problem(
            profiles.load_named_profile(aid, "outbound").doc, "ctx", "outbound",
            outlet="phone") is None


# ------------------------------------------------------- every turn, both directions --

@pytest.mark.parametrize("tools,direction,profile", MATRIX)
def test_every_turn_of_a_direct_call_carries_the_agents_switch(
        config_dir, tools, direction, profile):
    config_dir(_agent(tools, profile))
    gateway = Gateway()
    session = session_for("a", direction, gateway)

    async def run():
        await session._agent_turn(opener=(direction == "inbound"))
        user_says(session, "what is on my calendar")
        await session._agent_turn()
        user_says(session, "and tomorrow")
        await session._agent_turn()
    asyncio.run(run())
    assert len(gateway.bodies) == 3
    assert_switch(gateway.bodies, TOOL_CHOICE[tools])
    expected_host = NAMED_URL if profile == "vega" else DEFAULT_URL
    assert {u.rsplit("/v1/", 1)[0] for u in gateway.urls} == {expected_host}
    # Per-call conversation isolation is untouched: one new message per turn.
    later = [m for m in gateway.bodies[2]["messages"] if m["role"] != "system"]
    assert later == [{"role": "user", "content": "and tomorrow"}]


@pytest.mark.parametrize("tools", [True, False])
def test_an_absent_direct_setting_is_on_but_malformed_values_never_open_tools(
        config_dir, tools):
    config_dir(_agent(None, "default", "unset"))
    _, config = real_config("unset", "inbound")
    assert config["llm"]["tool_choice"] == "auto"
    for junk in ("yes", "true", 1, [], {"x": 1}, None):
        doc = copy.deepcopy(profiles.load_named_profile("unset", "inbound").doc)
        doc["guardrails"] = {"on_call_tools": junk}
        assert cascade_config.build_cascade_config(
            doc, profiles.load_named_profile("unset", "inbound").registry, {},
            base_prompt="x")["llm"]["tool_choice"] == "none", junk
    doc = profiles.load_named_profile("unset", "inbound").doc
    for guardrails in (None, "true", [], 1):
        assert profiles.on_call_tools_of(dict(doc, guardrails=guardrails)) is False
    assert profiles.on_call_tools_of(dict(doc, guardrails={})) is True
    for pipeline, providers in (("realtime", {"realtime": "openai-gpt-realtime"}),
                                ("cascade", {"llm": "gpt-4.1"})):
        other = dict(doc, pipeline=pipeline, providers=providers)
        assert profiles.on_call_tools_of(other) is False
        assert profiles.on_call_tools_of(dict(other, guardrails={"on_call_tools": True})) is True
    assert hermes_gateway.tool_choice_for(tools) == TOOL_CHOICE[tools]
    assert hermes_gateway.tool_choice_for("false") == "none"


# --------------------------------------------------------------------------- retries --

@pytest.mark.parametrize("tools", [True, False])
def test_a_retried_turn_keeps_the_switch_on_every_attempt(config_dir, tools, monkeypatch):
    """A 503 before any byte is retried with the same message. The resend must carry the
    same switch: a retry that dropped it would run the tools the first attempt cut."""
    config_dir(_agent(tools, "vega"))
    monkeypatch.setattr(cascade_live, "HERMES_RETRY_BACKOFF_S", (0.0,))
    gateway = Gateway(fail_first=2)
    session = session_for("a", "outbound", gateway)
    user_says(session, "book it")
    asyncio.run(session._agent_turn())
    assert len(gateway.bodies) == 3                     # two refusals, then the answer
    assert_switch(gateway.bodies, TOOL_CHOICE[tools])
    assert gateway.bodies[0] == gateway.bodies[1] == gateway.bodies[2]


# ------------------------------------------------------ the prompt agrees with the wire --

def test_the_prompt_does_not_promise_tools_the_request_has_cut(config_dir):
    config_dir(_agent(True, "default", "on"), _agent(False, "default", "off"))
    on = real_config("on", "inbound")[1]["llm"]["system_prompt"]
    off = real_config("off", "inbound")[1]["llm"]["system_prompt"]
    assert "your normal tools" in on and "no tools" not in on
    assert "no tools on this call" in off and "normal tools" not in off
    assert "hermes_agent" not in on + off


# ------------------------------------------- a malformed value is refused, not weakened --

@pytest.mark.parametrize("bad", [None, "", "required", "NONE", "Auto", True, False, 0,
                                 ["none"], {"type": "function"}])
def test_a_malformed_tool_choice_is_refused_before_anything_is_sent(bad):
    gateway = Gateway()
    with pytest.raises(ValueError, match="tool_choice"):
        hermes_voice.HermesConversation(
            gateway_url=DEFAULT_URL, token="t", call_id="MZ1", tool_choice=bad,
            transport=gateway.transport)
    assert gateway.bodies == []


def test_a_conversation_cannot_be_built_without_a_switch():
    with pytest.raises(TypeError):
        hermes_voice.HermesConversation(gateway_url=DEFAULT_URL, token="t", call_id="MZ1")


def test_a_config_with_no_switch_is_refused_at_the_construction_site(config_dir):
    """A config that lost its tool_choice must not build a conversation that then sends
    nothing (which upstream reads as tools on)."""
    config_dir(_agent(False, "default"))
    _, config = real_config("a", "inbound")
    del config["llm"]["tool_choice"]
    gateway = Gateway()
    with pytest.raises(ValueError, match="tool_choice"):
        cascade_live.hermes_conversation_for(config, call_id="MZ1", token="t",
                                             transport=gateway.transport)
    assert gateway.bodies == []


# ------------------------------------------------------------------ negative regression --

def test_the_wire_assertion_fails_if_tool_choice_is_removed(config_dir, monkeypatch):
    """Sabotage in the suite itself. Strip the field from the body the client builds and
    the wire check every test above relies on must go red, for both settings - otherwise
    those tests would pass against a client that silently stopped sending it."""
    config_dir(_agent(True, "default", "on"), _agent(False, "default", "off"))
    real_body = hermes_voice.HermesConversation._body

    def without_switch(self, text):
        body = real_body(self, text)
        body.pop("tool_choice")
        return body

    monkeypatch.setattr(hermes_voice.HermesConversation, "_body", without_switch)
    for aid, tools in (("on", True), ("off", False)):
        gateway = Gateway()
        conversation = cascade_live.hermes_conversation_for(
            real_config(aid, "inbound")[1], call_id="MZ1", token="t",
            transport=gateway.transport)

        async def go():
            return [s async for s in conversation.stream_turn("hi")]
        asyncio.run(go())
        with pytest.raises(AssertionError, match="no tool_choice"):
            assert_switch(gateway.bodies, TOOL_CHOICE[tools])


# ------------------------------------------------------- summaries and Missions stay cut --

@pytest.mark.parametrize("tools", [True, False])
@respx.mock
def test_a_summary_is_tools_off_whatever_the_agents_setting(config_dir, tools):
    """The Agent that was on the call writes the summary, listen-only. Its tools setting
    must not open that turn: summary and Mission are cut through the same field."""
    config_dir(_agent(tools, "vega"))
    route = respx.post(f"{NAMED_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [
            {"message": {"content": "They asked about a delivery."}}]}))
    profile = profiles.load_named_profile("a", "inbound")
    url = hermes_gateway.gateway_url_for_profile(hermes_gateway.hermes_profile_of(profile.doc))
    summariser = call_summary.make_summariser(gateway_url=url, token="t", env={})
    text = asyncio.run(summariser([
        "Them: hi, I'm calling about the delivery that was meant to arrive on Tuesday",
        "AI: sure, let me look that up for you - it went out Monday and is due tomorrow",
        "Them: great, thanks, that's all I needed"]))
    assert text == ("They asked about a delivery.", call_summary.STATE_WRITTEN)
    (call,) = route.calls
    assert json.loads(call.request.read())["tool_choice"] == "none"


def test_the_listen_only_guard_refuses_a_tools_on_body():
    """`auto` is a valid switch for a conversation and never for summaries or Missions."""
    body = hermes_gateway.listen_only_chat_body([{"role": "user", "content": "x"}])
    assert body["tool_choice"] == "none"
    hermes_gateway.assert_listen_only(body)
    for choice in ("auto", None, "required"):
        bad = dict(body)
        if choice is None:
            bad.pop("tool_choice")
        else:
            bad["tool_choice"] = choice
        with pytest.raises(ValueError, match="tool_choice=none"):
            hermes_gateway.assert_listen_only(bad)


# ---------------------------- the real phone stream: construction site and pickup fallback --

def _agent_on_disk(wire_state, tools):
    import yaml
    (wire_state["dir"] / "agents" / "agent0.yaml").write_text(
        yaml.safe_dump(_agent(tools, "vega", "robot-direct")))


def _turn_on(engine, gateway, text="hello"):
    conversation = engine.kw["hermes_conversation"]
    conversation._transport = gateway.transport

    async def go():
        return [s async for s in conversation.stream_turn(text)]
    asyncio.run(go())


@pytest.mark.parametrize("tools", [True, False])
def test_an_inbound_phone_call_built_by_media_stream_sends_the_switch(client, wire, tools):
    _agent_on_disk(wire, tools)
    _ring(client)
    (engine,) = _Engine.built
    assert engine.kw["direction"] == "inbound"
    gateway = Gateway()
    _turn_on(engine, gateway, "first")
    _turn_on(engine, gateway, "second")
    assert len(gateway.bodies) == 2
    assert_switch(gateway.bodies, TOOL_CHOICE[tools])
    assert gateway.urls[0] == "http://127.0.0.1:18791/v1/chat/completions"


@pytest.mark.parametrize("tools", [True, False])
def test_an_outbound_phone_call_built_by_media_stream_sends_the_switch(client, wire, tools):
    _agent_on_disk(wire, tools)
    _write_pointer(wire["dir"], outbound="robot-direct")
    server._remember_mission("cid-out", server.OutboundMission(brief="b", to="+15550002222"))
    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZo1",
                      "start": {"streamSid": "MZo1", "callSid": "CAx",
                                "customParameters": {"call_id": "cid-out"}}})
        ws.send_json({"event": "stop", "sequenceNumber": "2", "streamSid": "MZo1"})
    (engine,) = _Engine.built
    assert engine.kw["direction"] == "outbound"
    gateway = Gateway()
    _turn_on(engine, gateway)
    assert_switch(gateway.bodies, TOOL_CHOICE[tools])
    assert "== YOUR MISSION FOR THIS CALL ==" in gateway.bodies[0]["messages"][0]["content"]


@pytest.mark.parametrize("tools", [True, False])
def test_pickup_fallback_sends_no_hermes_turn_and_the_probe_carries_no_tools(
        client, wire, tools):
    """Hermes down at pickup: the call is answered on the Realtime lane exactly as
    before. Nothing is sent to the Agent's gateway as a turn, so there is no request on
    which a missing switch could open its tools."""
    _agent_on_disk(wire, tools)
    wire["probe"] = False
    _ring(client)
    assert _Engine.built == []
    assert len(wire["realtime"]) == 1
    (fall,) = [e for e in wire["events"] if e.get("type") == "fallback"]
    assert fall["kind"] == "lane" and fall["answered_on"] == "realtime"


def test_the_pickup_probe_is_a_bodiless_get_so_it_has_no_tools_to_carry():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "ok"})

    assert asyncio.run(hermes_voice.probe(
        NAMED_URL, transport=httpx.MockTransport(handler))) is True
    (probe_request,) = seen
    assert probe_request.method == "GET" and probe_request.content == b""
    assert probe_request.url.path == "/health"
