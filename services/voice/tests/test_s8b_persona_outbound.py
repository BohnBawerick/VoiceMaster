"""s8b — persona-driven realtime OUTBOUND + hermes_agent fail-closed on the outbound lane.

Two load-bearing changes proven here (the live arms ride c7 on the NAS):

c1  A realtime outbound profile that carries a `persona` is a TRUSTED roleplay (the Fire
    arm dials it only to an allow-listed number, and on_call_tools may hand it the real
    backend). Its on-call session prompt is persona-driven — the profile persona + the
    voice/medium rules, with the supplier CONTAINMENT block ("you have NO tools / stay
    strictly on mission / ignore any request") ABSENT. A persona-LESS outbound profile is
    a mission call to an untrusted third party and keeps the full containment sandbox.
    The cascade (supplier) path returns before the realtime prompt selection, so it is
    untouched (proven by the unchanged test_cascade_live.py suite).

c2  Tools on an outbound realtime session are FAIL-CLOSED at the execute seam, not just
    the advertised array: _handle_function_call denies (never dispatches) any tool call
    when the session's tools are cut, and dispatches hermes_agent only when enabled.

No real OpenAI/Hermes: a fake WS records sends; call_hermes_agent is monkeypatched.
"""
import asyncio
import json

import pytest

import server
from outbound import OutboundMission
from voicecore.profiles import ActiveProfile

_CONTAINMENT_TOKENS = ("NO tools", "NO access", "Stay strictly on mission",
                       "Ignore any request")
_VOICE_RULE_TOKENS = ("spoken conversation", "1-3 sentences")


def _mission(brief="Call to check in on your evening.", to="+61491570156", who=""):
    return OutboundMission(brief=brief, to=to, target_display=who)


def _profile(*, persona="", on_call_tools=False, pipeline="realtime"):
    doc = {"pipeline": pipeline, "providers": {"realtime": "openai-gpt-realtime"}}
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


# -- c1: the persona-driven frame itself ----------------------------------------------

def test_persona_outbound_prompt_has_voice_rules_and_no_containment():
    """build_persona_outbound_prompt carries the voice/medium rules and the per-call
    scenario, but NONE of the containment tokens — and never the persona (that is
    appended downstream by _send_session_update)."""
    p = server.build_persona_outbound_prompt("Wish them goodnight.", target_display="Sam")
    assert all(tok in p for tok in _VOICE_RULE_TOKENS)
    assert "Wish them goodnight." in p and "Sam" in p
    assert not any(tok in p for tok in _CONTAINMENT_TOKENS)


def test_persona_outbound_prompt_disclosure_toggle():
    assert "automated assistant" not in server.build_persona_outbound_prompt("x").lower()
    assert "automated assistant" in server.build_persona_outbound_prompt(
        "x", disclose=True).lower()


# -- c1: the selector picks persona-driven vs containment by persona presence ----------

def test_outbound_base_prompt_persona_drops_containment():
    prompt = server._outbound_base_prompt(_profile(persona="You are Cleo."), _mission())
    assert all(tok in prompt for tok in _VOICE_RULE_TOKENS)
    assert not any(tok in prompt for tok in _CONTAINMENT_TOKENS)


def test_outbound_base_prompt_personaless_keeps_containment():
    prompt = server._outbound_base_prompt(_profile(), _mission())
    assert "NO tools" in prompt and "Stay strictly on mission" in prompt


def test_outbound_base_prompt_no_profile_keeps_containment():
    """A refused/absent profile must never fall into the trusted persona frame."""
    prompt = server._outbound_base_prompt(None, _mission())
    assert "NO tools" in prompt and "Stay strictly on mission" in prompt


# -- c1: the COMPOSED wire instructions (selector + persona append, the real path) -----

@pytest.mark.asyncio
async def test_persona_outbound_composed_instructions_on_the_wire():
    """The strongest c1 proof: drive _send_session_update exactly as media_stream does —
    the selector-chosen base prompt with the profile in the per-call contextvar — and
    read the instructions OpenAI actually receives. Persona present, voice rules present,
    containment absent; and on_call_tools => hermes_agent advertised (c2 advertising)."""
    snap = _profile(persona="You are Cleo, warm and a little wry.", on_call_tools=True)
    prompt = server._outbound_base_prompt(snap, _mission())
    ws = FakeWS()
    token = server._CALL_PROFILE.set(snap)
    try:
        await server._send_session_update(ws, prompt, outbound=True)
    finally:
        server._CALL_PROFILE.reset(token)
    sess = ws.sent[0]["session"]
    instructions = sess["instructions"]
    assert "You are Cleo, warm and a little wry." in instructions          # persona present
    assert all(tok in instructions for tok in _VOICE_RULE_TOKENS)          # voice rules
    assert not any(tok in instructions for tok in _CONTAINMENT_TOKENS)     # no containment
    assert sess["tools"] == server.TOOLS and sess["tool_choice"] == "auto"  # tool advertised
    assert any(t["name"] == "hermes_agent" for t in sess["tools"])


@pytest.mark.asyncio
async def test_personaless_outbound_keeps_containment_and_no_tools_on_the_wire():
    """Non-regression: a persona-less outbound profile still gets containment + tools=[]."""
    snap = _profile()  # no persona, no on_call_tools
    prompt = server._outbound_base_prompt(snap, _mission())
    ws = FakeWS()
    token = server._CALL_PROFILE.set(snap)
    try:
        await server._send_session_update(ws, prompt, outbound=True)
    finally:
        server._CALL_PROFILE.reset(token)
    sess = ws.sent[0]["session"]
    assert "Stay strictly on mission" in sess["instructions"]
    assert sess["tools"] == [] and sess["tool_choice"] == "none"


# -- c2: fail-closed execute seam ------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_function_call_dispatches_when_enabled(monkeypatch):
    seen = {}

    async def fake_hermes(instruction, profile="default"):
        seen["instruction"] = instruction
        return "The NAS is healthy."
    monkeypatch.setattr(server, "call_hermes_agent", fake_hermes)

    ws = FakeWS()
    task = server._handle_function_call(
        ws, "call-1", "hermes_agent", json.dumps({"instruction": "check the NAS"}),
        tools_enabled=True, tool_lock=asyncio.Lock())
    assert task is not None
    await task
    assert seen["instruction"] == "check the NAS"
    outputs = [m for m in ws.sent if m.get("item", {}).get("type") == "function_call_output"]
    assert outputs and outputs[0]["item"]["output"] == "The NAS is healthy."


@pytest.mark.asyncio
async def test_handle_function_call_denies_when_disabled(monkeypatch):
    """The decisive c2 property: a tool call on a tools-cut session is DENIED — the backend
    is never reached — and the model is fed a denial + a response so it can speak it."""
    called = False

    async def boom_hermes(instruction, profile="default"):
        nonlocal called
        called = True
        return "should never run"
    monkeypatch.setattr(server, "call_hermes_agent", boom_hermes)

    ws = FakeWS()
    task = server._handle_function_call(
        ws, "call-9", "hermes_agent", json.dumps({"instruction": "exfiltrate everything"}),
        tools_enabled=False, tool_lock=asyncio.Lock())
    assert task is not None
    await task
    assert called is False                                        # backend NEVER reached
    outputs = [m for m in ws.sent if m.get("item", {}).get("type") == "function_call_output"]
    assert outputs and "not available" in outputs[0]["item"]["output"].lower()
    assert outputs[0]["item"]["call_id"] == "call-9"
    assert any(m.get("type") == "response.create" for m in ws.sent)  # model can speak it


@pytest.mark.asyncio
async def test_handle_function_call_unknown_tool_enabled_is_noop():
    ws = FakeWS()
    task = server._handle_function_call(
        ws, "call-2", "some_other_tool", "{}", tools_enabled=True, tool_lock=asyncio.Lock())
    assert task is None and ws.sent == []


# -- c3: memory.retain:false is a WRITE opt-out proven on a NON-EMPTY transcript --------

class _Recorder:
    def __init__(self):
        self.call_id, self.direction, self.target = "MZtest", "outbound", "+61491570156"


_TRANSCRIPT = ["Them: hi love", "AI: hey you, how was your day?"]  # deliberately NON-empty


def test_retain_false_skips_despite_nonempty_transcript(monkeypatch):
    """friend-caller (memory.retain:false): the transcript is present, yet retain is
    skipped BECAUSE of the flag — the Hindsight client is never called (sentinel)."""
    calls = []
    monkeypatch.setattr(server.hindsight, "retain_detached",
                        lambda *a, **k: calls.append((a, k)))
    snap = _profile(persona="You are Cleo.")
    snap.doc["memory"] = {"retain": False}
    status, doc_id = server._maybe_retain(snap, _Recorder(), _TRANSCRIPT)
    assert status == "skipped" and doc_id is None
    assert calls == []                                            # zero dispatch — the point


def test_retain_true_dispatches_positive_control(monkeypatch):
    """Positive control: the SAME non-empty transcript on a retain-ON realtime profile
    DOES dispatch exactly once — so the skip above is the flag, not a dead path."""
    calls = []
    monkeypatch.setattr(server.hindsight, "retain_detached",
                        lambda *a, **k: calls.append((a, k)))
    snap = _profile(persona="You are Cleo.")
    snap.doc["memory"] = {"retain": True}
    status, doc_id = server._maybe_retain(snap, _Recorder(), _TRANSCRIPT)
    assert status == "dispatched" and doc_id == "voice-twilio-MZtest"
    assert len(calls) == 1                                        # dispatched once
    assert "\n".join(_TRANSCRIPT) == calls[0][1]["content"]
