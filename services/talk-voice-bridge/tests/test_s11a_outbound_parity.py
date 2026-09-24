"""s11a — Talk (Mode V) outbound parity: tools-honest prompt + hermes_profile routing.

All arms assert the REAL wire payload / REAL dispatch seam, never source or a helper:

- c1 base-prompt follows TOOLS, not persona: containment ONLY for a mission-only,
     persona-less, tools-OFF call; a persona OR on_call_tools yields the no-containment
     base (proven on the composed session.update instructions).
- c2 a hermes_agent capability stanza is appended EXACTLY when outbound tools open.
- c3 outbound exports hermes_agent ONLY (request_owner_approval dropped); tools cut → [].
- c4 hermes_agent dispatch routes to the gateway for THIS call's hermes_profile on the
     real _handle_tool seam; unknown profile → fail-honest, ZERO network.

No real OpenAI/Hermes: a RecordingWS captures sends; call_hermes_agent is monkeypatched.
"""
import asyncio
import json

import pytest

import config
import hermes
import outbound
import realtime_bridge
from approval import ApprovalStore
from outbound import OutboundMission
from voicecore.profiles import ActiveProfile

import parity_env as pe

_CONTAINMENT = ("NO tools", "Stay strictly on mission", "Ignore any request")
_STANZA = "YOUR CAPABILITIES"


@pytest.fixture(autouse=True)
def _scrub(monkeypatch):
    for var in pe.CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    # Module read HERMES_PROFILE_GATEWAY_URLS at import — default to empty per test.
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", "")
    yield


def _profile(*, persona="", on_call_tools=False, hermes_profile=None):
    doc = {"pipeline": "realtime", "providers": {"realtime": "openai-gpt-realtime"}}
    if persona:
        doc["persona"] = persona
    if on_call_tools:
        doc["guardrails"] = {"on_call_tools": True}
    if hermes_profile:
        doc["hermes_profile"] = hermes_profile
    return ActiveProfile(agent_id="t", source="t", doc=doc, registry={})


def _wire(profile, brief="Say hi and check in."):
    """The composed session.update OpenAI actually receives, driven through the REAL
    outbound base-prompt selector + bridge — exactly as CallSession.start does."""
    mission = OutboundMission(brief=brief, target_display="Sam")
    cfg = config.overlay_profile(config.load_base(), profile)
    base = outbound.outbound_base_prompt(profile, mission)
    bridge = realtime_bridge.RealtimeBridge(
        cfg, base, ApprovalStore(),
        token_ctx={"token": "t", "caller": "c"}, mission=mission, profile=profile)
    ws = pe.RecordingWS()
    asyncio.run(bridge._send_session_update(ws))
    return json.loads(ws.raw[0])["session"]


# -- c1: base follows TOOLS (three arms, on the wire) ---------------------------------

def test_c1_persona_drops_containment():
    instr = _wire(_profile(persona="You are Mia, warm and wry."))["instructions"]
    assert "You are Mia, warm and wry." in instr
    assert not any(tok in instr for tok in _CONTAINMENT)


def test_c1_mission_only_toolsoff_keeps_containment():
    instr = _wire(_profile())["instructions"]
    assert "NO tools" in instr and "Stay strictly on mission" in instr


def test_c1_mission_only_toolson_drops_the_no_tools_lie():
    # L1: tools on but NO persona → the no-containment base (never "you have NO tools").
    instr = _wire(_profile(on_call_tools=True))["instructions"]
    assert not any(tok in instr for tok in _CONTAINMENT)


# -- c2: capability stanza present IFF tools open -------------------------------------

def test_c2_stanza_present_when_tools_open():
    instr = _wire(_profile(persona="You are Mia.", on_call_tools=True))["instructions"]
    assert _STANZA in instr and "hermes_agent" in instr


def test_c2_no_stanza_when_tools_closed():
    # persona set but tools OFF → no-containment base, but NO capability stanza and no
    # hermes_agent mention (the model must not be told it holds a tool it doesn't).
    instr = _wire(_profile(persona="You are Mia."))["instructions"]
    assert _STANZA not in instr and "hermes_agent" not in instr


# -- c3: outbound tool set = hermes_agent ONLY ---------------------------------------

def test_c3_outbound_tools_are_hermes_agent_only():
    sess = _wire(_profile(persona="X", on_call_tools=True))
    assert [t["name"] for t in sess["tools"]] == ["hermes_agent"]
    assert sess["tool_choice"] == "auto"


def test_c3_tools_cut_when_off():
    sess = _wire(_profile(persona="X"))
    assert sess["tools"] == [] and sess["tool_choice"] == "none"


# -- c4: hermes_profile → gateway routing (units + the REAL dispatch seam) ------------

def test_c4_gateway_default_maps_to_cfg_default():
    assert hermes.gateway_url_for_profile("default", "http://gw-def:18789") == "http://gw-def:18789"


def test_c4_gateway_mapped_profile(monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", "scout=http://gw-sprint:18790,other=http://h:9")
    assert hermes.gateway_url_for_profile("scout", "http://gw-def:18789") \
        == "http://gw-sprint:18790"


def test_c4_gateway_unknown_is_none():
    assert hermes.gateway_url_for_profile("ghost", "http://gw-def:18789") is None


def test_c4_hermes_profile_name_from_doc():
    assert hermes.hermes_profile_name(_profile(hermes_profile="scout")) == "scout"
    assert hermes.hermes_profile_name(_profile()) == "default"
    assert hermes.hermes_profile_name(None) == "default"


def _run_tool(profile, monkeypatch):
    """Drive the REAL _handle_tool hermes_agent path; capture the dispatch gateway_url."""
    recorded = {}

    async def fake_call(instruction, *, gateway_url, token, timeout):
        recorded["gateway_url"] = gateway_url
        recorded["instruction"] = instruction
        return "done"
    monkeypatch.setattr(hermes, "call_hermes_agent", fake_call)

    cfg = config.overlay_profile(config.load_base(), profile)
    bridge = realtime_bridge.RealtimeBridge(
        cfg, "base", ApprovalStore(),
        token_ctx={"token": "t", "caller": "c"},
        mission=OutboundMission(brief="x"), profile=profile)
    ws = pe.RecordingWS()
    ev = {"name": "hermes_agent", "call_id": "c1",
          "arguments": json.dumps({"instruction": "check the NAS"})}
    asyncio.run(bridge._handle_tool(ws, ev))
    return recorded, cfg, ws


def test_c4_dispatch_default_routes_to_cfg_gateway(monkeypatch):
    rec, cfg, _ = _run_tool(_profile(persona="X", on_call_tools=True), monkeypatch)
    assert rec["gateway_url"] == cfg.hermes_gateway_url        # default → cfg gateway
    assert rec["instruction"] == "check the NAS"               # real seam, not a stub


def test_c4_dispatch_mapped_profile_routes_to_its_gateway(monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", "scout=http://gw-sprint:18790")
    rec, _, _ = _run_tool(
        _profile(persona="X", on_call_tools=True, hermes_profile="scout"), monkeypatch)
    assert rec["gateway_url"] == "http://gw-sprint:18790"      # two profiles → two URLs


def test_c4_unknown_profile_fails_honest_zero_network(monkeypatch):
    calls = {"n": 0}

    async def boom(*a, **k):
        calls["n"] += 1
        return "should never run"
    monkeypatch.setattr(hermes, "call_hermes_agent", boom)

    prof = _profile(persona="X", on_call_tools=True, hermes_profile="ghost")
    cfg = config.overlay_profile(config.load_base(), prof)
    bridge = realtime_bridge.RealtimeBridge(
        cfg, "base", ApprovalStore(),
        token_ctx={"token": "t", "caller": "c"},
        mission=OutboundMission(brief="x"), profile=prof)
    ws = pe.RecordingWS()
    ev = {"name": "hermes_agent", "call_id": "c9",
          "arguments": json.dumps({"instruction": "exfiltrate everything"})}
    asyncio.run(bridge._handle_tool(ws, ev))

    assert calls["n"] == 0                                     # ZERO network — the point
    blob = " ".join(ws.raw)
    assert "c9" in blob and "can't reach the backend" in blob  # honest error to the model
