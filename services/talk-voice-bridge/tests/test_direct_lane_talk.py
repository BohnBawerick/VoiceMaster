"""VC24 on the Talk bridge: the direct Hermes lane answers an inbound Talk call for the
OWNER, behind a pickup check, and a guest never reaches it.

Same rule as test_s15b_lane_traversal: the profile is on-disk YAML through the real
loader, and the bridges are the REAL classes with their real ``__init__`` running. Only
``run()`` / ``stop()`` of the two inner bridges are patched, as methods. Replacing a
class is what hid ``mission.to`` until the first live dial.
"""
import asyncio
import json

import pytest
import yaml

import cascade_bridge
import config as config_mod
import hermes
import outbound as outbound_mod
import session as session_mod
from approval import ApprovalStore
from realtime_bridge import RealtimeBridge
from voicecore import lkg
from voicecore import profiles
from test_s15b_lane_traversal import FakeBrowser, _QuietDeepgram, _QuietWire

DIRECT = {"stt": "deepgram", "llm": "hermes-agent", "tts": "elevenlabs"}
BOTH = {"talk": frozenset({"outbound", "inbound"})}
GATEWAY = "http://127.0.0.1:18791"


def _agent(aid="robot-direct", **over):
    doc = {"id": aid, "pipeline": "cascade", "providers": dict(DIRECT),
           "hermes_profile": "vega", "knobs": {"voice": "el-voice-1"}}
    doc.update(over)
    return doc


@pytest.fixture
def talk_env(tmp_path, monkeypatch):
    d = tmp_path / "vcfg"
    (d / "agents").mkdir(parents=True)
    (d / "agents" / "direct.yaml").write_text(yaml.safe_dump(_agent()))
    (d / "gateways").mkdir()
    (d / "gateways" / "gateways.json").write_text(json.dumps(
        {"profiles": {"vega": {"status": "ok", "gateway_url": GATEWAY}}}))
    (d / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {"talk": {"inbound": "robot-direct", "outbound": None}}}))
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(d))
    monkeypatch.delenv("VOICE_AGENT", raising=False)
    monkeypatch.delenv("HERMES_PROFILE_GATEWAY_URLS", raising=False)
    monkeypatch.delenv(profiles.ENV_HERMES_DIRECT, raising=False)
    for key in ("DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(key, "k")
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", dict(BOTH))
    events: list = []
    monkeypatch.setattr(lkg.eventlog, "append_event",
                        lambda obj, *a, **k: events.append(obj))
    return {"dir": d, "events": events}


def _lanes(monkeypatch):
    """Patch ONLY run()/stop() on the two inner bridges; record which one ran."""
    ran: list = []

    async def cascade_run(self):
        ran.append(("cascade", self))

    async def realtime_run(self):
        ran.append(("realtime", self))

    async def stop(self):
        return None

    monkeypatch.setattr(cascade_bridge.CascadeBridge, "run", cascade_run)
    monkeypatch.setattr(cascade_bridge.CascadeBridge, "stop", stop)
    monkeypatch.setattr(RealtimeBridge, "run", realtime_run)
    monkeypatch.setattr(RealtimeBridge, "stop", stop)
    return ran


def _probe(monkeypatch, answer):
    probed: list = []

    async def probe(url, **kw):
        probed.append(url)
        return answer

    monkeypatch.setattr(cascade_bridge.hermes_voice, "probe", probe)
    return probed


async def _start(trust, monkeypatch, *, answer=True):
    ran = _lanes(monkeypatch)
    probed = _probe(monkeypatch, answer)
    cs = session_mod.CallSession(config_mod.load(), FakeBrowser(), ApprovalStore())
    ok = await cs.start("TOKroom", trust, "alex", "Alex", mission=None)
    bridge = cs._bridge
    if cs._run_task is not None:
        await cs._run_task
        await asyncio.sleep(0)                      # let the done-callback's reconcile run
        if cs._reconcile_task is not None:
            await cs._reconcile_task
    return cs, ok, bridge, ran, probed


# ------------------------------------------------------------ owner, Hermes up --

@pytest.mark.asyncio
async def test_the_owner_is_answered_by_the_selected_hermes_profile(talk_env, monkeypatch):
    cs, ok, bridge, ran, probed = await _start("owner", monkeypatch)
    assert ok is True
    assert isinstance(bridge, cascade_bridge.DirectLaneBridge)
    assert probed == [GATEWAY]
    ((lane, inner),) = ran
    assert lane == "cascade" and isinstance(inner, cascade_bridge.CascadeBridge)
    # The REAL CascadeBridge constructor ran with no Mission: every `mission.` access in
    # it had to be made optional, and this is what executes them.
    assert inner._mission is None
    assert inner._recorder.direction == "inbound"
    assert inner._session._direction == "inbound"
    conversation = inner._session._hermes
    assert conversation._url == f"{GATEWAY}/v1/chat/completions"
    assert conversation.session_id == "voice-TOKroom"
    assert "You are speaking with Alex." in conversation._instructions
    assert talk_env["events"] == []
    snap = lkg.load(profiles.OUTLET_TALK, "inbound")
    assert snap is not None and snap.profile.agent_id == "robot-direct"


# ------------------------------------------------- a guest never reaches the lane --

@pytest.mark.asyncio
@pytest.mark.parametrize("trust", ["guest", "", "OWNER", "unknown"])
async def test_a_guest_never_reaches_the_direct_lane(talk_env, monkeypatch, trust):
    """The owner put Talk in scope "owner only" (q2-outlets). The direct lane hands the
    caller Hermes's own tools with no approval step, so anyone who is not exactly
    ``owner`` keeps what a guest had: the Realtime lane and the owner-approval loop.
    ``trust`` arrives from the plugin on an unauthenticated local endpoint and defaults
    to ``guest``, so the comparison is exact and everything else falls on the safe side."""
    cs, ok, bridge, ran, probed = await _start(trust, monkeypatch)
    assert ok is True
    assert isinstance(bridge, RealtimeBridge)
    assert not isinstance(bridge, cascade_bridge.DirectLaneBridge)
    assert probed == []                              # Hermes is not even asked
    assert [lane for lane, _ in ran] == ["realtime"]
    assert bridge._profile is None                   # the bridge's own defaults
    assert "request_owner_approval" in json.dumps(hermes.TOOLS)
    # Not the assigned Agent's call, so not its last-known-good either.
    assert lkg.load(profiles.OUTLET_TALK, "inbound") is None


# ------------------------------------------------------------ owner, Hermes down --

@pytest.mark.asyncio
async def test_hermes_down_at_pickup_answers_on_realtime_loudly(talk_env, monkeypatch):
    cs, ok, bridge, ran, probed = await _start("owner", monkeypatch, answer=False)
    assert ok is True and isinstance(bridge, cascade_bridge.DirectLaneBridge)
    assert probed == [GATEWAY]
    ((lane, inner),) = ran
    assert lane == "realtime" and inner._profile is None
    (fall,) = [e for e in talk_env["events"] if e.get("type") == "fallback"]
    assert fall["kind"] == "lane" and fall["outlet"] == "talk"
    assert fall["assigned_agent"] == "robot-direct" and fall["answered_on"] == "realtime"
    assert "did not answer the pickup check" in fall["reason"]
    assert bridge.fell_back is True
    # The direct Agent did not take this call, so it is not what just completed one.
    assert lkg.load(profiles.OUTLET_TALK, "inbound") is None


@pytest.mark.asyncio
async def test_an_unroutable_profile_is_the_same_loud_fallback(talk_env, monkeypatch):
    (talk_env["dir"] / "gateways" / "gateways.json").write_text(
        json.dumps({"profiles": {"vega": {"status": "incomplete"}}}))
    cs, ok, bridge, ran, probed = await _start("owner", monkeypatch)
    assert probed == [] and [lane for lane, _ in ran] == ["realtime"]
    (fall,) = [e for e in talk_env["events"] if e.get("type") == "fallback"]
    assert "hermes_profile 'vega' is not routable" in fall["reason"]


@pytest.mark.asyncio
async def test_a_hangup_during_the_pickup_check_answers_nothing(talk_env, monkeypatch):
    ran = _lanes(monkeypatch)
    release = asyncio.Event()

    async def slow_probe(url, **kw):
        await release.wait()
        return True

    monkeypatch.setattr(cascade_bridge.hermes_voice, "probe", slow_probe)
    profile = profiles.load_effective_profile("inbound", outlet=profiles.OUTLET_TALK)
    bridge = cascade_bridge.DirectLaneBridge(
        config_mod.load(), profile, token="TOK", caller="Alex",
        realtime_factory=lambda: pytest.fail("realtime launched after a hangup"))
    task = asyncio.create_task(bridge.run())
    await asyncio.sleep(0)
    await bridge.stop()
    release.set()
    await task
    assert ran == []


# ------------------------------------------------------------------ refusals --

@pytest.mark.asyncio
async def test_an_unwired_direct_agent_is_refused_in_start_and_frees_the_slot(
        talk_env, monkeypatch):
    """The pickup check defers every resource, but NOT the honesty gates: an Agent whose
    TTS has no live client must still be refused inside start(), where a refusal frees
    the slot, rather than crash inside the run task."""
    (talk_env["dir"] / "agents" / "direct.yaml").write_text(yaml.safe_dump(
        _agent(providers={"stt": "deepgram", "llm": "hermes-agent", "tts": "cartesia"})))
    cs, ok, bridge, ran, probed = await _start("owner", monkeypatch)
    assert ok is False and bridge is None and not cs.busy
    assert cs.last_start_failure["code"] == "setup_failed"
    assert "cartesia" in cs.last_start_failure["detail"]
    assert ran == [] and probed == []


@pytest.mark.asyncio
async def test_a_vendor_cascade_still_cannot_answer_a_talk_call(talk_env, monkeypatch):
    (talk_env["dir"] / "agents" / "direct.yaml").write_text(yaml.safe_dump(
        _agent(providers={"stt": "deepgram", "llm": "openrouter", "tts": "elevenlabs"})))
    cs, ok, bridge, ran, probed = await _start("owner", monkeypatch)
    assert ok is False and bridge is None and ran == []


# ------------------------------------------------------------ the idle backstop --

def _inbound_bridge(talk_env, idle_timeout):
    profile = profiles.load_effective_profile("inbound", outlet=profiles.OUTLET_TALK)
    cfg = config_mod.load()
    cfg = type(cfg)(**{**cfg.__dict__, "idle_timeout": idle_timeout})
    return cascade_bridge.CascadeBridge(
        cfg, profile, None, token="TOK", caller="Alex",
        stt=_QuietDeepgram(), wire=_QuietWire())


@pytest.mark.asyncio
async def test_an_inbound_call_nobody_is_speaking_on_is_ended(talk_env, monkeypatch):
    """The cascade lane on Talk had no idleness signal: parec streams silence forever. An
    inbound call the plugin failed to hang up held the single slot, with Deepgram
    streaming, until CallSession's 30-minute ceiling."""
    bridge = _inbound_bridge(talk_env, idle_timeout=0.0)
    closed: list = []

    async def aclose():
        closed.append(True)

    bridge._wire.aclose = aclose
    real_sleep = asyncio.sleep
    monkeypatch.setattr(cascade_bridge.asyncio, "sleep", lambda s: real_sleep(0))
    await asyncio.wait_for(bridge._idle_watchdog(), timeout=2.0)
    assert closed == [True]


@pytest.mark.asyncio
async def test_a_call_with_recent_speech_is_left_alone(talk_env, monkeypatch):
    bridge = _inbound_bridge(talk_env, idle_timeout=3600.0)
    closed: list = []

    async def aclose():
        closed.append(True)

    bridge._wire.aclose = aclose
    real_sleep = asyncio.sleep
    monkeypatch.setattr(cascade_bridge.asyncio, "sleep", lambda s: real_sleep(0))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bridge._idle_watchdog(), timeout=0.2)
    assert closed == []


# ------------------------------------- the in-call tool goes to THIS Agent's profile --

@pytest.mark.asyncio
async def test_the_cascade_tool_reaches_the_agents_own_gateway(talk_env, monkeypatch):
    """Found while unifying the resolver. The Talk cascade lane sent hermes_agent to
    cfg.hermes_gateway_url whatever the Agent was bound to, so a second profile's tool
    calls landed on the DEFAULT Agent's backend. The realtime lane never had this bug."""
    (talk_env["dir"] / "agents" / "direct.yaml").write_text(yaml.safe_dump(
        _agent(providers={"stt": "deepgram", "llm": "openrouter", "tts": "elevenlabs"},
               guardrails={"on_call_tools": True})))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    (talk_env["dir"] / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {"talk": {"inbound": None, "outbound": "robot-direct"}}}))
    profile = profiles.load_effective_profile("outbound", outlet=profiles.OUTLET_TALK)
    sent: list = []

    async def call(instruction, *, gateway_url, token, timeout):
        sent.append(gateway_url)
        return "done"

    monkeypatch.setattr(cascade_bridge.hermes, "call_hermes_agent", call)
    cfg = config_mod.load()
    mission = outbound_mod.OutboundMission(brief="b", report_channel="talk",
                                           report_address="R", target_display="Alex")
    bridge = cascade_bridge.CascadeBridge(cfg, profile, mission, token="TOK",
                                          stt=_QuietDeepgram(), wire=_QuietWire())
    assert await bridge._session._hermes_call("check the NAS") == "done"
    assert sent == [GATEWAY] and GATEWAY != cfg.hermes_gateway_url

    # Unroutable: an honest sentence and no request at all, never the default backend.
    (talk_env["dir"] / "gateways" / "gateways.json").write_text('{"profiles": {}}')
    bridge = cascade_bridge.CascadeBridge(cfg, profile, mission, token="TOK2",
                                          stt=_QuietDeepgram(), wire=_QuietWire())
    reply = await bridge._session._hermes_call("check the NAS")
    assert "can't reach the backend" in reply and sent == [GATEWAY]


def test_there_is_one_resolver_on_this_bridge_too():
    assert not hasattr(hermes, "_profile_gateway_map")
    assert not hasattr(hermes, "HERMES_PROFILE_GATEWAY_URLS")
