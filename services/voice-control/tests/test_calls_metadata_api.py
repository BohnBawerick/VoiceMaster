"""s5 (ticket 05): the Calls API over documents that carry real metadata.

`test_calls_hindsight_api` covers the pre-05 archive, where every one of these fields
is absent and must render as not retained. This module covers the other half:

* a post-05 document's Agent, Outlet, Mission, outcome and duration reach the API;
* the two generations sit in one list without either being coerced into the other;
* `agent=` and `outlet=` filter that list, including the "not retained" bucket, which
  is the WHOLE pre-05 archive and would otherwise be unreachable through the filters;
* the facet lists offered to the screen come from the corpus, so a filter the screen
  offers always has something behind it.

Every document comes from `hindsight_producer_fixtures`, built from the producers.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

import app as voice_app
import hindsight_calls

from hindsight_producer_fixtures import (
    phone_inbound_v5_no_agent,
    phone_outbound_v5,
    talk_inbound_v5,
    twilio_inbound,
)


@pytest.fixture
def client():
    return TestClient(voice_app.create_app())


@pytest.fixture(autouse=True)
def _hindsight_env(monkeypatch):
    """These tests read the Hindsight store, so they name one (URL-agnostic mocks serve it).

    An unset HINDSIGHT_URL now means the SQLite archive (voicecore.call_store).
    A shell with deployment secrets sourced must not change what these tests read.
    """
    monkeypatch.setenv("HINDSIGHT_URL", "http://hindsight.test:8888")
    monkeypatch.delenv("HINDSIGHT_BANK", raising=False)


def _bank_of(url) -> str:
    text = str(url)
    for bank in ("voice", "hermes"):
        if f"/banks/{bank}/" in text:
            return bank
    return ""


def _serve(monkeypatch, docs, bank="voice"):
    """Serve `docs` from one bank; every other bank answers empty."""

    async def mock_get(self_arg, url, **kwargs):
        if "/documents/" in str(url):
            wanted = str(url).rsplit("/", 1)[-1]
            for doc in docs:
                if doc["id"] == wanted:
                    return httpx.Response(200, json=doc)
            return httpx.Response(404)
        if "/documents" in str(url) and _bank_of(url) == bank:
            return httpx.Response(200, json={"items": docs, "total": len(docs)})
        return httpx.Response(200, json={"items": [], "total": 0})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)


CORPUS = [
    phone_outbound_v5("voice-twilio-out-1", "2026-08-18T09:00:00+00:00",
                      agent="hermes-main"),
    talk_inbound_v5("voice-talk-in-1", "2026-08-18T10:00:00+00:00",
                    agent="talk-answerer", duration_s=12.0),
    phone_inbound_v5_no_agent("voice-twilio-in-1", "2026-08-18T11:00:00+00:00"),
    # One document from before this ticket: no outlet, no agent, no mission.
    twilio_inbound("voice-twilio-legacy", "2026-08-17T09:05:00Z"),
]


def _by_id(calls):
    return {c["call_id"]: c for c in calls}


# --------------------------------------------------------------------------
# What a retained call now says about itself
# --------------------------------------------------------------------------


def test_every_recorded_field_reaches_the_api(client, monkeypatch):
    _serve(monkeypatch, CORPUS)
    calls = _by_id(client.get("/api/calls").json()["calls"])

    out = calls["voice-twilio-out-1"]
    assert out["agent"] == "hermes-main"
    assert out["outlet"] == "phone"
    assert out["mission"] == "Book a table for 7pm."
    assert out["outcome"] == "ok"
    assert out["duration_s"] == pytest.approx(63.4)
    assert out["direction"] == "outbound"


def test_the_outlet_is_the_one_recorded_not_one_derived_from_the_transport(
        client, monkeypatch):
    """A Talk INBOUND call and a phone INBOUND call share a direction and differ in
    Outlet. Nothing in the reader may collapse them."""
    _serve(monkeypatch, CORPUS)
    calls = _by_id(client.get("/api/calls").json()["calls"])
    assert calls["voice-talk-in-1"]["outlet"] == "talk"
    assert calls["voice-twilio-in-1"]["outlet"] == "phone"
    assert calls["voice-talk-in-1"]["direction"] == "inbound"
    assert calls["voice-twilio-in-1"]["direction"] == "inbound"


def test_an_inbound_call_has_no_mission_and_none_is_supplied(client, monkeypatch):
    _serve(monkeypatch, CORPUS)
    calls = _by_id(client.get("/api/calls").json()["calls"])
    assert calls["voice-talk-in-1"]["mission"] == ""
    # ... and the outbound one does have one, so "" is a fact about the call and not
    # about the reader.
    assert calls["voice-twilio-out-1"]["mission"]


def test_a_call_with_no_assigned_agent_reports_no_agent(client, monkeypatch):
    _serve(monkeypatch, CORPUS)
    calls = _by_id(client.get("/api/calls").json()["calls"])
    assert calls["voice-twilio-in-1"]["agent"] == ""
    assert calls["voice-twilio-in-1"]["outlet"] == "phone"   # what IS known, is known


def test_a_pre_ticket_document_keeps_its_absences(client, monkeypatch):
    """History is not lost, and it is not embellished either."""
    _serve(monkeypatch, CORPUS)
    calls = _by_id(client.get("/api/calls").json()["calls"])
    legacy = calls["voice-twilio-legacy"]
    assert legacy["transcript"]                       # still readable
    assert legacy["outlet"] == ""
    assert legacy["agent"] == ""
    assert legacy["mission"] == ""
    assert legacy["outcome"] is None
    assert legacy["duration_s"] is None


def test_a_nonsense_duration_is_not_retained_rather_than_zero(client, monkeypatch):
    """0.0 would render as a call that lasted no time. None renders as unknown."""
    doc = phone_outbound_v5("voice-twilio-bad-dur", "2026-08-18T09:00:00+00:00")
    doc["metadata"]["duration_s"] = "not a number"
    _serve(monkeypatch, [doc])
    call = client.get("/api/calls").json()["calls"][0]
    assert call["duration_s"] is None


def test_the_duration_reaches_the_detail_payload(client, monkeypatch):
    _serve(monkeypatch, CORPUS)
    detail = client.get("/api/calls/voice-twilio-out-1").json()
    assert detail["call"]["duration_s"] == pytest.approx(63.4)
    assert detail["summary"]["duration_s"] == pytest.approx(63.4)
    assert detail["summary"]["outcome"] == "ok"


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------


def test_the_facets_offered_are_the_values_present(client, monkeypatch):
    _serve(monkeypatch, CORPUS)
    body = client.get("/api/calls").json()
    assert body["agents"] == ["hermes-main", "talk-answerer",
                              hindsight_calls.UNKNOWN_FILTER]
    assert body["outlets"] == ["phone", "talk", hindsight_calls.UNKNOWN_FILTER]


def test_no_unknown_bucket_is_offered_when_every_call_has_the_field(
        client, monkeypatch):
    _serve(monkeypatch, [CORPUS[0], CORPUS[1]])
    body = client.get("/api/calls").json()
    assert hindsight_calls.UNKNOWN_FILTER not in body["outlets"]
    assert hindsight_calls.UNKNOWN_FILTER not in body["agents"]


def test_filtering_by_agent(client, monkeypatch):
    _serve(monkeypatch, CORPUS)
    body = client.get("/api/calls?agent=hermes-main").json()
    assert [c["call_id"] for c in body["calls"]] == ["voice-twilio-out-1"]
    assert body["total"] == 1
    assert body["filters"]["agent"] == "hermes-main"


def test_filtering_by_outlet(client, monkeypatch):
    _serve(monkeypatch, CORPUS)
    body = client.get("/api/calls?outlet=phone").json()
    assert sorted(c["call_id"] for c in body["calls"]) == [
        "voice-twilio-in-1", "voice-twilio-out-1"]
    assert body["total"] == 2


def test_the_two_filters_compose(client, monkeypatch):
    _serve(monkeypatch, CORPUS)
    body = client.get("/api/calls?outlet=phone&agent=hermes-main").json()
    assert [c["call_id"] for c in body["calls"]] == ["voice-twilio-out-1"]


def test_the_unknown_bucket_selects_exactly_the_calls_with_no_value(
        client, monkeypatch):
    """The pre-05 archive is the biggest bucket on this screen. It has to be
    selectable, or the filter row silently hides most of the history."""
    _serve(monkeypatch, CORPUS)
    body = client.get(
        "/api/calls", params={"outlet": hindsight_calls.UNKNOWN_FILTER}).json()
    assert [c["call_id"] for c in body["calls"]] == ["voice-twilio-legacy"]

    body = client.get(
        "/api/calls", params={"agent": hindsight_calls.UNKNOWN_FILTER}).json()
    assert sorted(c["call_id"] for c in body["calls"]) == [
        "voice-twilio-in-1", "voice-twilio-legacy"]


def test_the_facets_do_not_shrink_to_the_active_filter(client, monkeypatch):
    """A dropdown that only offered the value already chosen could not be used to
    choose another one."""
    _serve(monkeypatch, CORPUS)
    body = client.get("/api/calls?agent=hermes-main").json()
    assert body["agents"] == ["hermes-main", "talk-answerer",
                              hindsight_calls.UNKNOWN_FILTER]


def test_a_filter_matching_nothing_is_an_empty_list_not_an_error(client, monkeypatch):
    _serve(monkeypatch, CORPUS)
    body = client.get("/api/calls?agent=nobody").json()
    assert body["calls"] == [] and body["total"] == 0
    assert body["unreachable"] is False and body["error"] is None


def test_a_filtered_read_still_reports_that_a_bank_could_not_be_read(
        client, monkeypatch):
    """Filtering must not swallow the warning that the list is short.

    The filter runs after the fetch, so a bounded or failed read is exactly as
    partial as it would be unfiltered -- and has to say so, or a filtered view
    looks like the complete answer for that Agent when it is not.
    """
    async def mock_get(self_arg, url, **kwargs):
        if _bank_of(url) == "hermes":
            return httpx.Response(503)
        if "/documents" in str(url):
            return httpx.Response(200, json={"items": CORPUS, "total": len(CORPUS)})
        return httpx.Response(404)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    body = client.get("/api/calls?outlet=phone").json()
    assert body["partial"] is True
    assert "hermes" in body["warning"]
    assert [c["call_id"] for c in body["calls"]]   # what WAS read is still shown


def test_paging_counts_the_filtered_set(client, monkeypatch):
    _serve(monkeypatch, CORPUS)
    body = client.get("/api/calls?outlet=phone&page_size=1").json()
    assert body["total"] == 2 and body["has_more"] is True
    assert len(body["calls"]) == 1
