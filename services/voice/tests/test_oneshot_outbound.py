"""Ticket 09: one-shot outbound. The Agent travels WITH the call.

These tests would go red if /voice/outbound still read the pointer for a
named agent, stored the snapshot in a process-global cell, or let the
allow-list refuse a number the owner typed. The Hermes-skill path (no
``agent`` in the body) is unchanged and is covered by test_outbound.py.
"""
import yaml
import pytest
from fastapi.testclient import TestClient

import server
from outbound import OutboundMission
from profile_helpers import profile_doc, write_config_dir
from voicecore import lkg
from voicecore import profiles

OWNER = "+61491570156"
OTHER = "+61899990000"
MODEL_A = "gpt-realtime-oneshot-a"
MODEL_B = "gpt-realtime-oneshot-b"


def _agent(agent_id, model, **overrides):
    return profile_doc(id=agent_id, knobs={"voice": "marin", "model": model},
                       **overrides)


def _write_pointer(directory, outbound=None, inbound=None):
    path = directory / "active.yaml"
    path.write_text(yaml.safe_dump({
        "outlets": {
            "phone": {"inbound": inbound, "outbound": outbound},
            "talk": {"inbound": None, "outbound": None},
        }
    }))
    return path


def _point(monkeypatch, directory):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(directory))
    monkeypatch.delenv("VOICE_AGENT", raising=False)


def _fake_twilio(monkeypatch):
    captured = []

    class FakeCall:
        def __init__(self):
            self.sid = f"CA{len(captured):04d}"

    class FakeCalls:
        def create(self, **kw):
            captured.append(kw)
            return FakeCall()

    class FakeClient:
        def __init__(self, *a, **kw):
            self.calls = FakeCalls()

    monkeypatch.setattr(server, "Client", FakeClient)
    return captured


from conftest import FakeOpenAIWS  # noqa: E402


@pytest.fixture
def client():
    server._MISSIONS.clear()
    server._OUTBOUND_SNAPSHOTS.clear()
    server._ACTIVE_OUTBOUND.clear()
    return TestClient(server.app)


def _auth():
    return {"Authorization": "Bearer test-token"}


def test_oneshot_binds_the_named_agent_not_the_pointer(client, monkeypatch, tmp_path):
    """A fire that names agent-a must bind agent-a even when the pointer
    names agent-b. If the route still called lkg.resolve / load_effective_profile
    the stored snapshot would be B."""
    d = write_config_dir(tmp_path, [_agent("agent-a", MODEL_A),
                                    _agent("agent-b", MODEL_B)])
    pointer = _write_pointer(d, outbound="agent-b")
    before = pointer.read_text()
    _point(monkeypatch, d)
    _fake_twilio(monkeypatch)

    r = client.post("/voice/outbound", headers=_auth(),
                    json={"brief": "Ask about Friday.", "to": OTHER,
                          "agent": "agent-a", "disclose": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["placed"] is True
    assert body["agent"] == "agent-a"
    assert pointer.read_text() == before          # one-shot never writes the pointer

    snap = server._take_oneshot_snapshot(body["call_id"])
    assert snap is not None
    assert snap.agent_id == "agent-a"
    assert snap.doc["knobs"]["model"] == MODEL_A
    # And the mission is still there for media_stream (we only took the snapshot).
    mission = server._take_mission(body["call_id"])
    assert mission is not None and mission.brief == "Ask about Friday."
    assert mission.disclose is True
    assert mission.to == OTHER


def test_two_oneshot_calls_keep_distinct_snapshots(client, monkeypatch, tmp_path):
    """THE shared-mutable-state test.

    Place A, then place B, then take A. If the snapshot lived in a module-level
    ``_CURRENT_AGENT`` the second fire would overwrite it and A would come back
    as B. Keying by call_id is what makes this stay green; a global cell makes
    it red.
    """
    d = write_config_dir(tmp_path, [_agent("agent-a", MODEL_A),
                                    _agent("agent-b", MODEL_B)])
    pointer = _write_pointer(d, outbound="agent-b")
    before = pointer.read_text()
    _point(monkeypatch, d)
    captured = _fake_twilio(monkeypatch)

    first = client.post("/voice/outbound", headers=_auth(),
                        json={"brief": "Mission A", "to": OWNER, "agent": "agent-a"})
    second = client.post("/voice/outbound", headers=_auth(),
                         json={"brief": "Mission B", "to": OTHER, "agent": "agent-b"})
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert len(captured) == 2
    assert pointer.read_text() == before

    id_a, id_b = first.json()["call_id"], second.json()["call_id"]
    assert id_a != id_b
    # Take A AFTER B has been placed — this is the order a global cell fails.
    snap_a = server._take_oneshot_snapshot(id_a)
    snap_b = server._take_oneshot_snapshot(id_b)
    assert snap_a is not None and snap_a.agent_id == "agent-a"
    assert snap_b is not None and snap_b.agent_id == "agent-b"
    assert server._take_mission(id_a).brief == "Mission A"
    assert server._take_mission(id_b).brief == "Mission B"


def test_oneshot_skips_the_allow_list(client, monkeypatch, tmp_path):
    """Outbound stays allow-any on the one-shot path. The named agent can
    carry number_policy.allow and the env can list only the owner — a typed
    number still dials."""
    d = write_config_dir(tmp_path, [
        _agent("agent-a", MODEL_A, number_policy={"allow": [OWNER]})])
    _write_pointer(d, outbound="agent-a")
    _point(monkeypatch, d)
    captured = _fake_twilio(monkeypatch)

    r = client.post("/voice/outbound", headers=_auth(),
                    json={"brief": "hi", "to": OTHER, "agent": "agent-a"})
    assert r.status_code == 200, r.text
    assert len(captured) == 1
    assert captured[0]["to"] == OTHER


def test_oneshot_refuses_a_broken_agent_without_falling_back_to_lkg(
        client, monkeypatch, tmp_path):
    """The owner picked THIS agent. LKG of a different one would be a lie."""
    d = write_config_dir(tmp_path, [_agent("good-agent", MODEL_A)])
    _write_pointer(d, outbound="good-agent")
    _point(monkeypatch, d)
    captured = _fake_twilio(monkeypatch)

    r = client.post("/voice/outbound", headers=_auth(),
                    json={"brief": "hi", "to": OWNER, "agent": "ghost-agent"})
    assert r.status_code == 409
    assert "ghost-agent" in r.json()["error"]
    assert captured == []


def test_oneshot_refuses_a_disabled_agent(client, monkeypatch, tmp_path):
    d = write_config_dir(tmp_path, [_agent("asleep", MODEL_A, enabled=False)])
    _write_pointer(d, outbound=None)
    _point(monkeypatch, d)
    captured = _fake_twilio(monkeypatch)

    r = client.post("/voice/outbound", headers=_auth(),
                    json={"brief": "hi", "to": OWNER, "agent": "asleep"})
    assert r.status_code == 409
    assert "enabled: false" in r.json()["error"]
    assert captured == []


def test_legacy_path_without_agent_still_enforces_allow_list_and_busy(
        client, monkeypatch, tmp_path):
    """The Hermes-skill path (no agent field) is unchanged."""
    d = write_config_dir(tmp_path, [_agent("agent-a", MODEL_A)])
    _write_pointer(d, outbound="agent-a")
    _point(monkeypatch, d)
    captured = _fake_twilio(monkeypatch)

    first = client.post("/voice/outbound", headers=_auth(),
                        json={"brief": "one", "to": OWNER})
    assert first.status_code == 200, first.text
    second = client.post("/voice/outbound", headers=_auth(),
                         json={"brief": "two", "to": OWNER})
    assert second.status_code == 409
    assert "already in progress" in second.json()["error"]
    assert len(captured) == 1


def test_per_call_disclose_is_stored_on_the_mission(client, monkeypatch, tmp_path):
    d = write_config_dir(tmp_path, [_agent("agent-a", MODEL_A)])
    _write_pointer(d, outbound=None)
    _point(monkeypatch, d)
    _fake_twilio(monkeypatch)

    on = client.post("/voice/outbound", headers=_auth(),
                     json={"brief": "x", "to": OWNER, "agent": "agent-a",
                           "disclose": True})
    off = client.post("/voice/outbound", headers=_auth(),
                      json={"brief": "y", "to": OTHER, "agent": "agent-a",
                            "disclose": False})
    assert on.status_code == 200 and off.status_code == 200
    assert server._take_mission(on.json()["call_id"]).disclose is True
    assert server._take_mission(off.json()["call_id"]).disclose is False


def test_outbound_prompt_uses_the_mission_disclose_not_the_env(monkeypatch):
    """Two calls can disagree about disclosure. The env is only the fallback
    for a Hermes-skill fire that never sent the field."""
    monkeypatch.setattr(server, "AI_DISCLOSURE", False)
    on = OutboundMission(brief="x", disclose=True)
    off = OutboundMission(brief="x", disclose=False)
    unset = OutboundMission(brief="x")
    assert "automated assistant" in server._outbound_base_prompt(None, on).lower()
    assert "automated assistant" not in server._outbound_base_prompt(None, off).lower()
    assert "automated assistant" not in server._outbound_base_prompt(None, unset).lower()
    monkeypatch.setattr(server, "AI_DISCLOSURE", True)
    assert "automated assistant" in server._outbound_base_prompt(None, unset).lower()


def test_media_stream_uses_the_oneshot_snapshot_not_the_pointer(
        client, monkeypatch, tmp_path):
    """media_stream must NOT re-resolve the pointer. If it still called
    lkg.resolve the URL would carry agent-b's model."""
    d = write_config_dir(tmp_path, [_agent("agent-a", MODEL_A),
                                    _agent("agent-b", MODEL_B)])
    _write_pointer(d, outbound="agent-b")
    _point(monkeypatch, d)

    snap_a = profiles.load_named_profile("agent-a", "outbound")
    server._remember_mission(
        "cid-oneshot", OutboundMission(brief="b", to=OWNER, disclose=True),
        snapshot=snap_a)

    urls = []

    def connect(url, *a, **kw):
        urls.append(url)
        return FakeOpenAIWS()

    monkeypatch.setattr(server.websockets, "connect", connect)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(server.eventlog, "append_event", lambda *a, **k: None)

    async def _no_deliver(mission, transcript):
        return None

    monkeypatch.setattr(server, "deliver_transcript", _no_deliver)

    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZo",
                      "start": {"streamSid": "MZo", "callSid": "CAx",
                                "customParameters": {"call_id": "cid-oneshot"}}})
        ws.send_json({"event": "stop", "sequenceNumber": "2", "streamSid": "MZo"})

    assert urls == [f"wss://api.openai.com/v1/realtime?model={MODEL_A}"]


def _drive_outbound_stream(client, monkeypatch, call_id):
    """One media_stream outbound session, start to clean teardown."""
    urls = []

    def connect(url, *a, **kw):
        urls.append(url)
        return FakeOpenAIWS()

    monkeypatch.setattr(server.websockets, "connect", connect)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(server.eventlog, "append_event", lambda *a, **k: None)

    async def _no_deliver(mission, transcript):
        return None

    monkeypatch.setattr(server, "deliver_transcript", _no_deliver)

    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZo",
                      "start": {"streamSid": "MZo", "callSid": "CAx",
                                "customParameters": {"call_id": call_id}}})
        ws.send_json({"event": "stop", "sequenceNumber": "2", "streamSid": "MZo"})
    return urls


def test_oneshot_teardown_does_not_overwrite_the_outlet_lkg(
        client, monkeypatch, tmp_path):
    """A one-shot is not a configuration of the phone Outlet.

    Seed an honest LKG by completing a call as the assigned outbound Agent,
    then run a one-shot naming a different Agent through media_stream to a
    clean teardown. The snapshot file must still name the assigned Agent.
    If the guard on lkg.record is removed, the file becomes agent-b and a
    later broken assignment answers as whoever was last placed from /place.
    """
    d = write_config_dir(tmp_path, [_agent("agent-a", MODEL_A),
                                    _agent("agent-b", MODEL_B)])
    _write_pointer(d, outbound="agent-a")
    _point(monkeypatch, d)

    # Honest LKG: the assigned outbound Agent completes a call.
    server._remember_mission("cid-assigned",
                             OutboundMission(brief="assigned", to=OWNER))
    _drive_outbound_stream(client, monkeypatch, "cid-assigned")
    seeded = lkg.load(profiles.OUTLET_PHONE, "outbound")
    assert seeded is not None and seeded.profile.agent_id == "agent-a"
    snap_path = lkg.snapshot_path(profiles.OUTLET_PHONE, "outbound")
    seeded_bytes = snap_path.read_bytes()

    # One-shot naming a different Agent, clean teardown.
    snap_b = profiles.load_named_profile("agent-b", "outbound")
    server._remember_mission(
        "cid-oneshot", OutboundMission(brief="oneshot", to=OTHER),
        snapshot=snap_b)
    urls = _drive_outbound_stream(client, monkeypatch, "cid-oneshot")
    assert urls == [f"wss://api.openai.com/v1/realtime?model={MODEL_B}"]

    after = lkg.load(profiles.OUTLET_PHONE, "outbound")
    assert after is not None and after.profile.agent_id == "agent-a"
    assert snap_path.read_bytes() == seeded_bytes

    # Legacy path with the assignment broken still answers as the assigned
    # Agent, not as the one-shot.
    _write_pointer(d, outbound="ghost-agent")
    _point(monkeypatch, d)
    resolved = lkg.resolve("outbound", outlet=profiles.OUTLET_PHONE)
    assert resolved is not None and resolved.agent_id == "agent-a"


def test_oneshot_cascade_teardown_does_not_overwrite_the_outlet_lkg(
        client, monkeypatch, tmp_path):
    """Same guard on the cascade lane. A one-shot cascade Agent must not
    become the phone Outlet's outbound snapshot."""
    from test_lkg import _FakeCascadeSession, _fake_cascade_lane

    d = write_config_dir(tmp_path, [
        _agent("agent-a", MODEL_A, pipeline="cascade"),
        _agent("agent-b", MODEL_B, pipeline="cascade"),
    ])
    _write_pointer(d, outbound="agent-a")
    _point(monkeypatch, d)
    _fake_cascade_lane(monkeypatch, session_cls=_FakeCascadeSession)

    server._remember_mission("cid-assigned",
                             OutboundMission(brief="assigned", to=OWNER))
    _drive_outbound_stream(client, monkeypatch, "cid-assigned")
    seeded = lkg.load(profiles.OUTLET_PHONE, "outbound")
    assert seeded is not None and seeded.profile.agent_id == "agent-a"

    snap_b = profiles.load_named_profile("agent-b", "outbound")
    server._remember_mission(
        "cid-oneshot", OutboundMission(brief="oneshot", to=OTHER),
        snapshot=snap_b)
    _drive_outbound_stream(client, monkeypatch, "cid-oneshot")

    after = lkg.load(profiles.OUTLET_PHONE, "outbound")
    assert after is not None and after.profile.agent_id == "agent-a"
    _write_pointer(d, outbound="ghost-agent")
    _point(monkeypatch, d)
    resolved = lkg.resolve("outbound", outlet=profiles.OUTLET_PHONE)
    assert resolved is not None and resolved.agent_id == "agent-a"
