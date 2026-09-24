"""The reader against the shape the STORE SERVES, not the shape the retainers POST.

This module exists because of a defect that every other test in this suite was blind
to, by construction. `hindsight.retain_result` POSTs a document as::

    {"items": [{"content": ..., "document_id": ..., "metadata": {...}, "tags": [...]}]}

and every fixture in `hindsight_producer_fixtures` models that body, because the rule
here is "build fixtures from the producers". But Hindsight does not hand those fields
back under ``metadata``. It serves ``document_metadata``, and a returned document has no
``metadata`` key at all - on the list endpoint and on fetch-by-id, on the `voice` bank
and on `hermes`. Verified against the live store on 2026-08-19.

So `format_call_doc` read a key that never arrives. Every Call on the real dashboard
rendered "not retained" in every column the last three tickets added - ticket 05's
agent, outlet, outcome and duration, ticket 07's recording, ticket 06's summary and
summary_state - while the store held all of them. The fixtures agreed with the code
instead of with the store, which is the exact failure this repo's AGENTS.md warns about,
and no amount of sabotage could have caught it: there was no test that opened a document
in the shape a document arrives in.

Two things keep it from coming back. `browser_harness` now serves every document through
`as_store_returns`, so the whole Playwright suite runs against the real shape. And this
module asserts the reader directly, on both endpoints, for the fields of all three
tickets - including the case that matters most, a Call that HAS a summary.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

import app as voice_app
import hindsight_calls

from hindsight_producer_fixtures import (
    as_store_returns,
    phone_outbound_v5,
    summary_nothing_to_say_v5,
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


def _serve_live(monkeypatch, docs, bank="voice"):
    """Serve `docs` the way the real store serves them: `document_metadata`, no `metadata`."""
    served = [as_store_returns(doc) for doc in docs]

    async def mock_get(self_arg, url, **kwargs):
        if "/documents/" in str(url):
            wanted = str(url).rsplit("/", 1)[-1]
            for doc in served:
                if doc["id"] == wanted:
                    return httpx.Response(200, json=doc)
            return httpx.Response(404)
        if "/documents" in str(url) and _bank_of(url) == bank:
            return httpx.Response(200, json={"items": served, "total": len(served)})
        return httpx.Response(200, json={"items": [], "total": 0})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)


WRITTEN = summary_written_v5("voice-twilio-live-written", "2026-08-18T09:00:00+00:00",
                             recording="2026/08/18/phone-live.opus")
NOTHING = summary_nothing_to_say_v5("voice-twilio-live-nothing",
                                    "2026-08-18T10:00:00+00:00")
LEGACY = twilio_inbound("voice-twilio-live-legacy", "2026-08-17T09:05:00Z")

CORPUS = [WRITTEN, NOTHING, LEGACY]


# --------------------------------------------------------------------------
# The shape itself
# --------------------------------------------------------------------------


def test_the_fixture_really_is_a_different_shape():
    """Without this, every assertion below could be passing on the POST key by accident."""
    served = as_store_returns(WRITTEN)
    assert "metadata" not in served, "this is not the shape the store serves"
    assert served["document_metadata"] == WRITTEN["metadata"]
    assert served["id"] == WRITTEN["id"]
    assert served["tags"] == WRITTEN["tags"]


def test_the_reader_finds_the_metadata_the_store_actually_serves():
    """`format_call_doc` on one live-shaped document, field by field.

    These are the columns tickets 05, 06 and 07 added. On the live store every one of
    them was None or "" before this fix.
    """
    call = hindsight_calls.format_call_doc(as_store_returns(WRITTEN))

    assert call["summary"] == (
        "Chased the Tuesday delivery; it shipped Monday and lands tomorrow.")
    assert call["summary_state"] == "written"           # ticket 06
    assert call["agent"] == "hermes-main"               # ticket 05
    assert call["outlet"] == "phone"                    # ticket 05
    assert call["outcome"] == "ok"                      # ticket 05
    assert call["duration_s"] == pytest.approx(63.4)    # ticket 05
    assert call["mission"] == "Book a table for 7pm."   # ticket 05
    assert call["direction"] == "outbound"
    assert call["who"] == "+61400000000"
    assert call["recording_ref"] == "2026/08/18/phone-live.opus"   # ticket 07


def test_the_post_shape_still_reads_too():
    """The reader takes both keys. A document in the POST shape - our own fixtures, and
    anything that ever replays a retain body - must not start reading as empty."""
    live = hindsight_calls.format_call_doc(as_store_returns(WRITTEN))
    posted = hindsight_calls.format_call_doc(WRITTEN)
    for field in ("summary", "summary_state", "agent", "outlet", "outcome",
                  "duration_s", "mission", "recording_ref", "who", "direction"):
        assert live[field] == posted[field], field


def test_a_document_with_neither_key_is_absent_not_a_crash():
    """A store that returns something we have never seen renders as not retained."""
    call = hindsight_calls.format_call_doc({"id": "voice-twilio-bare", "tags": ["voice"]})
    assert call["summary"] is None and call["summary_state"] is None
    assert call["agent"] == "" and call["outlet"] == ""


def test_a_call_document_is_recognised_by_its_served_metadata():
    """`_is_call_doc` reads metadata too, and the `hermes` bank is where it matters:
    it holds ordinary memories beside calls, and a call that is not recognised there
    is a call that vanishes from the history."""
    served = as_store_returns(phone_outbound_v5("no-voice-prefix", "2026-08-18T09:00:00Z"))
    served.pop("tags", None)
    served["id"] = served["document_id"] = "not-a-voice-prefixed-id"
    assert hindsight_calls._is_call_doc(served) is True


# --------------------------------------------------------------------------
# End to end, through the API the screens call
# --------------------------------------------------------------------------


def test_the_calls_list_shows_the_summary_against_a_live_shaped_store(client, monkeypatch):
    """Ticket 06's own checkbox: "stored with the Call and appears in the Calls list"."""
    _serve_live(monkeypatch, CORPUS)
    calls = {c["call_id"]: c for c in client.get("/api/calls").json()["calls"]}

    written = calls["voice-twilio-live-written"]
    assert written["summary"].startswith("Chased the Tuesday delivery")
    assert written["summary_state"] == "written"
    assert written["agent"] == "hermes-main" and written["outlet"] == "phone"

    # ...and the three absences are still three different answers on this shape.
    assert calls["voice-twilio-live-nothing"]["summary"] is None
    assert calls["voice-twilio-live-nothing"]["summary_state"] == "nothing_to_summarise"
    assert calls["voice-twilio-live-legacy"]["summary_state"] is None


def test_the_call_detail_shows_the_summary_against_a_live_shaped_store(client, monkeypatch):
    _serve_live(monkeypatch, CORPUS)
    body = client.get("/api/calls/voice-twilio-live-written").json()

    assert body["call"]["summary_state"] == "written"
    assert body["call"]["summary"].startswith("Chased the Tuesday delivery")
    assert body["call"]["recording_ref"] == "2026/08/18/phone-live.opus"
    assert body["call"]["duration_s"] == pytest.approx(63.4)
    assert body["summary"]["outcome"] == "ok"


def test_no_column_the_store_filled_reads_as_not_retained(client, monkeypatch):
    """The blunt version of the whole finding, stated as one property.

    Every field this document HAS must arrive as something. Before the fix, all of them
    came back empty and the screens said "Not retained" over a store that held them.
    """
    _serve_live(monkeypatch, [WRITTEN])
    call = client.get("/api/calls").json()["calls"][0]
    for field in ("summary", "summary_state", "agent", "outlet", "outcome",
                  "mission", "recording_ref"):
        assert call[field], f"{field} was retained but reads as not retained"
    assert call["duration_s"]
