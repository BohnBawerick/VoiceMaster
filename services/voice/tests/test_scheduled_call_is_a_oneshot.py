"""Ticket 11: a Call the scheduler placed is a one-shot, at the bridge.

The dashboard half of this lives in
``services/voice-control/tests/test_scheduler_fire.py``: a fired Schedule goes
through the one placement and so produces the one dial body. This is the other
half — that THAT body really does take the one-shot path here, and that a whole
call down it leaves the phone Outlet's last-known-good exactly as it was.

The two halves are joined by ``voicecore.dial_request.build_dial_payload``: the
dashboard builds the request with it and this test drives ``/voice/outbound``
with it, so a change to the wire shape moves both sides at once. A hand-copied
body here would go on passing while the dashboard sent something else — which is
this repo's most expensive recurring bug, not a hypothetical.

Ticket 09 already proved the guard for a snapshot handed straight to
``_remember_mission`` (``test_oneshot_outbound.py``). This starts one step
earlier, at the HTTP request, because that is where a scheduled Call enters.
"""
import pytest
import yaml
from fastapi.testclient import TestClient

import server
from outbound import OutboundMission
from profile_helpers import profile_doc, write_config_dir
from voicecore import lkg
from voicecore import profiles
from voicecore.dial_request import DIAL_PATH, build_dial_payload

from conftest import FakeOpenAIWS

OWNER = "+61491570156"
OTHER = "+61899990000"
MODEL_ASSIGNED = "gpt-realtime-assigned"
MODEL_SCHEDULED = "gpt-realtime-scheduled"


def _agent(agent_id, model):
    return profile_doc(id=agent_id, knobs={"voice": "marin", "model": model})


def _write_pointer(directory, outbound=None):
    (directory / "active.yaml").write_text(yaml.safe_dump({
        "outlets": {"phone": {"inbound": None, "outbound": outbound},
                    "talk": {"inbound": None, "outbound": None}}}))


def _fake_twilio(monkeypatch):
    placed = []

    class FakeCall:
        sid = "CAsched1"

    class FakeCalls:
        def create(self, **kw):
            placed.append(kw)
            return FakeCall()

    class FakeClient:
        def __init__(self, *a, **kw):
            self.calls = FakeCalls()

    monkeypatch.setattr(server, "Client", FakeClient)
    return placed


def _drive_to_teardown(client, monkeypatch, call_id):
    """One outbound media_stream session, start to clean teardown."""
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


@pytest.fixture
def client():
    server._MISSIONS.clear()
    server._OUTBOUND_SNAPSHOTS.clear()
    server._ACTIVE_OUTBOUND.clear()
    return TestClient(server.app)


@pytest.fixture
def wired(monkeypatch, tmp_path):
    directory = write_config_dir(tmp_path, [
        _agent("agent-assigned", MODEL_ASSIGNED),
        _agent("agent-scheduled", MODEL_SCHEDULED)])
    _write_pointer(directory, outbound="agent-assigned")
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(directory))
    monkeypatch.delenv("VOICE_AGENT", raising=False)
    return directory


def _place(client, monkeypatch, **kw):
    """POST the dial the dashboard builds, and return the bridge's call_id."""
    _fake_twilio(monkeypatch)
    payload = build_dial_payload(**kw)
    response = client.post(DIAL_PATH, json=payload,
                           headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200, response.text
    return response.json()["call_id"]


def test_a_scheduled_dial_answers_as_its_own_agent_not_the_assigned_one(
        client, wired, monkeypatch):
    """The Agent on a scheduled Call is the one the Schedule named, even though
    a different Agent is assigned to phone outbound."""
    call_id = _place(client, monkeypatch, to=OTHER, mission="Ask about Friday.",
                     agent="agent-scheduled", disclose=True)
    urls = _drive_to_teardown(client, monkeypatch, call_id)
    assert urls == [f"wss://api.openai.com/v1/realtime?model={MODEL_SCHEDULED}"]


def test_a_scheduled_call_does_not_become_the_outlets_known_good(
        client, wired, monkeypatch):
    """THE ticket 09 invariant, for a Call that nobody was watching.

    A Schedule fires at 3am, as an Agent that is not the assigned one. If its
    teardown recorded a last-known-good, the phone Outlet would answer as that
    Agent the next time its assignment broke — a per-call binding silently
    promoted to a configuration of the Outlet, by a call the owner slept
    through. Remove the ``oneshot_snapshot is None`` guard in server.py and this
    goes red twice: on the snapshot file and on what a broken assignment
    resolves to.
    """
    # An honest known-good first: the ASSIGNED Agent completes a call on the
    # Hermes-skill path (no ``agent`` on the request), which is the only way a
    # phone/outbound snapshot is ever written.
    server._remember_mission("cid-assigned",
                             OutboundMission(brief="assigned call", to=OWNER))
    _drive_to_teardown(client, monkeypatch, "cid-assigned")
    seeded = lkg.load(profiles.OUTLET_PHONE, "outbound")
    assert seeded is not None and seeded.profile.agent_id == "agent-assigned"
    snapshot = lkg.snapshot_path(profiles.OUTLET_PHONE, "outbound")
    seeded_bytes = snapshot.read_bytes()

    # Now the scheduled one, as a different Agent, start to clean teardown.
    scheduled_id = _place(client, monkeypatch, to=OTHER,
                          mission="Ask if Friday still works.",
                          agent="agent-scheduled", disclose=True)
    _drive_to_teardown(client, monkeypatch, scheduled_id)

    after = lkg.load(profiles.OUTLET_PHONE, "outbound")
    assert after is not None and after.profile.agent_id == "agent-assigned"
    assert snapshot.read_bytes() == seeded_bytes

    # And the consequence that would actually be felt: with the assignment
    # broken, the Outlet still falls back to the assigned Agent, never to
    # whoever a Schedule last placed.
    _write_pointer(wired, outbound="ghost-agent")
    resolved = lkg.resolve("outbound", outlet=profiles.OUTLET_PHONE)
    assert resolved is not None and resolved.agent_id == "agent-assigned"


def test_the_scheduled_dial_carries_the_mission_and_the_disclosure_choice(
        client, wired, monkeypatch):
    """Both per-call things ticket 09 made per-call travel on the same body."""
    call_id = _place(client, monkeypatch, to=OTHER,
                     mission="Ask the plumber about Tuesday.",
                     agent="agent-scheduled", disclose=False,
                     target_display="The plumber")
    mission = server._MISSIONS[call_id][1]
    assert mission.brief == "Ask the plumber about Tuesday."
    assert mission.disclose is False
    assert mission.to == OTHER
    assert mission.target_display == "The plumber"
    assert server._OUTBOUND_SNAPSHOTS[call_id].agent_id == "agent-scheduled"


def test_the_dial_body_is_the_one_the_dashboard_builds():
    """The seam itself: the keys this bridge reads are the keys that module
    writes. If ``brief`` were renamed on one side only, every behavioural test
    above would still pass against its own hand-written body — so the shape is
    asserted here, once, against the shared builder."""
    payload = build_dial_payload(to=OTHER, mission="M", agent="agent-scheduled",
                                 disclose=True, target_display="Someone")
    assert payload == {"to": OTHER, "brief": "M", "agent": "agent-scheduled",
                       "disclose": True, "target_display": "Someone"}
    assert DIAL_PATH == "/voice/outbound"
