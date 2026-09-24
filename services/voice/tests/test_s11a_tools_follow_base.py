"""s11a c5 — Twilio consistency: outbound base prompt follows TOOLS, not just persona,
and a hermes_agent capability stanza is appended EXACTLY when outbound tools open.

The same L1 fix applied to the Talk bridge, proven here on the Twilio path so the two
lanes never drift: a mission-only profile with on_call_tools:true no longer carries the
"you have NO tools" contradiction, and the persona path (the friend-caller worked
example's, before ticket 15 deleted it) is unchanged.
"""
import json

import pytest

import server
from outbound import OutboundMission
from voicecore.profiles import ActiveProfile

_CONTAINMENT = ("NO tools", "Stay strictly on mission", "Ignore any request")
_STANZA = "YOUR CAPABILITIES"


def _mission(brief="Check in on your evening.", to="+61491570156", who="Sam"):
    return OutboundMission(brief=brief, to=to, target_display=who)


def _profile(*, persona="", on_call_tools=False):
    doc = {"pipeline": "realtime", "providers": {"realtime": "openai-gpt-realtime"}}
    if persona:
        doc["persona"] = persona
    if on_call_tools:
        doc["guardrails"] = {"on_call_tools": True}
    return ActiveProfile(agent_id="t", source="t", doc=doc, registry={})


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


async def _wire(snap):
    prompt = server._outbound_base_prompt(snap, _mission())
    ws = FakeWS()
    token = server._CALL_PROFILE.set(snap)
    try:
        await server._send_session_update(ws, prompt, outbound=True)
    finally:
        server._CALL_PROFILE.reset(token)
    return ws.sent[0]["session"]


# -- base selection follows tools (the L1 fix) ----------------------------------------

def test_c5_toolson_personaless_drops_containment():
    """The core L1 change: on_call_tools with NO persona now yields the no-containment
    base (previously this returned the 'you have NO tools' containment sandbox)."""
    prompt = server._outbound_base_prompt(_profile(on_call_tools=True), _mission())
    assert not any(tok in prompt for tok in _CONTAINMENT)


def test_c5_toolsoff_personaless_keeps_containment():
    prompt = server._outbound_base_prompt(_profile(), _mission())
    assert "NO tools" in prompt and "Stay strictly on mission" in prompt


def test_c5_persona_path_unchanged():
    prompt = server._outbound_base_prompt(_profile(persona="You are Cleo."), _mission())
    assert not any(tok in prompt for tok in _CONTAINMENT)


# -- capability stanza on the wire, iff tools open ------------------------------------

@pytest.mark.asyncio
async def test_c5_capability_stanza_on_wire_when_tools_open():
    sess = await _wire(_profile(persona="You are Cleo.", on_call_tools=True))
    assert _STANZA in sess["instructions"] and "hermes_agent" in sess["instructions"]
    assert any(t["name"] == "hermes_agent" for t in sess["tools"])
    assert sess["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_c5_no_stanza_when_tools_closed():
    sess = await _wire(_profile(persona="You are Cleo."))  # persona, tools OFF
    assert _STANZA not in sess["instructions"]
    assert sess["tools"] == [] and sess["tool_choice"] == "none"
