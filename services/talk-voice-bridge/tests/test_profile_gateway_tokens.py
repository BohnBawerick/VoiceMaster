"""Per-profile gateway bearers on the Talk bridge (HERMES_PROFILE_GATEWAY_TOKENS).

Both Talk lanes send the Agent's own token to that Agent's gateway when one is configured,
else the shared HERMES_GATEWAY_TOKEN: the direct lane's conversation, the in-call
hermes_agent tool on either lane, and the post-call summary. The bearer this bridge
requires of its own callers stays the shared token.
"""
import dataclasses
import json

import pytest

import cascade_bridge
import config as config_mod
import hermes
import outbound as outbound_mod
import realtime_bridge
from approval import ApprovalStore
from voicecore import hermes_gateway
from voicecore import profiles
from voicecore import summary as call_summary
from test_direct_lane_talk import GATEWAY, talk_env  # noqa: F401  (fixture)
from test_s15b_lane_traversal import _QuietDeepgram, _QuietWire

SHARED = "shared-token"
REAL_CALL = [
    "AI: Hi, this is the clinic calling about Thursday.",
    "Them: Yes, I need to move it to Friday afternoon if that works for you.",
]


def _mission():
    return outbound_mod.OutboundMission(brief="b", report_channel="talk",
                                        report_address="R", target_display="Alex")


def _direct_bridge(monkeypatch, tokens):
    monkeypatch.setenv("HERMES_GATEWAY_TOKEN", SHARED)
    if tokens is None:
        monkeypatch.delenv("HERMES_PROFILE_GATEWAY_TOKENS", raising=False)
    else:
        monkeypatch.setenv("HERMES_PROFILE_GATEWAY_TOKENS", tokens)
    tool_tokens: list = []

    async def call(instruction, *, gateway_url, token, timeout):
        tool_tokens.append((gateway_url, token))
        return "done"

    monkeypatch.setattr(cascade_bridge.hermes, "call_hermes_agent", call)
    profile = profiles.load_effective_profile("inbound", outlet=profiles.OUTLET_TALK)
    bridge = cascade_bridge.CascadeBridge(config_mod.load(), profile, _mission(),
                                          token="TOK", stt=_QuietDeepgram(),
                                          wire=_QuietWire())
    return bridge, tool_tokens


async def _summary_token(monkeypatch, summariser):
    asked = {}

    async def spy(prompt, *, gateway_url, token, **kw):
        asked.update(gateway_url=gateway_url, token=token)
        return "a summary"

    monkeypatch.setattr(hermes_gateway, "ask_chat", spy)
    assert (await summariser(REAL_CALL))[1] == call_summary.STATE_WRITTEN
    return asked["gateway_url"], asked["token"]


@pytest.mark.asyncio
async def test_the_direct_lane_sends_the_agents_own_token(talk_env, monkeypatch):  # noqa: F811
    bridge, tool_tokens = _direct_bridge(monkeypatch, "vega=vega-own")
    assert bridge._session._hermes._token == "vega-own"
    assert await bridge._session._hermes_call("check the NAS") == "done"
    assert tool_tokens == [(GATEWAY, "vega-own")]
    assert await _summary_token(monkeypatch, bridge._session._summariser) == \
        (GATEWAY, "vega-own")


@pytest.mark.asyncio
async def test_a_profile_without_an_entry_gets_the_shared_token(talk_env, monkeypatch):  # noqa: F811
    bridge, tool_tokens = _direct_bridge(monkeypatch, "somebody-else=other-own")
    assert bridge._session._hermes._token == SHARED
    await bridge._session._hermes_call("check the NAS")
    assert tool_tokens == [(GATEWAY, SHARED)]
    assert await _summary_token(monkeypatch, bridge._session._summariser) == \
        (GATEWAY, SHARED)


@pytest.mark.asyncio
async def test_with_no_map_the_direct_lane_is_as_before(talk_env, monkeypatch):  # noqa: F811
    bridge, tool_tokens = _direct_bridge(monkeypatch, None)
    assert bridge._session._hermes._token == SHARED


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _realtime(monkeypatch, profile_name, tokens):
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS",
                       "scout=http://scout.test,vega=http://vega.test")
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_TOKENS", tokens)
    cfg = dataclasses.replace(config_mod.load(), hermes_gateway_token=SHARED,
                              filler_debounce_ms=5000)
    profile = None
    if profile_name is not None:
        profile = type("P", (), {"agent_id": "a", "doc": {"hermes_profile": profile_name}})()
    return realtime_bridge.RealtimeBridge(cfg, "PROMPT", ApprovalStore(),
                                          token_ctx={"token": "ROOM", "caller": "Alex"},
                                          profile=profile)


@pytest.mark.asyncio
@pytest.mark.parametrize("name, url, token", [
    ("scout", "http://scout.test", "scout-own"),
    ("vega", "http://vega.test", SHARED),
])
async def test_the_realtime_tool_and_summary_send_the_agents_token(
        monkeypatch, name, url, token):
    sent: list = []

    async def call(instruction, *, gateway_url, token, timeout):
        sent.append((gateway_url, token))
        return "done"

    monkeypatch.setattr(hermes, "call_hermes_agent", call)
    bridge = _realtime(monkeypatch, name, "scout=scout-own")
    await bridge._handle_tool(_FakeWS(), {
        "type": "response.function_call_arguments.done", "name": "hermes_agent",
        "call_id": "c1", "arguments": json.dumps({"instruction": "do it"})})
    assert sent == [(url, token)]
    assert await _summary_token(monkeypatch, bridge._summariser()) == (url, token)


@pytest.mark.asyncio
async def test_an_unknown_profile_with_a_token_entry_is_still_refused(monkeypatch):
    sent: list = []

    async def call(instruction, **kw):
        sent.append(kw)
        return "done"

    monkeypatch.setattr(hermes, "call_hermes_agent", call)
    bridge = _realtime(monkeypatch, "ghost", "ghost=ghost-own")
    ws = _FakeWS()
    await bridge._handle_tool(ws, {
        "type": "response.function_call_arguments.done", "name": "hermes_agent",
        "call_id": "c1", "arguments": json.dumps({"instruction": "do it"})})
    assert sent == []
    outputs = [m["item"]["output"] for m in ws.sent
               if m.get("item", {}).get("type") == "function_call_output"]
    assert outputs and "can't reach the backend" in outputs[0]
