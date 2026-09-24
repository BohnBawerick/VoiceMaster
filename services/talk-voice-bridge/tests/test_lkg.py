"""s8 tests, Mode V: the last-known-good answering fallback on the TALK call path.

The shared ``voicecore.lkg`` contracts (record/load round-trip, isolation, loudness,
pointer-untouched, env precedence) live ONCE in the phone suite's ``test_lkg.py`` -
both suites import the same module, and one copy of the matrix is the point of the
shared package (VC17). What lives HERE is what only this bridge can prove:
CallSession.start and the /call/outbound gate fall back on the TALK outlet's snapshot
when the talk slot breaks, teardown records it, and a phone-outlet snapshot never
answers a Talk call.

Every behavioural claim here was sabotage-checked: remove the fallback and the
broken-slot answer tests go red (the call is refused), remove the teardown record and
the store stays empty, point the fallback at the phone store and the isolation test
goes red.
"""
import json

import pytest
import yaml
from fastapi.testclient import TestClient

import config
import outbound as outbound_mod
from voicecore import lkg
from voicecore import profiles
import realtime_bridge
import server
import session as session_mod
from approval import ApprovalStore
from outbound import OutboundMission
from session import CallSession

from profile_helpers import profile_doc, write_config_dir

PHONE_MODEL = "gpt-realtime-probe-phone"
TALK_MODEL = "gpt-realtime-probe-talk"
LKG_MODEL = "gpt-realtime-probe-lkg"


def _agent(aid, voice, model, **overrides):
    return profile_doc(id=aid, knobs={"voice": voice, "model": model}, **overrides)


def _write_pointer(d, *, phone=None, talk=None):
    doc = {"outlets": {}}
    if phone is not None:
        doc["outlets"]["phone"] = phone
    if talk is not None:
        doc["outlets"]["talk"] = talk
    (d / "active.yaml").write_text(yaml.safe_dump(doc))
    return d / "active.yaml"


def _point_env(monkeypatch, d, voice_agent=None):
    for var in ("VOICE_AGENT", "VOICE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(d))
    if voice_agent is not None:
        monkeypatch.setenv("VOICE_AGENT", voice_agent)


class FakeBrowser:
    async def join_call(self, token):
        return None

    async def start_call(self, token):
        return None

    async def leave_call(self):
        return None


class FakeRealtimeWS:
    """Async CM + async-iterable: yields no events, records sends."""

    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        return None

    def __aiter__(self):
        async def _gen():
            if False:  # pragma: no cover - no events needed for these assertions
                yield None
        return _gen()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def _drive(monkeypatch, *, mission=None, capture_events=None):
    """One full Mode V call setup through CallSession.start."""
    urls = []
    fake = FakeRealtimeWS()

    def connect(url, **kw):
        urls.append(url)
        return fake

    monkeypatch.setattr(realtime_bridge.websockets, "connect", connect)
    monkeypatch.setattr(realtime_bridge, "parec_cmd", lambda rate: ["true"])
    monkeypatch.setattr(realtime_bridge, "pacat_cmd", lambda rate: ["cat"])
    if capture_events is None:
        monkeypatch.setattr(lkg.eventlog, "append_event", lambda *a, **k: None)
    else:
        monkeypatch.setattr(lkg.eventlog, "append_event",
                            lambda obj, *a, **k: capture_events.append(obj))

    async def _no_deliver(cfg, mission_, lines):
        return None

    monkeypatch.setattr(outbound_mod, "deliver_transcript", _no_deliver)
    sess = CallSession(config.load_base(), FakeBrowser(), ApprovalStore())
    ok = await sess.start("tok-s8", "owner", "owner", "Owner", mission=mission)
    if ok and sess._run_task is not None:
        await sess._run_task
        await sess._reconcile_task
    return urls, fake, ok


def _session_update(fake):
    ups = [m for m in fake.sent if m.get("type") == "session.update"]
    assert len(ups) == 1, f"expected exactly one session.update, got {len(ups)}"
    return ups[0]["session"]


@pytest.mark.asyncio
async def test_talk_call_records_the_snapshot_at_teardown(monkeypatch, tmp_path):
    """A call that completed on the Talk outlet leaves its snapshot in the talk slot
    - the wire-level record pin (removing the teardown record makes this red)."""
    d = write_config_dir(tmp_path, [_agent("talk-agent", "cedar", TALK_MODEL)])
    _write_pointer(d, talk={"inbound": "talk-agent", "outbound": None})
    _point_env(monkeypatch, d)

    urls, fake, ok = await _drive(monkeypatch)
    assert ok and urls == [f"wss://api.openai.com/v1/realtime?model={TALK_MODEL}"]

    snap = lkg.load(profiles.OUTLET_TALK, "inbound")
    assert snap is not None and snap.profile.agent_id == "talk-agent"
    assert lkg.load(profiles.OUTLET_PHONE, "inbound") is None


@pytest.mark.asyncio
async def test_talk_call_answers_with_the_snapshot_when_the_slot_breaks(
        monkeypatch, tmp_path):
    """THE sabotage-checked claim: with a recorded snapshot, breaking the talk slot
    must NOT refuse the call - it answers with the snapshot, loudly. (Reverting the
    lkg.resolve call at CallSession.start makes this test red: ok stays False.)"""
    d = write_config_dir(tmp_path, [_agent("talk-agent", "cedar", LKG_MODEL)])
    pointer = _write_pointer(d, talk={"inbound": "talk-agent", "outbound": None})
    _point_env(monkeypatch, d)

    # First call: healthy, completes, records the snapshot.
    urls, fake, ok = await _drive(monkeypatch)
    assert ok and urls == [f"wss://api.openai.com/v1/realtime?model={LKG_MODEL}"]
    assert lkg.load(profiles.OUTLET_TALK, "inbound") is not None

    # The slot breaks after storage.
    _write_pointer(d, talk={"inbound": "ghost-agent", "outbound": None})
    _point_env(monkeypatch, d)

    events = []
    urls, fake, ok = await _drive(monkeypatch, capture_events=events)
    assert ok
    assert urls == [f"wss://api.openai.com/v1/realtime?model={LKG_MODEL}"]
    assert _session_update(fake)["audio"]["output"]["voice"] == "cedar"

    falls = [e for e in events if e.get("type") == "fallback"]
    assert len(falls) == 1
    assert falls[0]["outlet"] == "talk"
    assert falls[0]["direction"] == "inbound"
    assert falls[0]["broken_agent"] == "ghost-agent"
    assert falls[0]["snapshot_agent_id"] == "talk-agent"
    # The dashboard still shows the slot broken - the pointer is untouched.
    assert pointer.read_text() == yaml.safe_dump(
        {"outlets": {"talk": {"inbound": "ghost-agent", "outbound": None}}})


@pytest.mark.asyncio
async def test_broken_talk_slot_without_snapshot_still_refuses(monkeypatch, tmp_path):
    """The loud-dead bar survives: broken slot + empty store = refusal (the same
    shape as pre-fallback)."""
    d = write_config_dir(tmp_path, [_agent("talk-agent", "cedar", TALK_MODEL)])
    _write_pointer(d, talk={"inbound": "ghost-agent", "outbound": None})
    _point_env(monkeypatch, d)

    urls, fake, ok = await _drive(monkeypatch)
    assert ok is False and urls == [] and fake.sent == []


@pytest.mark.asyncio
async def test_phone_snapshot_never_answers_the_talk_outlet(monkeypatch, tmp_path):
    """Per-outlet isolation on the wire: a snapshot recorded for the PHONE outlet
    must not arm the talk outlet's fallback - the talk call still refuses."""
    d = write_config_dir(tmp_path, [_agent("talk-agent", "cedar", TALK_MODEL),
                                    _agent("phone-agent", "marin", PHONE_MODEL)])
    _write_pointer(d,
                   phone={"inbound": "phone-agent", "outbound": None},
                   talk={"inbound": "talk-agent", "outbound": None})
    _point_env(monkeypatch, d)
    profile = profiles.load_effective_profile(
        "inbound", outlet=profiles.OUTLET_PHONE)
    lkg.record(profiles.OUTLET_PHONE, "inbound", profile, call_id="phone-call")

    _write_pointer(d, talk={"inbound": "ghost-agent", "outbound": None})
    _point_env(monkeypatch, d)

    urls, fake, ok = await _drive(monkeypatch)
    assert ok is False and urls == [] and fake.sent == []


def test_outbound_gate_falls_back_to_the_snapshot(monkeypatch, tmp_path):
    """/call/outbound's talk_policy gate must grade the profile the call will
    actually run: with the outbound slot broken, the snapshot's deny-all gate
    refuses the dial (403), not the activation failure (500)."""
    d = write_config_dir(tmp_path, [
        _agent("talk-agent", "cedar", TALK_MODEL, talk_policy={"allow": []})])
    _write_pointer(d, talk={"inbound": None, "outbound": "talk-agent"})
    _point_env(monkeypatch, d)
    profile = profiles.load_effective_profile("outbound", outlet=profiles.OUTLET_TALK)
    lkg.record(profiles.OUTLET_TALK, "outbound", profile, call_id="talk-call")
    _write_pointer(d, talk={"inbound": None, "outbound": "ghost-agent"})
    _point_env(monkeypatch, d)

    client = TestClient(server.app, headers={
        "Authorization": f"Bearer {server._cfg.hermes_gateway_token}"})
    r = client.post("/call/outbound", json={"brief": "hi", "token": "anyroom"})
    assert r.status_code == 403
    assert "talk_policy" in r.json()["error"]


def test_outbound_gate_without_snapshot_keeps_the_activation_refusal(
        monkeypatch, tmp_path):
    d = write_config_dir(tmp_path, [_agent("talk-agent", "cedar", TALK_MODEL)])
    _write_pointer(d, talk={"inbound": None, "outbound": "ghost-agent"})
    _point_env(monkeypatch, d)

    client = TestClient(server.app, headers={
        "Authorization": f"Bearer {server._cfg.hermes_gateway_token}"})
    r = client.post("/call/outbound", json={"brief": "hi", "token": "anyroom"})
    assert r.status_code == 500
    assert "failed to load" in r.json()["error"]


@pytest.mark.asyncio
async def test_a_talk_call_whose_stt_was_lost_records_nothing(monkeypatch, tmp_path):
    """Ticket 21 (review S2): a bridge whose STT session died for good ran deaf; it is
    not a known-good configuration. The rule sits in CallSession's teardown and reads
    the bridge's `stt_lost`. Sabotage: drop that condition - a snapshot is recorded, red."""
    d = write_config_dir(tmp_path, [_agent("talk-agent", "cedar", TALK_MODEL)])
    _write_pointer(d, talk={"inbound": "talk-agent", "outbound": None})
    _point_env(monkeypatch, d)
    monkeypatch.setattr(realtime_bridge.RealtimeBridge, "stt_lost", True, raising=False)

    urls, fake, ok = await _drive(monkeypatch)
    assert ok
    assert lkg.load(profiles.OUTLET_TALK, "inbound") is None


def test_the_cascade_bridges_say_when_their_stt_was_lost():
    import cascade_bridge

    class Session:
        stt_lost = True

    bridge = cascade_bridge.CascadeBridge.__new__(cascade_bridge.CascadeBridge)
    bridge._session = Session()
    assert bridge.stt_lost is True
    direct = cascade_bridge.DirectLaneBridge.__new__(cascade_bridge.DirectLaneBridge)
    direct._inner = None
    assert direct.stt_lost is False              # nothing answered yet
    direct._inner = bridge
    assert direct.stt_lost is True
