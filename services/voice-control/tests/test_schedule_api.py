"""Ticket 11: the Schedule API — the contract Hermes and the screen both use.

VC14: Hermes may create a Schedule, but it is never responsible for remembering
it. So the test that matters most here is that a Schedule created through the
API survives with no help from its creator and with no help from the process
that took the request — ``test_a_schedule_created_through_the_api_is_remembered_
by_this_app``.

Firing is in ``test_scheduler_fire.py``; this file is the HTTP surface around it.
"""
from datetime import timedelta

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

import schedules
from conftest import RecordingTransport, SentinelTransport
from hindsight_producer_fixtures import as_store_returns, phone_outbound_v5

OWNER = "+61491570156"
PERTH = "Australia/Perth"
SYDNEY = "Australia/Sydney"

AGENT = {
    "id": "agent-a",
    "description": "the scheduled one",
    "enabled": True,
    "pipeline": "realtime",
    "providers": {"realtime": "openai-gpt-realtime"},
}

DISABLED = dict(AGENT, id="agent-off", enabled=False)


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_GATEWAY_TOKEN", "gw-token")
    monkeypatch.setenv("VOICE_MODE_C_URL", "http://127.0.0.1:3336")
    monkeypatch.setenv("VOICE_TIMEZONE", PERTH)
    (tmp_path / "agents").mkdir()
    for doc in (AGENT, DISABLED):
        (tmp_path / "agents" / f"{doc['id']}.yaml").write_text(yaml.safe_dump(doc))
    (tmp_path / "active.yaml").write_text(yaml.safe_dump({
        "outlets": {"phone": {"inbound": None, "outbound": None},
                    "talk": {"inbound": None, "outbound": None}}}))
    return tmp_path


@pytest.fixture
def client(make_app):
    """A dashboard that would trip loudly on any network call: creating,
    listing and cancelling a Schedule must not touch the phone bridge."""
    return TestClient(make_app(SentinelTransport()))


def _body(at="2026-08-20T15:00:00", tz=PERTH, agent="agent-a", to=OWNER,
          mission="Ask if Friday still works.", disclose=False, **extra):
    body = {"agent": agent, "to": to, "mission": mission, "disclose": disclose,
            **extra}
    if at is not None:
        body["at"] = at
    if tz is not None:
        body["tz"] = tz
    return body


def _future(days=30, hour=15):
    """A wall-clock time comfortably ahead of whenever the suite runs."""
    day = (schedules.now_utc() + timedelta(days=days)).date().isoformat()
    return f"{day}T{hour:02d}:00:00"


# -- creating --------------------------------------------------------------

def test_creating_a_schedule_answers_201_and_the_whole_record(client, config):
    created = client.post("/api/schedules", json=_body(
        at=_future(), mission="Ask the plumber about Tuesday.", disclose=True))
    assert created.status_code == 201, created.text
    record = created.json()
    assert schedules.is_schedule_id(record["id"])
    assert record["status"] == "pending"
    assert record["agent"] == "agent-a"
    assert record["to"] == OWNER
    assert record["mission"] == "Ask the plumber about Tuesday."
    assert record["disclose"] is True
    assert record["timezone"] == PERTH
    assert record["local_time"] == _future()
    assert record["due_at"].endswith("Z")
    assert record["created_at"].endswith("Z")


def test_a_schedule_created_through_the_api_is_remembered_by_this_app(
        client, config, make_app):
    """VC14: Hermes creates it and forgets it; the memory is this app's.

    The record is read back by a DIFFERENT app instance — the creating process
    is gone as far as this assertion is concerned — from the disk it was
    written to.
    """
    created = client.post("/api/schedules", json=_body(at=_future())).json()
    assert (config / "schedules" / f"{created['id']}.yaml").is_file()

    restarted = TestClient(make_app(SentinelTransport()))
    listed = restarted.get("/api/schedules").json()["schedules"]
    assert [r["id"] for r in listed] == [created["id"]]
    assert restarted.get(f"/api/schedules/{created['id']}").json() == created


def test_a_number_is_normalized_the_way_a_placed_call_normalizes_it(client, config):
    record = client.post("/api/schedules",
                         json=_body(at=_future(), to="+61 491 570 156")).json()
    assert record["to"] == OWNER


def test_the_zone_may_be_left_to_the_service_default(client, config):
    """The deployment sets TZ; VOICE_TIMEZONE overrides it. A caller that sends a bare
    local time gets the service's zone, named back in the record so there is no
    doubt which it used."""
    record = client.post("/api/schedules",
                         json=_body(at=_future(), tz=None)).json()
    assert record["timezone"] == PERTH


def test_a_caller_may_name_a_different_zone(client, config):
    record = client.post("/api/schedules",
                         json=_body(at="2026-12-20T15:00:00", tz=SYDNEY)).json()
    assert record["timezone"] == SYDNEY
    assert record["due_at"] == "2026-12-20T04:00:00Z"     # AEDT, +11


def test_a_local_time_that_does_not_exist_is_refused_with_a_reason(client, config):
    refused = client.post("/api/schedules",
                          json=_body(at="2026-10-04T02:30:00", tz=SYDNEY))
    assert refused.status_code == 422
    assert "does not exist" in refused.text
    assert schedules.load_all() == []


def test_a_time_in_the_past_is_refused(client, config):
    past = schedules.to_iso(schedules.now_utc() - timedelta(hours=2))
    refused = client.post("/api/schedules", json=_body(at=past, tz=None))
    assert refused.status_code == 422
    assert "in the past" in refused.text
    assert schedules.load_all() == []


def test_a_time_a_moment_ago_is_accepted_because_clocks_differ(client, config):
    """The browser computes the local time from its own clock. A minute of skew
    must not turn "in five minutes" into a refusal."""
    nearly_now = schedules.to_iso(schedules.now_utc() - timedelta(seconds=30))
    created = client.post("/api/schedules", json=_body(at=nearly_now, tz=None))
    assert created.status_code == 201, created.text


def test_a_missing_time_is_refused(client, config):
    refused = client.post("/api/schedules", json=_body(at=None, tz=None))
    assert refused.status_code == 422
    assert "at" in refused.text


def test_a_disabled_agent_is_refused_when_the_schedule_is_written(client, config):
    """Refused now, to whoever is asking, rather than at 3am to nobody."""
    refused = client.post("/api/schedules",
                          json=_body(at=_future(), agent="agent-off"))
    assert refused.status_code == 409
    assert "enabled: false" in refused.text
    assert schedules.load_all() == []


def test_a_body_that_is_not_an_object_is_refused(client, config):
    assert client.post("/api/schedules", content="not json").status_code == 422
    assert client.post("/api/schedules", json=[1, 2, 3]).status_code == 422


# -- listing ---------------------------------------------------------------

def test_the_list_is_upcoming_first_and_carries_the_clock(client, config):
    later = client.post("/api/schedules", json=_body(at=_future(days=40))).json()
    sooner = client.post("/api/schedules", json=_body(at=_future(days=20))).json()
    payload = client.get("/api/schedules").json()
    assert [r["id"] for r in payload["schedules"]] == [sooner["id"], later["id"]]
    assert payload["now"].endswith("Z")
    assert payload["timezone"] == PERTH
    assert payload["grace_s"] == schedules.DEFAULT_GRACE_S


def test_an_empty_schedule_list_is_an_empty_list(client, config):
    assert client.get("/api/schedules").json()["schedules"] == []


def test_one_schedule_can_be_fetched_by_id(client, config):
    created = client.post("/api/schedules", json=_body(at=_future())).json()
    assert client.get(f"/api/schedules/{created['id']}").json() == created
    assert client.get("/api/schedules/sch-000000000000").status_code == 404


# -- cancelling ------------------------------------------------------------

def test_cancelling_removes_it_from_upcoming_and_says_why(client, config):
    created = client.post("/api/schedules", json=_body(at=_future())).json()
    cancelled = client.delete(f"/api/schedules/{created['id']}")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["reason"]
    listed = client.get("/api/schedules").json()["schedules"]
    assert [r["status"] for r in listed] == ["cancelled"]


def test_cancelling_twice_is_refused_not_silently_repeated(client, config):
    created = client.post("/api/schedules", json=_body(at=_future())).json()
    assert client.delete(f"/api/schedules/{created['id']}").status_code == 200
    again = client.delete(f"/api/schedules/{created['id']}")
    assert again.status_code == 409
    assert "already cancelled" in again.text


# -- afterwards, the Call is an ordinary Call ------------------------------

def test_the_call_a_schedule_placed_is_read_from_the_store_like_any_other(
        config, make_app, monkeypatch):
    """The last link: the ``call_id`` a fired Schedule recorded is the id the
    Calls screen resolves, out of the live store's OWN document shape.

    The document here goes through ``as_store_returns`` — fields under
    ``document_metadata``, no ``metadata`` key — because that is what Hindsight
    hands back, and a fixture built from the POST body would agree with a reader
    that reads the wrong key (it did once, and cost every retained field).
    """
    async def dial(request):
        return httpx.Response(200, json={"placed": True, "call_sid": "CAsched",
                                         "call_id": "voice-twilio-sched-1"})

    transport = RecordingTransport(dial)
    application = make_app(transport)
    client = TestClient(application)

    import scheduler as call_scheduler
    created = client.post("/api/schedules", json=_body(
        at=schedules.to_iso(schedules.now_utc() - timedelta(seconds=5)), tz=None,
        mission="Book a table for 7pm.")).json()

    import asyncio
    asyncio.run(call_scheduler.Scheduler(
        transport_get=lambda: application.state.transport).tick())
    settled = client.get(f"/api/schedules/{created['id']}").json()
    assert settled["status"] == "placed"
    assert settled["call_id"] == "voice-twilio-sched-1"

    served = as_store_returns(phone_outbound_v5(
        settled["call_id"], "2026-08-19T09:00:00+00:00", agent="agent-a"))
    assert "metadata" not in served and "document_metadata" in served

    async def mock_get(self_arg, url, **kwargs):
        text = str(url)
        if "/documents/" in text and text.endswith(settled["call_id"]):
            return httpx.Response(200, json=served)
        if "/documents" in text and "/banks/voice/" in text:
            return httpx.Response(200, json={"items": [served], "total": 1})
        return httpx.Response(200, json={"items": [], "total": 0})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    monkeypatch.setenv("HINDSIGHT_URL", "http://hindsight.test:8888")   # the store mocked above
    detail = client.get(f"/api/calls/{settled['call_id']}").json()["call"]
    assert detail["call_id"] == settled["call_id"]
    assert detail["mission"] == "Book a table for 7pm."
    assert detail["agent"] == "agent-a"
    assert detail["direction"] == "outbound"
    listed = client.get("/api/calls").json()["calls"]
    assert [c["call_id"] for c in listed] == [settled["call_id"]]
