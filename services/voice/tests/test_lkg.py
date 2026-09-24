"""s8 tests, Mode C: the last-known-good answering fallback (ticket 08).

Two layers, deliberately split like the s16 suites:

1. The SHARED ``voicecore.lkg`` contracts - record/load round-trip, per-slot
   isolation, loud fallback resolution, pointer-untouched, env precedence. These
   live ONCE here (both suites import the same module; duplicating the matrix is
   the twin-copy smell VC17 removed).
2. The phone bridge's own wiring - media_stream answers with the snapshot when the
   phone slot breaks, records it when a call completes, refuses when no snapshot
   exists, and the outbound pre-dial gate grades the snapshot.

Every behavioural claim here was sabotage-checked: remove the fallback and the
broken-slot answer tests go red (the line goes dead), remove the announce and the
loudness tests go red, remove the teardown record and the store stays empty.
"""
import importlib
import json

import pytest
import yaml
from fastapi.testclient import TestClient

from voicecore import lkg
from voicecore import profiles
import server
from conftest import FakeOpenAIWS
from profile_helpers import profile_doc, write_config_dir

PHONE_MODEL = "gpt-realtime-probe-phone"
LKG_MODEL = "gpt-realtime-probe-lkg"


def _agent(aid, voice, model, **overrides):
    return profile_doc(id=aid, knobs={"voice": voice, "model": model}, **overrides)


def _point_env(monkeypatch, d, voice_agent=None):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(d))
    if voice_agent is None:
        monkeypatch.delenv("VOICE_AGENT", raising=False)
    else:
        monkeypatch.setenv("VOICE_AGENT", voice_agent)


def _write_pointer(d, inbound=None, outbound=None):
    (d / "active.yaml").write_text(yaml.safe_dump({
        "outlets": {"phone": {"inbound": inbound, "outbound": outbound}}}))
    return d / "active.yaml"


def _recorded_profile(d, monkeypatch, *, direction="inbound", agent_id="lkg-agent"):
    """Activate a REAL profile and record it as the phone outlet's LKG - the store is
    only ever written from something that actually activated (the same object a
    completed call would hand lkg.record)."""
    _point_env(monkeypatch, d)
    profile = profiles.load_effective_profile(direction, outlet=profiles.OUTLET_PHONE)
    assert profile is not None and profile.agent_id == agent_id
    lkg.record(profiles.OUTLET_PHONE, direction, profile, call_id="call-that-worked")
    return profile


# ---------------------------------------------------------------------------
# The shared store: record -> load round-trip, isolation, honesty
# ---------------------------------------------------------------------------

def test_record_then_load_roundtrip_and_survives_restart(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [_agent("lkg-agent", "marin", LKG_MODEL)])
    _write_pointer(d, inbound="lkg-agent", outbound=None)
    profile = _recorded_profile(d, monkeypatch)

    snap = lkg.load(profiles.OUTLET_PHONE, "inbound")
    assert snap is not None
    assert snap.profile.agent_id == "lkg-agent"
    assert snap.profile.doc == profile.doc
    assert snap.profile.registry == profile.registry
    assert snap.last_call_id == "call-that-worked"
    assert isinstance(snap.recorded_at, float)

    # A restart is just a fresh read: reload the module (new module state, same disk).
    importlib.reload(lkg)
    again = lkg.load(profiles.OUTLET_PHONE, "inbound")
    assert again is not None and again.profile.agent_id == "lkg-agent"


def test_load_missing_store_is_none(tmp_path, monkeypatch):
    assert lkg.load(profiles.OUTLET_PHONE, "inbound") is None


def test_load_corrupt_store_is_none(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [_agent("lkg-agent", "marin", LKG_MODEL)])
    _write_pointer(d, inbound="lkg-agent")
    _recorded_profile(d, monkeypatch)
    lkg.snapshot_path(profiles.OUTLET_PHONE, "inbound").write_text("{not json")
    assert lkg.load(profiles.OUTLET_PHONE, "inbound") is None


def test_load_wrong_shape_or_foreign_slot_is_none(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [_agent("lkg-agent", "marin", LKG_MODEL)])
    _write_pointer(d, inbound="lkg-agent")
    _recorded_profile(d, monkeypatch)
    path = lkg.snapshot_path(profiles.OUTLET_PHONE, "inbound")
    good = json.loads(path.read_text())

    bad_schema = dict(good, schema=99)
    path.write_text(json.dumps(bad_schema))
    assert lkg.load(profiles.OUTLET_PHONE, "inbound") is None

    bad_doc_id = dict(good, doc=dict(good["doc"], id="someone-else"))
    path.write_text(json.dumps(bad_doc_id))
    assert lkg.load(profiles.OUTLET_PHONE, "inbound") is None

    foreign = dict(good, direction="outbound")
    path.write_text(json.dumps(foreign))
    assert lkg.load(profiles.OUTLET_PHONE, "inbound") is None


def test_slots_are_isolated(tmp_path, monkeypatch):
    """One (outlet, direction) snapshot serves exactly that slot - recording the phone
    inbound slot must not arm the phone outbound or talk slots."""
    d = write_config_dir(tmp_path, [_agent("lkg-agent", "marin", LKG_MODEL)])
    _write_pointer(d, inbound="lkg-agent", outbound=None)
    _recorded_profile(d, monkeypatch, direction="inbound")

    assert lkg.load(profiles.OUTLET_PHONE, "inbound") is not None
    assert lkg.load(profiles.OUTLET_PHONE, "outbound") is None
    assert lkg.load(profiles.OUTLET_TALK, "inbound") is None
    assert lkg.load(profiles.OUTLET_TALK, "outbound") is None


def test_record_is_never_written_from_a_pointer_alone(tmp_path, monkeypatch):
    """The store is only written by a completed call. Assigning a pointer (even a
    healthy one) writes NOTHING - a pointer that never served a call is not
    known-good."""
    d = write_config_dir(tmp_path, [_agent("lkg-agent", "marin", LKG_MODEL)])
    _write_pointer(d, inbound="lkg-agent")
    _point_env(monkeypatch, d)
    profiles.load_effective_profile("inbound", outlet=profiles.OUTLET_PHONE)
    assert lkg.load(profiles.OUTLET_PHONE, "inbound") is None


def test_record_ignores_profile_less_and_unknown_slots(tmp_path, monkeypatch):
    lkg.record(profiles.OUTLET_PHONE, "inbound", None)          # no profile: no write
    lkg.record("sms", "inbound", object())                      # unknown slot: no write
    assert lkg.load(profiles.OUTLET_PHONE, "inbound") is None
    assert not lkg._events_dir().exists()                       # never even created


# ---------------------------------------------------------------------------
# resolve(): loud fallback at the point of failure, and nothing else
# ---------------------------------------------------------------------------

def test_resolve_answers_with_the_snapshot_and_announces(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [_agent("lkg-agent", "marin", LKG_MODEL)])
    pointer = _write_pointer(d, inbound="lkg-agent")
    _recorded_profile(d, monkeypatch)
    _write_pointer(d, inbound="ghost-agent")                    # the slot breaks
    _point_env(monkeypatch, d)

    events = []
    monkeypatch.setattr(lkg.eventlog, "append_event",
                        lambda obj, *a, **k: events.append(obj))

    snapshot = lkg.resolve("inbound", outlet=profiles.OUTLET_PHONE)
    assert snapshot is not None and snapshot.agent_id == "lkg-agent"

    falls = [e for e in events if e.get("type") == "fallback"]
    assert len(falls) == 1
    fall = falls[0]
    assert fall["outlet"] == "phone" and fall["direction"] == "inbound"
    assert fall["broken_agent"] == "ghost-agent"
    assert fall["snapshot_agent_id"] == "lkg-agent"
    assert "ghost-agent" in fall["reason"]
    # The dashboard still sees the slot broken: the pointer file is untouched.
    assert pointer.read_text() == yaml.safe_dump(
        {"outlets": {"phone": {"inbound": "ghost-agent", "outbound": None}}})


def test_resolve_without_snapshot_still_refuses_loudly(tmp_path, monkeypatch):
    """No snapshot => the pre-fallback loud refusal, byte for byte."""
    d = write_config_dir(tmp_path, [_agent("lkg-agent", "marin", LKG_MODEL)])
    _write_pointer(d, inbound="ghost-agent")
    _point_env(monkeypatch, d)

    events = []
    monkeypatch.setattr(lkg.eventlog, "append_event",
                        lambda obj, *a, **k: events.append(obj))

    with pytest.raises(profiles.ProfileError) as exc:
        lkg.resolve("inbound", outlet=profiles.OUTLET_PHONE)
    assert "ghost-agent" in str(exc.value) and "not found" in str(exc.value)
    assert events == []


def test_healthy_assignment_never_consults_the_store(tmp_path, monkeypatch):
    """The snapshot is consulted ONLY on activation failure - it never overrides a
    working assignment."""
    d = write_config_dir(tmp_path, [_agent("lkg-agent", "marin", LKG_MODEL),
                                    _agent("new-agent", "cedar", PHONE_MODEL)])
    _write_pointer(d, inbound="lkg-agent")
    _recorded_profile(d, monkeypatch)
    _write_pointer(d, inbound="new-agent")                      # operator reassigns
    _point_env(monkeypatch, d)

    events = []
    monkeypatch.setattr(lkg.eventlog, "append_event",
                        lambda obj, *a, **k: events.append(obj))

    snapshot = lkg.resolve("inbound", outlet=profiles.OUTLET_PHONE)
    assert snapshot is not None and snapshot.agent_id == "new-agent"
    assert events == []


def test_env_voice_agent_still_wins_outright(tmp_path, monkeypatch):
    """VOICE_AGENT precedence is untouched: a valid env selection resolves with no
    fallback, even when the pointer is broken AND a snapshot exists."""
    d = write_config_dir(tmp_path, [_agent("lkg-agent", "marin", LKG_MODEL),
                                    _agent("env-agent", "cedar", PHONE_MODEL)])
    _write_pointer(d, inbound="lkg-agent")
    _recorded_profile(d, monkeypatch)
    _write_pointer(d, inbound="ghost-agent")                    # pointer breaks
    _point_env(monkeypatch, d, voice_agent="env-agent")         # env selection wins

    events = []
    monkeypatch.setattr(lkg.eventlog, "append_event",
                        lambda obj, *a, **k: events.append(obj))

    snapshot = lkg.resolve("inbound", outlet=profiles.OUTLET_PHONE)
    assert snapshot is not None and snapshot.agent_id == "env-agent"
    assert events == []


def test_broken_env_voice_agent_also_falls_back(tmp_path, monkeypatch):
    """A broken VOICE_AGENT is a broken assignment like any other: the outlet answers
    with the snapshot, loudly - "the phone never goes dead silently" holds whichever
    selection mechanism broke."""
    d = write_config_dir(tmp_path, [_agent("lkg-agent", "marin", LKG_MODEL)])
    _write_pointer(d, inbound="lkg-agent")
    _recorded_profile(d, monkeypatch)
    _point_env(monkeypatch, d, voice_agent="ghost-agent")       # env selection breaks

    snapshot = lkg.resolve("inbound", outlet=profiles.OUTLET_PHONE)
    assert snapshot is not None and snapshot.agent_id == "lkg-agent"


def test_programming_errors_are_never_masked_by_a_fallback(tmp_path, monkeypatch):
    """Unknown outlet/direction are call-site bugs, not activation failures - they
    must raise even when a snapshot exists (the fallback answers activation
    failures, never paper over a bridge that resolves the wrong slot)."""
    d = write_config_dir(tmp_path, [_agent("lkg-agent", "marin", LKG_MODEL)])
    _write_pointer(d, inbound="lkg-agent")
    _recorded_profile(d, monkeypatch)
    _point_env(monkeypatch, d)

    with pytest.raises(profiles.ProfileError) as exc:
        lkg.resolve("inbound", outlet="sms")
    assert "unknown outlet" in str(exc.value)
    with pytest.raises(profiles.ProfileError) as exc:
        lkg.resolve("sideways", outlet=profiles.OUTLET_PHONE)
    assert "unknown call direction" in str(exc.value)


# ---------------------------------------------------------------------------
# The phone bridge on the wire
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    return TestClient(server.app)


def _drive(client, monkeypatch, fake, *, capture_events=None):
    urls = []

    def connect(url, *a, **kw):
        urls.append(url)
        return fake

    monkeypatch.setattr(server.websockets, "connect", connect)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")
    if capture_events is None:
        monkeypatch.setattr(server.eventlog, "append_event", lambda *a, **k: None)
    else:
        monkeypatch.setattr(server.eventlog, "append_event",
                            lambda obj, *a, **k: capture_events.append(obj))
    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZs3",
                      "start": {"streamSid": "MZs3", "callSid": "CAx",
                                "customParameters": {"inbound_token":
                                                     server._mint_inbound_token()}}})
        ws.send_json({"event": "stop", "sequenceNumber": "2", "streamSid": "MZs3"})
    return urls


def test_media_stream_answers_with_the_snapshot_when_the_slot_breaks(
        client, monkeypatch, tmp_path):
    """THE sabotage-checked claim: with a recorded snapshot, breaking the phone slot
    must NOT go dead - the call answers with the snapshot, loudly. (Reverting the
    lkg.resolve call at media_stream makes this test red: urls stays empty.)"""
    d = write_config_dir(tmp_path, [_agent("lkg-agent", "marin", LKG_MODEL)])
    pointer = _write_pointer(d, inbound="lkg-agent")
    _point_env(monkeypatch, d)

    # First call: healthy, completes, records the snapshot (the wire-level record pin).
    _drive(client, monkeypatch, FakeOpenAIWS())
    assert lkg.load(profiles.OUTLET_PHONE, "inbound") is not None

    # The slot breaks after storage.
    _write_pointer(d, inbound="ghost-agent")
    _point_env(monkeypatch, d)

    events = []
    urls = _drive(client, monkeypatch, FakeOpenAIWS(), capture_events=events)
    assert urls == [f"wss://api.openai.com/v1/realtime?model={LKG_MODEL}"]

    falls = [e for e in events if e.get("type") == "fallback"]
    assert len(falls) == 1
    assert falls[0]["outlet"] == "phone"
    assert falls[0]["broken_agent"] == "ghost-agent"
    assert falls[0]["snapshot_agent_id"] == "lkg-agent"
    # The dashboard still shows the slot broken - the pointer is untouched.
    assert pointer.read_text() == yaml.safe_dump(
        {"outlets": {"phone": {"inbound": "ghost-agent", "outbound": None}}})


def test_media_stream_still_refuses_when_no_snapshot_exists(
        client, monkeypatch, tmp_path):
    """The loud-dead bar survives: broken slot + empty store = refusal (the same
    wire shape as pre-fallback)."""
    d = write_config_dir(tmp_path, [_agent("lkg-agent", "marin", LKG_MODEL)])
    _write_pointer(d, inbound="ghost-agent")
    _point_env(monkeypatch, d)

    events = []
    urls = _drive(client, monkeypatch, FakeOpenAIWS(), capture_events=events)
    assert urls == []
    assert [e for e in events if e.get("type") == "fallback"] == []


def test_outbound_gate_falls_back_to_the_snapshot(client, monkeypatch, tmp_path):
    """The pre-dial gate grades the profile the call will actually run: with the
    outbound slot broken, the snapshot's deny-all number policy refuses the dial
    (403), not the activation failure (500)."""
    d = write_config_dir(tmp_path, [
        _agent("lkg-agent", "marin", LKG_MODEL,
               number_policy={"allow": ["+61000000000"]})])
    _write_pointer(d, inbound=None, outbound="lkg-agent")
    _recorded_profile(d, monkeypatch, direction="outbound")
    _write_pointer(d, inbound=None, outbound="ghost-agent")
    _point_env(monkeypatch, d)

    r = client.post("/voice/outbound",
                    json={"brief": "b", "to": "+61491570156"},
                    headers={"authorization": "Bearer test-token"})
    assert r.status_code == 403
    assert "not in the outbound allow-list" in r.json()["error"]


def test_outbound_gate_without_snapshot_keeps_the_activation_refusal(
        client, monkeypatch, tmp_path):
    d = write_config_dir(tmp_path, [_agent("lkg-agent", "marin", LKG_MODEL)])
    _write_pointer(d, inbound=None, outbound="ghost-agent")
    _point_env(monkeypatch, d)

    r = client.post("/voice/outbound",
                    json={"brief": "b", "to": "+61491570156"},
                    headers={"authorization": "Bearer test-token"})
    assert r.status_code == 500
    assert "failed to load" in r.json()["error"]


# ---------------------------------------------------------------------------
# The cascade lane records too (outbound-only, clean end only)
# ---------------------------------------------------------------------------

class _FakeCascadeSession:
    """Stands in for CascadeLiveSession inside _run_cascade_call: run() is the whole
    call (no audio pipeline), teardown just records the outcome."""

    stt_lost = False

    def __init__(self, *a, **kw):
        self._tools_enabled = False
        self.transcript = []

    async def run(self):
        return None

    async def teardown(self, outcome="ok"):
        return None


class _CrashingCascadeSession(_FakeCascadeSession):
    async def run(self):
        raise RuntimeError("provider died mid-call")


class _DeafCascadeSession(_FakeCascadeSession):
    stt_lost = True                   # the STT session died and could not come back


def _fake_cascade_lane(monkeypatch, *, session_cls):
    monkeypatch.setattr(server.cascade_config, "build_cascade_config",
                        lambda *a, **k: {
                            "stt": {"wired_live": True, "provider": "deepgram",
                                    "secret_env": "DG_KEY", "model": None,
                                    "language": None, "keyterms": None},
                            "tts": {"wired_live": True, "provider": "elevenlabs"}})
    monkeypatch.setattr(server.cascade_live, "open_stt", lambda *a, **k: None)
    monkeypatch.setattr(server.cascade_live, "CascadeLiveSession", session_cls)

    async def _no_deliver(mission, transcript):
        return None

    monkeypatch.setattr(server, "deliver_transcript", _no_deliver)


def _drive_outbound_cascade(client, monkeypatch):
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(server.eventlog, "append_event", lambda *a, **k: None)
    server._remember_mission("cid-cas",
                             server.OutboundMission(brief="b", to="+15550002222"))
    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MCs3",
                      "start": {"streamSid": "MCs3", "callSid": "CAx",
                                "customParameters": {"call_id": "cid-cas"}}})
        ws.send_json({"event": "stop", "sequenceNumber": "2", "streamSid": "MCs3"})


def test_cascade_call_that_completed_records_the_outbound_snapshot(
        client, monkeypatch, tmp_path):
    """The cascade lane rides the same record rule: a clean end records the phone
    outlet's outbound snapshot (removing the record makes this test red)."""
    d = write_config_dir(tmp_path, [_agent("cas-agent", "marin", LKG_MODEL,
                                           pipeline="cascade")])
    _write_pointer(d, inbound=None, outbound="cas-agent")
    _point_env(monkeypatch, d)
    _fake_cascade_lane(monkeypatch, session_cls=_FakeCascadeSession)

    _drive_outbound_cascade(client, monkeypatch)

    snap = lkg.load(profiles.OUTLET_PHONE, "outbound")
    assert snap is not None and snap.profile.agent_id == "cas-agent"


def test_cascade_call_that_crashed_records_nothing(
        client, monkeypatch, tmp_path):
    """A cascade call that died mid-run is NOT a known-good configuration."""
    d = write_config_dir(tmp_path, [_agent("cas-agent", "marin", LKG_MODEL,
                                           pipeline="cascade")])
    _write_pointer(d, inbound=None, outbound="cas-agent")
    _point_env(monkeypatch, d)
    _fake_cascade_lane(monkeypatch, session_cls=_CrashingCascadeSession)

    _drive_outbound_cascade(client, monkeypatch)

    assert lkg.load(profiles.OUTLET_PHONE, "outbound") is None


def test_cascade_call_whose_stt_was_lost_records_nothing(client, monkeypatch, tmp_path):
    """Ticket 21 (review S2): a call that went deaf ended "ok" but was never heard to
    its end, so it is not proof the configuration works. Sabotage: drop the
    ``session.stt_lost`` check at the phone record site - a snapshot is recorded, red."""
    d = write_config_dir(tmp_path, [_agent("cas-agent", "marin", LKG_MODEL,
                                           pipeline="cascade")])
    _write_pointer(d, inbound=None, outbound="cas-agent")
    _point_env(monkeypatch, d)
    _fake_cascade_lane(monkeypatch, session_cls=_DeafCascadeSession)

    _drive_outbound_cascade(client, monkeypatch)

    assert lkg.load(profiles.OUTLET_PHONE, "outbound") is None
