"""Ticket 06 on the Talk Outlet - both of its lanes summarise, from their own Agent.

The rules and the guard live in `voicecore/summary.py` and are exercised in depth by the
phone suite; what this suite has to prove is that the Talk lanes ARE wired to them - a
lane that quietly passes no summariser summarises nothing, silently, forever - and that
the gateway it asks is the one THIS call's Agent runs on rather than the bridge's
default.
"""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
import respx

from voicecore import eventlog
from voicecore import hermes_gateway
from voicecore import hindsight
from voicecore import summary as call_summary
import cascade_bridge
import outbound
import realtime_bridge
from approval import ApprovalStore
from config import load
from outbound import OutboundMission

REAL_CALL = [
    "Them: hi, I'm ringing to move Rufus's appointment from Tuesday to Thursday",
    "AI: no problem, Thursday at four is free - I've moved it and sent a confirmation",
    "Them: perfect, thank you",
]


def _gateway_reply(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def _cfg(**over):
    return replace(load(), openai_api_key="k", hindsight_url="http://hs:8888",
                   hindsight_bank="voice", retain_enabled=True,
                   hermes_gateway_url="http://talk-default.test",
                   hermes_gateway_token="talk-token", **over)


async def _run_bridge(monkeypatch, bridge):
    """Drive one Talk realtime call whose session never opens, to its teardown."""
    def boom(*a, **kw):
        raise RuntimeError("openai is down")
    monkeypatch.setattr(realtime_bridge.websockets, "connect", boom)
    monkeypatch.setattr(outbound, "deliver_transcript", lambda *a, **kw: asyncio.sleep(0))
    monkeypatch.setattr(eventlog, "append_event", lambda obj, path=None: None)
    await bridge.run()


@pytest.mark.asyncio
@respx.mock
async def test_the_talk_realtime_lane_summarises_from_this_calls_own_agent(monkeypatch):
    """The summary is written by the Agent on the call, and stored with the call."""
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", "")
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", "vet-caller=http://vet-caller.test")
    theirs = respx.post("http://vet-caller.test/v1/chat/completions").mock(
        return_value=_gateway_reply("Moved Rufus's appointment to Thursday at four."))
    default = respx.post("http://talk-default.test/v1/chat/completions").mock(
        return_value=_gateway_reply("The wrong Agent answered."))

    dispatched = []

    def capture(url, bank, **kw):
        dispatched.append(kw)
        prepare = kw.get("prepare")
        if prepare is not None:
            asyncio.get_running_loop().create_task(prepare())
        return True

    monkeypatch.setattr(hindsight, "retain_detached", capture)

    profile = SimpleNamespace(agent_id="vet-caller",
                              doc={"hermes_profile": "vet-caller"})
    bridge = realtime_bridge.RealtimeBridge(
        _cfg(), "PROMPT", ApprovalStore(),
        token_ctx={"token": "ROOM-06", "caller": "Alex"},
        mission=OutboundMission(brief="Move the appointment.", target_display="The vet"),
        profile=profile)
    bridge._transcript = list(REAL_CALL)

    await _run_bridge(monkeypatch, bridge)
    for _ in range(400):
        await asyncio.sleep(0.01)
        if theirs.called:
            break

    assert len(dispatched) == 1
    assert dispatched[0].get("prepare") is not None, "this lane never asks for a summary"
    assert theirs.called and not default.called, "the wrong Agent was asked"
    asked = json.loads(theirs.calls.last.request.read())["messages"][0]["content"]
    assert REAL_CALL[0] in asked
    assert theirs.calls.last.request.headers["Authorization"] == "Bearer talk-token"

    meta = dispatched[0]["metadata"]
    assert meta["summary"] == "Moved Rufus's appointment to Thursday at four."
    assert meta["summary_state"] == call_summary.STATE_WRITTEN
    # ...and nothing ticket 05 or 07 writes was disturbed by it.
    assert meta["outlet"] == "talk" and meta["agent"] == "vet-caller"
    assert meta["mission"] == "Move the appointment."


@pytest.mark.asyncio
@respx.mock
async def test_a_talk_call_with_nothing_on_it_gets_no_summary(monkeypatch):
    """Bar 2 on this Outlet: the gateway is not even asked, and nothing is invented."""
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", "")
    gateway = respx.post("http://talk-default.test/v1/chat/completions").mock(
        return_value=_gateway_reply("The caller said nothing at all."))

    dispatched = []

    async def run_prepare(url, bank, **kw):
        dispatched.append(kw)
        if kw.get("prepare") is not None:
            await kw["prepare"]()
        return True

    monkeypatch.setattr(hindsight, "retain_detached",
                        lambda url, bank, **kw: asyncio.get_running_loop().create_task(
                            run_prepare(url, bank, **kw)) and True)

    bridge = realtime_bridge.RealtimeBridge(
        _cfg(), "PROMPT", ApprovalStore(),
        token_ctx={"token": "ROOM-07", "caller": "Alex"}, mission=None, profile=None)
    # A greeting a real answered Talk line produces, not one trimmed to sit under a
    # threshold: the caller-side floor is what has to refuse this, not the length of
    # what the Agent said.
    bridge._transcript = ["AI: Hi, you've reached Jamie's assistant, how can I help?",
                          "Them: oh"]

    await _run_bridge(monkeypatch, bridge)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if dispatched and "summary_state" in dispatched[0]["metadata"]:
            break

    assert not gateway.called, "the Agent was asked to summarise a call with nothing on it"
    meta = dispatched[0]["metadata"]
    assert "summary" not in meta
    assert meta["summary_state"] == call_summary.STATE_NOTHING


@pytest.mark.asyncio
async def test_a_broken_summariser_costs_the_talk_call_nothing_but_its_summary(monkeypatch):
    """Bar 1 on this Outlet, asserted on the archive row and the call record."""
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", "")

    async def exploding(prompt):
        raise RuntimeError("the gateway is on fire")

    monkeypatch.setattr(hermes_gateway, "ask_chat",
                        lambda prompt, **kw: exploding(prompt))

    dispatched = []

    def capture(url, bank, **kw):
        dispatched.append(kw)
        if kw.get("prepare") is not None:
            asyncio.get_running_loop().create_task(kw["prepare"]())
        return True

    monkeypatch.setattr(hindsight, "retain_detached", capture)
    events = []
    monkeypatch.setattr(eventlog, "append_event", lambda obj, path=None: events.append(obj))

    profile = SimpleNamespace(agent_id="talk-answerer", doc={})
    bridge = realtime_bridge.RealtimeBridge(
        _cfg(), "PROMPT", ApprovalStore(),
        token_ctx={"token": "ROOM-08", "caller": "Alex"}, mission=None, profile=profile)
    bridge._transcript = list(REAL_CALL)

    def boom(*a, **kw):
        raise RuntimeError("openai is down")
    monkeypatch.setattr(realtime_bridge.websockets, "connect", boom)
    monkeypatch.setattr(outbound, "deliver_transcript", lambda *a, **kw: asyncio.sleep(0))
    await bridge.run()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if "summary_state" in dispatched[0]["metadata"]:
            break

    kw = dispatched[0]
    assert kw["content"] == "\n".join(REAL_CALL), "the transcript was damaged"
    meta = kw["metadata"]
    assert "summary" not in meta
    assert meta["summary_state"] == call_summary.STATE_UNAVAILABLE
    assert meta["outlet"] == "talk" and meta["agent"] == "talk-answerer"
    calls = [e for e in events if e.get("type") == "call"]
    assert len(calls) == 1, "the call record was lost with the summary"


def test_the_talk_cascade_lane_hands_the_engine_a_summariser(monkeypatch):
    """The other Talk lane, built by its REAL constructor (s15d's lesson).

    A fake session class here would agree with the code instead of with reality, which is
    exactly how the cascade lane once shipped a constructor that had never run.
    """
    import config
    from voicecore import profiles

    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", "vet-caller=http://vet-caller.test")
    env = {"DEEPGRAM_API_KEY": "dg-test", "OPENAI_API_KEY": "sk-test",
           "ELEVENLABS_API_KEY": "el-test"}
    doc = {"id": "vet-caller", "hermes_profile": "vet-caller", "pipeline": "cascade",
           "providers": {"stt": "deepgram", "llm": "gpt-4.1", "tts": "elevenlabs"},
           "knobs": {"vad": {"silence_ms": 250}}}
    profile = profiles.ActiveProfile(
        agent_id="vet-caller", source="ticket-06-test", doc=doc,
        registry=profiles.load_registry(profiles.config_dir(env)))
    cfg = replace(config.load_base(), hermes_gateway_url="http://talk-default.test",
                  hermes_gateway_token="talk-token")

    bridge = cascade_bridge.CascadeBridge(
        cfg, profile, OutboundMission(brief="Move the appointment.",
                                      target_display="The vet"),
        env=env, token="ROOM-09")

    summariser = bridge._session._summariser
    assert summariser is not None, "the Talk cascade lane never asks for a summary"
    # ...and it asks THIS Agent's backend, not the bridge's default one.
    asked = {}

    async def spy(prompt, *, gateway_url, token, timeout_s, **kw):
        asked.update(gateway_url=gateway_url, token=token, timeout_s=timeout_s)
        return "a summary"

    monkeypatch.setattr(hermes_gateway, "ask_chat", spy)
    text, state = asyncio.run(summariser(REAL_CALL))
    assert (text, state) == ("a summary", call_summary.STATE_WRITTEN)
    assert asked["gateway_url"] == "http://vet-caller.test"
    assert asked["token"] == "talk-token"
    assert asked["timeout_s"] == call_summary.DEFAULT_TIMEOUT_S
