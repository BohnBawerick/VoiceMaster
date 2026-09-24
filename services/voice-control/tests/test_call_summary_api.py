"""Ticket 06: what the Calls API says about a call's summary, and about its absence.

The producer (`services/voicecore/summary.py`) can write four different things, and the
whole point of this ticket's third bar is that the reader relays which one it was rather
than flattening them into one blank cell:

* a summary the Agent on the call wrote;
* ``nothing_to_summarise`` - the call held no conversation to describe (a fact about the
  call, like an inbound call having no Mission);
* ``unavailable`` - the Agent was asked and could not answer (a gap in the record);
* neither key - nobody was asked.

There is no fifth "still being written" state to relay: the summary is settled before the
call's document is written, so a call the API can see is a call whose summary question is
already closed. That is asserted here as a property of the reader - it never invents a
pending state - and the screen tests assert what the three absences look like.

Every document comes from `hindsight_producer_fixtures`, built from the producers.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

import app as voice_app

from hindsight_producer_fixtures import (
    summary_never_asked_v5,
    summary_nothing_to_say_v5,
    summary_unavailable_v5,
    summary_written_v5,
    twilio_inbound,
)


@pytest.fixture
def client():
    return TestClient(voice_app.create_app())


@pytest.fixture(autouse=True)
def _hindsight_env(monkeypatch):
    # These tests read the Hindsight store; an unset HINDSIGHT_URL now means the
    # SQLite archive (voicecore.call_store). The mocks are URL-agnostic.
    monkeypatch.setenv("HINDSIGHT_URL", "http://hindsight.test:8888")
    monkeypatch.delenv("HINDSIGHT_BANK", raising=False)


def _bank_of(url) -> str:
    text = str(url)
    for bank in ("voice", "hermes"):
        if f"/banks/{bank}/" in text:
            return bank
    return ""


def _serve(monkeypatch, docs, bank="voice"):
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
    summary_written_v5("voice-twilio-sum-written", "2026-08-18T09:00:00+00:00"),
    summary_nothing_to_say_v5("voice-twilio-sum-nothing", "2026-08-18T10:00:00+00:00"),
    summary_unavailable_v5("voice-twilio-sum-broken", "2026-08-18T11:00:00+00:00"),
    summary_never_asked_v5("voice-twilio-sum-unasked", "2026-08-18T12:00:00+00:00"),
    # A pre-06 document, which is the same case as "nobody was asked".
    twilio_inbound("voice-twilio-legacy", "2026-08-17T09:05:00Z"),
]


def _by_id(calls):
    return {c["call_id"]: c for c in calls}


def test_a_written_summary_reaches_the_list_verbatim(client, monkeypatch):
    _serve(monkeypatch, CORPUS)
    call = _by_id(client.get("/api/calls").json()["calls"])["voice-twilio-sum-written"]
    assert call["summary"] == (
        "Chased the Tuesday delivery; it shipped Monday and lands tomorrow.")
    assert call["summary_state"] == "written"


def test_the_three_absences_stay_three_different_answers(client, monkeypatch):
    """The bar: the reader must not flatten "why there is none" into one blank."""
    _serve(monkeypatch, CORPUS)
    calls = _by_id(client.get("/api/calls").json()["calls"])

    nothing = calls["voice-twilio-sum-nothing"]
    assert nothing["summary"] is None
    assert nothing["summary_state"] == "nothing_to_summarise"

    broken = calls["voice-twilio-sum-broken"]
    assert broken["summary"] is None
    assert broken["summary_state"] == "unavailable"

    unasked = calls["voice-twilio-sum-unasked"]
    assert unasked["summary"] is None
    assert unasked["summary_state"] is None

    # ...and the three really are distinguishable from each other, not just from a
    # written one.
    assert len({nothing["summary_state"], broken["summary_state"],
                str(unasked["summary_state"])}) == 3


def test_a_pre_ticket_06_call_claims_nothing_either_way(client, monkeypatch):
    """The whole archive up to this ticket. Absent is absent; nothing is inferred."""
    _serve(monkeypatch, CORPUS)
    legacy = _by_id(client.get("/api/calls").json()["calls"])["voice-twilio-legacy"]
    assert legacy["summary"] is None
    assert legacy["summary_state"] is None


def test_the_reader_never_invents_a_pending_state(client, monkeypatch):
    """No call is ever reported as "summary still coming".

    It is not a state the producer can write - the summary is settled before the document
    exists - so a screen that showed a spinner would be waiting for something that is
    never going to arrive.
    """
    _serve(monkeypatch, CORPUS)
    states = {c["summary_state"] for c in client.get("/api/calls").json()["calls"]}
    assert states == {"written", "nothing_to_summarise", "unavailable", None}


def test_the_call_detail_view_carries_the_summary_and_its_state(client, monkeypatch):
    """Additive: the detail view keeps everything tickets 02/05/07 put on it."""
    _serve(monkeypatch, CORPUS)

    written = client.get("/api/calls/voice-twilio-sum-written").json()
    assert written["call"]["summary_state"] == "written"
    assert written["call"]["summary"].startswith("Chased the Tuesday delivery")
    assert written["call"]["agent"] == "hermes-main"
    assert written["call"]["outlet"] == "phone"
    assert written["transcript"]["status"] == "ok"

    broken = client.get("/api/calls/voice-twilio-sum-broken").json()
    assert broken["call"]["summary"] is None
    assert broken["call"]["summary_state"] == "unavailable"
    # The call itself is untouched by its missing summary - bar 1, seen from the reader.
    assert broken["call"]["outcome"] == "ok"
    assert broken["call"]["duration_s"] == pytest.approx(63.4)
    assert broken["transcript"]["status"] == "ok"
