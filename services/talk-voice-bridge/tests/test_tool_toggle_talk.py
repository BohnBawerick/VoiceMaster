"""The Agent's tools setting reaches Hermes on the wire, on the Talk Outlet.

The phone half is services/voice/tests/test_tool_toggle.py; the two Outlets share
``hermes_conversation_for`` and ``HermesConversation``, and this file holds the Talk
bridge to it. Before the fix a tools-off and a tools-on Agent sent identical Hermes
requests (no ``tool_choice``) and an outbound tools-off Agent was refused.

The bridges here are the REAL ``CallSession`` / ``CascadeBridge`` / ``DirectLaneBridge``
with their real constructors; only ``run()`` and the network edge are replaced. The
Agents are on-disk YAML through the real loader. The Hermes gateway is an inert
MockTransport that records the serialised request and executes nothing.
"""
import asyncio
import json

import httpx
import pytest
import yaml

import cascade_bridge
import config as config_mod
import outbound as outbound_mod
import session as session_mod
from approval import ApprovalStore
from test_direct_lane_talk import (  # noqa: F401  (fixtures)
    GATEWAY, _agent, _lanes, _start, talk_env)
from test_s15b_lane_traversal import FakeBrowser

TOOL_CHOICE = {True: "auto", False: "none", None: "auto"}
SETTINGS = [pytest.param(True, id="tools-on"), pytest.param(False, id="tools-off"),
            pytest.param(None, id="tools-unset")]
PROFILES = [pytest.param("vega", id="named-profile"),
            pytest.param("default", id="default-profile")]


class Gateway:
    """An inert Hermes: records every request body, executes nothing."""

    def __init__(self, *, fail_first=0):
        self.bodies: list = []
        self.urls: list = []
        self.fail_first = fail_first
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request):
        self.bodies.append(json.loads(request.content))
        self.urls.append(str(request.url))
        if self.fail_first:
            self.fail_first -= 1
            return httpx.Response(503, json={"error": "down"})
        frames = [
            'data: {"choices":[{"delta":{"content":"Right, I can help with that today. "},'
            '"finish_reason":null}]}\n\n',
            'data: {"choices":[{"delta":{"content":"Anything else?"},'
            '"finish_reason":null}]}\n\n',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            "data: [DONE]\n\n"]
        return httpx.Response(200, content="".join(frames).encode(),
                              headers={"Content-Type": "text/event-stream"})


def assert_switch(bodies, expected):
    """Every serialised request carries ``tool_choice`` == expected, and never ``tools``."""
    assert bodies, "no request reached the gateway"
    for i, body in enumerate(bodies):
        assert "tool_choice" in body, f"request {i} has no tool_choice: {sorted(body)}"
        assert body["tool_choice"] == expected, (i, body["tool_choice"])
        assert "tools" not in body


DEFAULT_GATEWAY = "http://hermes.test:18789"


def _write_agent(talk_env, tools, profile):
    doc = _agent()
    if profile == "default":
        del doc["hermes_profile"]
    if tools is None:
        doc.pop("guardrails", None)
    else:
        doc["guardrails"] = {"on_call_tools": tools}
    (talk_env["dir"] / "agents" / "direct.yaml").write_text(yaml.safe_dump(doc))


def _turns(conversation, gateway, *texts):
    conversation._transport = gateway.transport

    async def go():
        for text in texts:
            _ = [s async for s in conversation.stream_turn(text)]
    asyncio.run(go())


# ----------------------------------------------------- inbound, the real start() path --

@pytest.mark.asyncio
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("tools", SETTINGS)
async def test_an_inbound_talk_call_sends_the_switch_on_every_turn(
        talk_env, monkeypatch, tools, profile):
    _write_agent(talk_env, tools, profile)
    monkeypatch.setenv("HERMES_GATEWAY_URL", DEFAULT_GATEWAY)
    cs, ok, bridge, ran, probed = await _start("owner", monkeypatch)
    assert ok is True and isinstance(bridge, cascade_bridge.DirectLaneBridge)
    ((lane, inner),) = ran
    assert lane == "cascade" and inner._session._direction == "inbound"
    gateway = Gateway()
    await asyncio.to_thread(_turns, inner._session._hermes, gateway, "first", "second")
    assert len(gateway.bodies) == 2
    assert_switch(gateway.bodies, TOOL_CHOICE[tools])
    # Profile selection and per-call isolation are unchanged.
    expected = GATEWAY if profile == "vega" else DEFAULT_GATEWAY
    assert gateway.urls[0] == f"{expected}/v1/chat/completions"
    assert [m for m in gateway.bodies[1]["messages"] if m["role"] != "system"] == [
        {"role": "user", "content": "second"}]
    system = gateway.bodies[0]["messages"][0]["content"]
    assert ("no tools on this call" in system) is (tools is False)


@pytest.mark.asyncio
@pytest.mark.parametrize("tools", SETTINGS)
async def test_a_retry_on_talk_keeps_the_switch(talk_env, monkeypatch, tools):
    from voicecore import cascade_live
    _write_agent(talk_env, tools, "vega")
    monkeypatch.setattr(cascade_live, "HERMES_RETRY_BACKOFF_S", (0.0,))
    cs, ok, bridge, ran, probed = await _start("owner", monkeypatch)
    ((_, inner),) = ran
    gateway = Gateway(fail_first=2)
    session = inner._session
    session._hermes._transport = gateway.transport
    session.messages.append({"role": "user", "content": "book it"})
    session.transcript.append("Them: book it")
    await session._agent_turn()
    assert len(gateway.bodies) == 3
    assert_switch(gateway.bodies, TOOL_CHOICE[tools])
    assert gateway.bodies[0] == gateway.bodies[1] == gateway.bodies[2]


# --------------------------------------------------------- outbound, the real start() path --

@pytest.mark.asyncio
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("tools", SETTINGS)
async def test_an_outbound_talk_call_sends_the_switch_and_tools_off_is_not_refused(
        talk_env, monkeypatch, tools, profile):
    """Outbound tools-off used to be refused at activation ('cannot be sandboxed'), so
    the call never reached the bridge. It is a valid call now."""
    _write_agent(talk_env, tools, profile)
    (talk_env["dir"] / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {"talk": {"inbound": None, "outbound": "robot-direct"}}}))
    ran = _lanes(monkeypatch)
    cs = session_mod.CallSession(config_mod.load(), FakeBrowser(), ApprovalStore())
    mission = outbound_mod.OutboundMission(brief="Confirm the Friday booking.",
                                           report_channel="talk", report_address="R",
                                           target_display="Alex")
    assert await cs.start("TOKout", "owner", "alex", "Alex", mission=mission) is True
    assert cs.last_start_failure is None or cs.last_start_failure == {}
    if cs._run_task is not None:
        await cs._run_task
    ((lane, inner),) = ran
    assert lane == "cascade" and inner._session._direction == "outbound"
    gateway = Gateway()
    await asyncio.to_thread(_turns, inner._session._hermes, gateway, "hello")
    assert_switch(gateway.bodies, TOOL_CHOICE[tools])
    assert "== YOUR MISSION FOR THIS CALL ==" in gateway.bodies[0]["messages"][0]["content"]


# --------------------------------------------- owner-only and pickup fallback unchanged --

@pytest.mark.asyncio
@pytest.mark.parametrize("tools", SETTINGS)
async def test_a_guest_still_never_reaches_the_direct_lane_whatever_the_setting(
        talk_env, monkeypatch, tools):
    _write_agent(talk_env, tools, "vega")
    cs, ok, bridge, ran, probed = await _start("guest", monkeypatch)
    assert ok is True and not isinstance(bridge, cascade_bridge.DirectLaneBridge)
    assert probed == [] and [lane for lane, _ in ran] == ["realtime"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tools", SETTINGS)
async def test_hermes_down_at_pickup_is_still_the_realtime_fallback(
        talk_env, monkeypatch, tools):
    _write_agent(talk_env, tools, "vega")
    cs, ok, bridge, ran, probed = await _start("owner", monkeypatch, answer=False)
    assert ok is True and bridge.fell_back is True
    ((lane, inner),) = ran
    assert lane == "realtime"
    (fall,) = [e for e in talk_env["events"] if e.get("type") == "fallback"]
    assert fall["kind"] == "lane" and fall["answered_on"] == "realtime"
