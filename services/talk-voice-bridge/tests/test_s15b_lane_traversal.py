"""s15b-B b1/b2: the Talk lanes traverse CallSession.start through REAL code.

The defect this closes: ``test_s12b_dispatch`` monkeypatches ``CascadeBridge`` with a
FAKE CLASS, feeds a ``types.SimpleNamespace`` mission and stubs ``overlay_profile`` to a
passthrough. The REAL constructor was therefore executed by NOTHING -- which is exactly
how ``mission.to`` (a field ``OutboundMission`` has never had) shipped and crashed the
first live Talk cascade dial. The fixture agreed with the code instead of with reality.

What is REAL here, and deliberately so:
  * the profile      -- an on-disk YAML through ``profiles.load_effective_profile``
  * the overlay      -- ``config.overlay_profile``, NOT a passthrough lambda
  * the mission      -- ``outbound.OutboundMission``, asserted by isinstance
  * the bridge       -- ``cascade_bridge.CascadeBridge`` / ``RealtimeBridge``, the real
                        classes, asserted by isinstance, with their real ``__init__``
                        running (config build + honesty gates + recorder wiring)

The ONLY fake is ``run()``, patched as a METHOD on the real class -- the audio path is
covered honestly by test_s12b_cascade_bridge and is not this test's subject. Patching a
method leaves ``__init__`` and the class identity intact; replacing the CLASS is what
hid the bug and is forbidden here.
"""
import asyncio
import json

import pytest
import yaml

import cascade_bridge
import config as config_mod
import outbound as outbound_mod
from voicecore import profiles
import session as session_mod
from approval import ApprovalStore
from realtime_bridge import RealtimeBridge

CASCADE_PROVIDERS = {"stt": "deepgram", "llm": "nvidia-nemotron", "tts": "elevenlabs"}


class FakeBrowser:
    """The Playwright/Nextcloud edge -- not a lane seam."""

    def __init__(self):
        self.started, self.joined, self.left = [], [], 0

    async def start_call(self, token):
        self.started.append(token)

    async def join_call(self, token):
        self.joined.append(token)

    async def leave_call(self):
        self.left += 1

    async def room_call_state(self, token):
        return {"hasCall": False}


def _write_agents(tmp_path):
    """A config dir carrying BOTH a real cascade profile and a real realtime one."""
    d = tmp_path / "vcfg"
    (d / "agents").mkdir(parents=True)
    (d / "agents" / "cascade.yaml").write_text(yaml.safe_dump({
        "id": "lane-cascade", "pipeline": "cascade",
        "providers": dict(CASCADE_PROVIDERS)}))
    (d / "agents" / "realtime.yaml").write_text(yaml.safe_dump({
        "id": "lane-realtime", "pipeline": "realtime",
        "providers": {"realtime": "openai-gpt-realtime"}}))
    # The active.yaml POINTER is how cascade is selected in production. VOICE_AGENT is
    # NOT usable here: config.load() resolves it with direction="inbound", and cascade is
    # outbound-only (D3), so an env-selected cascade agent raises before the lane is even
    # reached. Discovered by running it -- worth pinning as lane knowledge.
    (d / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {o: {"inbound": "lane-realtime", "outbound": "lane-cascade"}
                     for o in profiles.OUTLETS}}))
    return d


def _point(cfg_dir, *, outbound):
    """Repoint active.yaml's OUTBOUND slot (inbound stays realtime)."""
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {o: {"inbound": "lane-realtime", "outbound": outbound}
                     for o in profiles.OUTLETS}}))


@pytest.fixture
def lane_env(tmp_path, monkeypatch):
    d = _write_agents(tmp_path)
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(d))
    # The talk bridge IS a cascade host in production (server.py:36 flips this at import);
    # this test does not import server.py, so set it explicitly rather than rely on order.
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {"talk": frozenset({"outbound", "inbound"})})
    monkeypatch.setattr(session_mod.profiles, "CASCADE_CAPABILITY", {"talk": frozenset({"outbound", "inbound"})})
    return d


def _neutralise_run(monkeypatch):
    """Fake ONLY run(), as a method on the real classes. __init__ still executes."""
    async def _idle(self):
        await asyncio.Event().wait()

    async def _stop(self):
        return None

    monkeypatch.setattr(cascade_bridge.CascadeBridge, "run", _idle)
    monkeypatch.setattr(cascade_bridge.CascadeBridge, "stop", _stop)
    monkeypatch.setattr(RealtimeBridge, "run", _idle)
    monkeypatch.setattr(RealtimeBridge, "stop", _stop)


def _mission():
    """A REAL OutboundMission. test_s12b_dispatch used types.SimpleNamespace(to=...),
    which is precisely how a nonexistent field reached production."""
    return outbound_mod.OutboundMission(
        brief="say hi", report_channel="talk", report_address="R",
        target_display="Alex")


# -- b1: Talk-cascade ----------------------------------------------------------

@pytest.mark.asyncio
async def test_b1_talk_cascade_constructs_the_real_bridge(lane_env, monkeypatch):
    _neutralise_run(monkeypatch)
    mission = _mission()
    assert isinstance(mission, outbound_mod.OutboundMission), \
        "the mission must be the real dataclass, not a duck-type"

    cs = session_mod.CallSession(config_mod.load(), FakeBrowser(), ApprovalStore())
    assert await cs.start("TOK", "guest", "+61", "Alex", mission=mission) is True

    bridge = cs._bridge
    assert isinstance(bridge, cascade_bridge.CascadeBridge), \
        "the REAL CascadeBridge must be constructed -- not a fake class"
    # The real __init__ ran: it resolved a cascade config and wired a recorder.
    assert bridge._profile.pipeline == "cascade"
    assert bridge._mission is mission
    assert bridge._recorder is not None
    await cs.stop("TOK")


@pytest.mark.asyncio
async def test_b1_real_constructor_keys_call_id_on_the_room_token(lane_env, monkeypatch):
    """The s14b crash in one assertion: the recorder's call_id comes from the ROOM TOKEN.

    The shipped bug built it from ``mission.to``, a field OutboundMission does not have.
    A duck-typed mission carrying ``to`` made that unreachable in test.
    """
    _neutralise_run(monkeypatch)
    cs = session_mod.CallSession(config_mod.load(), FakeBrowser(), ApprovalStore())
    assert await cs.start("ROOM-XYZ", "guest", "+61", "Alex", mission=_mission()) is True
    assert cs._bridge._recorder.call_id == "ROOM-XYZ"
    assert not hasattr(outbound_mod.OutboundMission, "to")
    await cs.stop("ROOM-XYZ")


@pytest.mark.asyncio
async def test_b1_overlay_profile_is_the_real_one(lane_env, monkeypatch):
    """The graded path must not run a passthrough overlay.

    Proven by OBSERVATION, not by reading source: wrap the real overlay, assert it was
    called with the resolved cascade profile and that its result is what the bridge got.
    """
    _neutralise_run(monkeypatch)
    seen = {}
    real_overlay = config_mod.overlay_profile

    def spy(cfg, profile):
        out = real_overlay(cfg, profile)          # the REAL implementation runs
        if profile is not None:                   # config.load() also calls it with None
            seen["pipeline"] = profile.pipeline
            seen["result"] = out
        return out

    monkeypatch.setattr(session_mod.config, "overlay_profile", spy)
    cs = session_mod.CallSession(config_mod.load(), FakeBrowser(), ApprovalStore())
    assert await cs.start("TOK", "guest", "+61", "Alex", mission=_mission()) is True
    assert seen["pipeline"] == "cascade"
    assert cs._bridge._cfg is seen["result"]
    await cs.stop("TOK")


# -- b2: Talk-realtime ---------------------------------------------------------

@pytest.mark.asyncio
async def test_b2_talk_realtime_constructs_the_real_bridge(lane_env, monkeypatch):
    _neutralise_run(monkeypatch)
    _point(lane_env, outbound="lane-realtime")
    cs = session_mod.CallSession(config_mod.load(), FakeBrowser(), ApprovalStore())
    assert await cs.start("TOK", "guest", "+61", "Alex", mission=_mission()) is True
    assert isinstance(cs._bridge, RealtimeBridge)
    assert not isinstance(cs._bridge, cascade_bridge.CascadeBridge)
    await cs.stop("TOK")


@pytest.mark.asyncio
async def test_b2_the_two_lanes_are_distinguishable_from_one_config_dir(
        lane_env, monkeypatch):
    """Same config dir, same session class, same start() -- only the ACTIVATED profile
    differs, and the lane follows it. This is the dispatch contract, driven for real."""
    _neutralise_run(monkeypatch)
    got = {}
    for agent, expected in (("lane-cascade", cascade_bridge.CascadeBridge),
                            ("lane-realtime", RealtimeBridge)):
        _point(lane_env, outbound=agent)
        cs = session_mod.CallSession(config_mod.load(), FakeBrowser(), ApprovalStore())
        assert await cs.start("T", "guest", "+61", "Alex", mission=_mission()) is True
        got[agent] = type(cs._bridge)
        assert isinstance(cs._bridge, expected)
        await cs.stop("T")
    assert got["lane-cascade"] is not got["lane-realtime"]


# -- the code refuses inbound cascade by design (session.py:194-197) -----------

@pytest.mark.asyncio
async def test_b1_inbound_cascade_is_refused_not_traversed(lane_env, monkeypatch):
    """Pinned so nobody 'completes the matrix' by inventing an inbound cell for an
    OUTSIDE-VENDOR cascade: it has no inbound prompt, caller identity or tool policy, and
    load_effective_profile raises for it. (D3 is void since VC23. The one cascade shape
    that does answer a call is the direct Hermes lane, providers.llm: hermes-agent, and
    test_direct_lane_talk.py owns that cell.)"""
    _neutralise_run(monkeypatch)
    cfg = config_mod.load()
    # Point INBOUND at the cascade agent -- otherwise this proves nothing (the default
    # inbound pointer is realtime, which would legitimately succeed).
    import yaml as _yaml
    (lane_env / "active.yaml").write_text(_yaml.safe_dump(
        {"outlets": {o: {"inbound": "lane-cascade", "outbound": "lane-cascade"}
                     for o in profiles.OUTLETS}}))
    cs = session_mod.CallSession(cfg, FakeBrowser(), ApprovalStore())
    assert await cs.start("TOK", "guest", "+61", "Alex", mission=None) is False
    assert cs._bridge is None
    assert not cs.busy



class _QuietDeepgram:
    """Only what teardown touches. The STT path itself is test_s12b_cascade_bridge's."""

    async def close(self):
        return None


class _QuietWire:
    """A no-op wire: teardown must not need a live PulseAudio to reach retain."""

    def __getattr__(self, name):
        async def _noop(*a, **kw):
            return None
        return _noop


# -- b6: retain reached from the lane's own TEARDOWN ---------------------------

@pytest.mark.asyncio
async def test_b6_retain_is_dispatched_from_cascade_teardown(tmp_path, monkeypatch):
    """Retain must be reached by the lane ENDING, not by a test calling the client.

    A free-standing ``hindsight.retain(...)`` unit test proves the client works; it does
    NOT prove any lane ever calls it. This drives the real ``CascadeLiveSession`` teardown
    and asserts BOTH halves in one place: retain was dispatched, and the eventlog row on
    disk records that fact (``retain_status``), which is what the dashboard reads.
    """
    from voicecore import cascade_live
    from voicecore import eventlog
    from voicecore import hindsight
    from voicecore import turn_detect
    from test_s12b_cascade_bridge import CONFIG, ENV, FMT

    dispatched = []
    monkeypatch.setattr(hindsight, "retain_detached",
                        lambda url, bank, **kw: dispatched.append(kw) or True)
    monkeypatch.setattr(cascade_live.hindsight, "retain_detached",
                        lambda url, bank, **kw: dispatched.append(kw) or True)

    path = tmp_path / "events.jsonl"
    recorder = eventlog.CallRecorder(
        call_id="ROOM-9", mode="talk", pipeline="cascade", direction="outbound",
        target="Alex", path=str(path))

    session = cascade_live.CascadeLiveSession(
        twilio_ws=None, stream_sid=None, config=CONFIG, profile=None,
        recorder=recorder, env=ENV, mission_brief="", stt=_QuietDeepgram(),
        hermes_call=None, tools_enabled=False,
        detector=turn_detect.TurnDetector(silence_ms=60, bytes_per_ms=FMT.bytes_per_ms,
                                          decode=FMT.decode_pcm16),
        filler_text="one sec", filler_debounce_s=2.0, transport=None,
        retain_default=True, hindsight_url="http://hindsight.test",
        hindsight_bank="voice", audio_format=FMT, wire=_QuietWire())
    session.transcript = ["Them: hello", "AI: hi"]

    await session.teardown(outcome="ok")

    assert dispatched, "the cascade lane's teardown never dispatched retain"
    assert dispatched[0]["metadata"]["platform"] == "voice_cascade"
    assert dispatched[0]["document_id"] == "voice-cascade-ROOM-9"

    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    terminal = [r for r in rows if r.get("call_id") == "ROOM-9"]
    assert terminal, "teardown wrote no eventlog row"
    assert terminal[-1]["retain_status"] == "dispatched"
    assert terminal[-1]["mode"] == "talk" and terminal[-1]["pipeline"] == "cascade"


@pytest.mark.asyncio
async def test_b6_retain_is_skipped_when_the_profile_says_so(tmp_path, monkeypatch):
    """Positive control for b6: the dispatch is conditional, so a green b6 cannot come
    from a teardown that fires retain unconditionally."""
    from voicecore import cascade_live
    from voicecore import eventlog
    from voicecore import turn_detect
    from test_s12b_cascade_bridge import CONFIG, ENV, FMT

    dispatched = []
    monkeypatch.setattr(cascade_live.hindsight, "retain_detached",
                        lambda url, bank, **kw: dispatched.append(kw) or True)
    path = tmp_path / "events.jsonl"
    session = cascade_live.CascadeLiveSession(
        twilio_ws=None, stream_sid=None, config=CONFIG, profile=None,
        recorder=eventlog.CallRecorder(call_id="ROOM-X", mode="talk", pipeline="cascade",
                                       direction="outbound", path=str(path)),
        env=ENV, mission_brief="", stt=_QuietDeepgram(),
        hermes_call=None, tools_enabled=False,
        detector=turn_detect.TurnDetector(silence_ms=60, bytes_per_ms=FMT.bytes_per_ms,
                                          decode=FMT.decode_pcm16),
        filler_text="one sec", filler_debounce_s=2.0, transport=None,
        retain_default=False, hindsight_url="http://hindsight.test",
        hindsight_bank="voice", audio_format=FMT, wire=_QuietWire())
    session.transcript = ["Them: hello"]
    await session.teardown(outcome="ok")
    assert dispatched == []
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    assert rows[-1]["retain_status"] == "skipped"
