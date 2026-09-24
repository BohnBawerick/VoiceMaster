"""API-level tests for the Hindsight-backed Calls API.

Every document here comes from ``tests/hindsight_producer_fixtures``, which is
built from the three retainers' source rather than from anything an earlier round
of this ticket wrote. See that module's docstring.

Browser-level coverage of the same behaviour lives in ``test_calls_browser.py``:
every defect the three review rounds found was visible by opening a page.
"""
import pytest
from fastapi.testclient import TestClient
import httpx

import app as voice_app
import hindsight_calls

from hindsight_producer_fixtures import (
    cascade_outbound,
    drop_created_at,
    hypothetical_call_with_outcome,
    non_call_memory,
    talk_outbound,
    three_real_calls,
    twilio_inbound,
)


@pytest.fixture
def client():
    app_instance = voice_app.create_app()
    return TestClient(app_instance)


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


# --------------------------------------------------------------------------
# 1. Nothing is invented for a field no producer writes
# --------------------------------------------------------------------------


def test_no_invented_outcome_summary_duration_or_turns(client, monkeypatch):
    """The merge blocker of rounds 1-3, in one test.

    These are the documents retained BEFORE ticket 05: platform, direction, target
    and a date, and nothing else. None of them may come back carrying an outcome, a
    summary, an error sentence, a duration, a turn count or an Outlet, and none of
    them may be labelled incomplete. Nothing wrote any of that, so any value here
    would be invention - which is exactly what ticket 05 asks for on old records.
    """
    docs = three_real_calls()

    async def mock_get(self_arg, url, **kwargs):
        if "/documents/" in str(url):
            wanted = str(url).rsplit("/", 1)[-1]
            for doc in docs:
                if doc["id"] == wanted:
                    return httpx.Response(200, json=doc)
            return httpx.Response(404)
        if "/documents" in str(url) and _bank_of(url) == "voice":
            return httpx.Response(200, json={"items": docs, "total": len(docs)})
        return httpx.Response(200, json={"items": [], "total": 0})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    listed = client.get("/api/calls").json()["calls"]
    assert len(listed) == 3
    for call in listed:
        assert call["outcome"] is None, call
        assert call["summary"] is None, call
        assert call["err"] is None, call
        assert call["duration_s"] is None, call
        assert call["num_turns"] is None, call
        assert call["incomplete"] is None, call
        # what WAS retained is still relayed
        assert call["direction"] in ("inbound", "outbound")
        assert call["platform"].startswith("voice_")
        # s5 (ticket 05): a pre-05 document records NO Outlet, and the reader does not
        # back-fill one from `platform`. "voice_twilio" is the transport that carried the
        # call, not the Outlet it was assigned to; reading one as the other put a guess in
        # the column the owner now filters on.
        assert call["outlet"] == "", call

    detail = client.get("/api/calls/voice-talk-outbound-real").json()
    assert detail["incomplete"] is None
    assert detail["call"]["outcome"] is None
    assert detail["call"]["err"] is None
    assert detail["summary"]["outcome"] is None
    assert detail["summary"]["err"] is None
    assert detail["summary"]["duration_s"] is None
    assert detail["summary"]["num_turns"] is None
    # Hindsight retains no per-turn records; an empty list must not be read as
    # "this call had zero turns".
    assert detail["turns"] == []
    assert detail["turns_retained"] is False


def test_retained_outcome_and_summary_are_relayed(client, monkeypatch):
    """The contrast that keeps the test above from passing on a screen that
    always says "not retained": a document that DOES carry them shows them."""
    doc = hypothetical_call_with_outcome()

    async def mock_get(self_arg, url, **kwargs):
        if "/documents/" in str(url) and doc["id"] in str(url):
            return httpx.Response(200, json=doc)
        if "/documents" in str(url) and _bank_of(url) == "voice":
            return httpx.Response(200, json={"items": [doc], "total": 1})
        return httpx.Response(200, json={"items": [], "total": 0})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    call = client.get("/api/calls").json()["calls"][0]
    assert call["outcome"] == "ok"
    assert call["summary"] == "Booked the table for 7pm."

    detail = client.get("/api/calls/voice-talk-with-summary").json()
    assert detail["summary"]["outcome"] == "ok"


def test_detail_summary_object_for_a_call_with_nothing_retained(client, monkeypatch):
    """The detail payload's `summary` object survives a call with nothing retained.

    Round 3 suppressed the whole metadata block for these (every real call) and
    replaced it with a banner asserting the call did not finish cleanly. The
    object must be present, and every value in it must be either retained or
    explicitly absent.

    Ticket 15 note: this object was the deleted legacy screen's metadata block.
    The React screen reads `call`, not `summary`, so nothing on screen depends on
    it today - it is kept because it is API surface with a stated contract, and
    because "the store held nothing" versus "the reader lost it" is exactly the
    distinction this file exists to pin. Delete it deliberately or not at all.
    """
    doc = twilio_inbound("voice-twilio-inbound-nosummary", "2026-08-17T16:00:00Z")

    async def mock_get(self_arg, url, **kwargs):
        if doc["id"] in str(url):
            return httpx.Response(200, json=doc)
        if "/documents" in str(url):
            return httpx.Response(200, json={"items": [doc]})
        return httpx.Response(404)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    res = client.get("/api/calls/voice-twilio-inbound-nosummary")
    assert res.status_code == 200
    detail = res.json()

    # The metadata block is driven by this object being present.
    summary = detail["summary"]
    assert summary is not None
    assert summary["mode"] == "voice_twilio"
    assert summary["direction"] == "inbound"
    assert summary["start_ts"] == pytest.approx(1786982400.0)
    assert summary["outcome"] is None
    assert summary["err"] is None
    # raw retained values, not the screen's wording for their absence
    assert summary["caller"] == ""
    assert summary["target"] == ""

    # No claim, in either direction, about how the call ended.
    assert detail["incomplete"] is None
    assert detail["call"]["outcome"] is None
    assert detail["call"]["err"] is None

    # An inbound call's other party is not retained by any producer.
    assert detail["call"]["who"] == "Not retained"

    # And the verbatim transcript is still verbatim.
    assert detail["transcript"]["status"] == "ok"
    assert detail["transcript"]["transcript_in"] == doc["original_text"]


def test_detail_summary_object_for_a_call_that_has_one(client, monkeypatch):
    """Restored: the detail contract for a call that DOES have a summary.

    Round 3 deleted this. It is the only test of the branch that fills the
    metadata block with real values.
    """
    doc = hypothetical_call_with_outcome()

    async def mock_get(self_arg, url, **kwargs):
        if doc["id"] in str(url):
            return httpx.Response(200, json=doc)
        if "/documents" in str(url):
            return httpx.Response(200, json={"items": [doc]})
        return httpx.Response(404)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    detail = client.get("/api/calls/voice-talk-with-summary").json()
    summary = detail["summary"]
    assert summary is not None
    assert summary["call_id"] == "voice-talk-with-summary"
    assert summary["outcome"] == "ok"
    assert summary["mode"] == "voice_talk"
    assert summary["direction"] == "outbound"
    assert summary["target"] == "+61491570159"
    assert isinstance(summary["start_ts"], float)
    assert summary["num_tool_calls"] is None
    assert detail["transcript"]["status"] == "ok"


# --------------------------------------------------------------------------
# 2. Paging: complete, truthful, and bounded
# --------------------------------------------------------------------------


def test_list_calls_paged_past_100_boundary(client, monkeypatch):
    """GET /api/calls pages past the 100-doc boundary and reports a true total."""
    voice_items = [
        talk_outbound(f"voice-talk-voice-{i:03d}", f"2026-08-17T14:{i % 60:02d}:00Z")
        for i in range(125)
    ]
    hermes_items = [
        twilio_inbound(f"voice-twilio-hermes-{i:03d}", f"2026-08-17T10:{i % 60:02d}:00Z")
        for i in range(125)
    ]

    async def mock_get(self_arg, url, params=None, **kwargs):
        params = params or {}
        offset = params.get("offset", 0)
        limit = params.get("limit", 100)
        items = voice_items if _bank_of(url) == "voice" else hermes_items
        return httpx.Response(
            200, json={"items": items[offset : offset + limit], "total": len(items)}
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    d1 = client.get("/api/calls?page=1&page_size=20").json()
    assert d1["unreachable"] is False
    assert d1["partial"] is False
    assert d1["total"] == 250
    assert len(d1["calls"]) == 20
    assert d1["has_more"] is True

    # Page 11 is only reachable if history past 100 per bank was fetched.
    d11 = client.get("/api/calls?page=11&page_size=20").json()
    assert d11["total"] == 250
    assert len(d11["calls"]) == 20
    assert d11["has_more"] is True

    d13 = client.get("/api/calls?page=13&page_size=20").json()
    assert d13["total"] == 250
    assert len(d13["calls"]) == 10
    assert d13["has_more"] is False

    # Every document is reachable through some page, exactly once.
    seen = []
    for page in range(1, 14):
        seen += [c["call_id"] for c in client.get(f"/api/calls?page={page}&page_size=20").json()["calls"]]
    assert len(seen) == 250
    assert len(set(seen)) == 250


def test_paging_loop_is_bounded_when_store_ignores_offset(client, monkeypatch):
    """A store that honours `limit`, ignores `offset` and omits `total` must not
    hang the endpoint.

    Round 3's `while True:` made 2,947 requests in 25 seconds against exactly
    this store and never returned. The mock refuses to be hammered: past a small
    ceiling it fails the test rather than letting it run forever.
    """
    docs = [
        talk_outbound(f"voice-talk-voice-{i:03d}", f"2026-08-17T14:{i % 60:02d}:00Z")
        for i in range(125)
    ]
    requests = {"n": 0}

    async def mock_get(self_arg, url, params=None, **kwargs):
        requests["n"] += 1
        if requests["n"] > 30:
            raise AssertionError(
                f"unbounded paging loop: {requests['n']} requests to the store"
            )
        params = params or {}
        limit = params.get("limit", 100)
        items = docs if _bank_of(url) == "voice" else []
        # honours limit, ignores offset, reports no total
        return httpx.Response(200, json={"items": items[:limit]})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    data = client.get("/api/calls?page=1&page_size=20").json()

    assert requests["n"] <= 10, f"{requests['n']} requests for one page load"
    # 100 of the bank's 125 documents were reachable; the response says so
    # rather than presenting 100 as the whole history.
    assert data["unreachable"] is False
    assert data["total"] == 100
    assert data["partial"] is True
    assert "voice" in data["warning"]


def test_fetch_bounds_are_finite():
    """The bounds ARE the contract, so their values are asserted directly.

    Every behavioural test below scales itself to these constants, so raising one
    to an absurd value would slip past all of them. This does not.
    """
    assert 0 < hindsight_calls.MAX_PAGES_PER_BANK <= 1000
    assert 0 < hindsight_calls.FETCH_DEADLINE_S <= 60
    assert 0 < hindsight_calls.PAGE_LIMIT <= 1000
    assert 0 < hindsight_calls.REQUEST_TIMEOUT_S <= 30


def test_page_cap_stops_an_endless_store_and_says_the_read_was_short(client, monkeypatch):
    """A store with no end to its documents: every page is full and every page is
    new, so the no-new-documents stop never fires and only the page cap can.

    The request ceiling below is a fixed number, not derived from
    MAX_PAGES_PER_BANK, so removing or inflating the cap fails this test instead
    of moving its expectations along with it.
    """
    requests = {"n": 0}

    async def mock_get(self_arg, url, params=None, **kwargs):
        requests["n"] += 1
        if requests["n"] > 150:
            raise AssertionError(
                f"page cap did not stop an endless store: {requests['n']} requests"
            )
        params = params or {}
        offset = params.get("offset", 0)
        limit = params.get("limit", 100)
        bank = _bank_of(url)
        # always a full page, always documents this request has not seen
        items = [
            talk_outbound(f"voice-talk-{bank}-{i:06d}", "2026-08-17T14:00:00Z")
            for i in range(offset, offset + limit)
        ]
        return httpx.Response(200, json={"items": items})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    data = client.get("/api/calls?page=1&page_size=20").json()

    assert requests["n"] <= 150, f"{requests['n']} requests for one page load"
    assert data["unreachable"] is False
    assert len(data["calls"]) == 20
    # the short read is declared, never presented as the whole history
    assert data["partial"] is True
    assert "not shown" in data["warning"]
    assert "voice" in data["warning"] and "hermes" in data["warning"]


def test_deadline_stops_a_slow_store_and_says_the_read_was_short(client, monkeypatch):
    """A store that answers, but too slowly to finish inside the deadline.

    The clock is driven rather than slept through. This test used to shorten
    FETCH_DEADLINE_S to 0.2s and rely on real time, so on a loaded machine the
    budget could expire before the first request was even issued and no bank was
    read at all -- 6 failures in 10 with four processes competing. Advancing a
    fake clock inside the mock makes it exact and instant, and still binds the
    behaviour: remove the deadline check and this goes red on the request count.
    """
    clock = {"t": 1000.0}
    monkeypatch.setattr(hindsight_calls, "_now", lambda: clock["t"])
    monkeypatch.setattr(hindsight_calls, "FETCH_DEADLINE_S", 1.0)
    requests = {"n": 0}

    async def mock_get(self_arg, url, params=None, **kwargs):
        requests["n"] += 1
        if requests["n"] > 20:
            raise AssertionError(f"deadline did not stop a slow store: {requests['n']} requests")
        # Each request "takes" 0.6s of the 1.0s budget, so the second one lands
        # exactly on the far side of the deadline.
        clock["t"] += 0.6
        params = params or {}
        offset = params.get("offset", 0)
        limit = params.get("limit", 100)
        bank = _bank_of(url)
        items = [
            talk_outbound(f"voice-talk-{bank}-{i:06d}", "2026-08-17T14:00:00Z")
            for i in range(offset, offset + limit)
        ]
        return httpx.Response(200, json={"items": items})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    data = client.get("/api/calls?page=1&page_size=20").json()

    # one page per bank got through before the clock ran out
    assert requests["n"] <= 4, f"{requests['n']} requests after the deadline"
    assert data["unreachable"] is False
    assert len(data["calls"]) == 20
    assert data["partial"] is True
    assert "not shown" in data["warning"]


# --------------------------------------------------------------------------
# 3. Bank failure is partial, never total
# --------------------------------------------------------------------------


def test_partial_bank_failure_keeps_the_calls_the_other_bank_returned(client, monkeypatch):
    """One bank 503s while the other holds five real calls.

    Round 3 threw all five away and showed an empty "unreachable" screen.
    """
    docs = three_real_calls() + [
        talk_outbound("voice-talk-voice-004", "2026-08-17T13:00:00Z"),
        cascade_outbound("voice-cascade-voice-005", "2026-08-17T13:30:00Z"),
    ]

    async def mock_get(self_arg, url, **kwargs):
        if _bank_of(url) == "hermes":
            return httpx.Response(503, json={"detail": "hermes bank unavailable"})
        return httpx.Response(200, json={"items": docs, "total": len(docs)})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    data = client.get("/api/calls").json()

    assert len(data["calls"]) == 5
    assert data["total"] == 5
    assert data["unreachable"] is False
    assert data["partial"] is True
    assert "hermes" in data["warning"]
    assert "503" in data["warning"]


def test_healthy_empty_bank_does_not_mask_a_failing_bank(client, monkeypatch):
    """voice answers 200-empty, hermes 503. This must not read as "no calls"."""

    async def mock_get(self_arg, url, **kwargs):
        if _bank_of(url) == "hermes":
            return httpx.Response(503, json={"detail": "hermes bank unavailable"})
        return httpx.Response(200, json={"items": [], "total": 0})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    data = client.get("/api/calls").json()
    assert data["calls"] == []
    assert data["partial"] is True
    assert data["warning"] and "hermes" in data["warning"]
    assert data["error"] is None or "hermes" in data["error"]


def test_absent_secondary_bank_is_not_a_store_outage(client, monkeypatch):
    """The configuration the compose actually ships.

    `HINDSIGHT_BANK=hermes` for voice-control, so the fallback `voice` bank is
    queried too. Nothing has ever written to `voice`, so the store may well 404
    it. Five real calls in a healthy store must not become an empty screen.
    """
    monkeypatch.setenv("HINDSIGHT_BANK", "hermes")
    docs = three_real_calls() + [
        talk_outbound("voice-talk-hermes-004", "2026-08-17T13:00:00Z"),
        cascade_outbound("voice-cascade-hermes-005", "2026-08-17T13:30:00Z"),
    ]

    async def mock_get(self_arg, url, **kwargs):
        if _bank_of(url) == "voice":
            return httpx.Response(404, json={"detail": "bank 'voice' not found"})
        return httpx.Response(200, json={"items": docs, "total": len(docs)})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    data = client.get("/api/calls").json()
    assert len(data["calls"]) == 5
    assert data["unreachable"] is False
    assert data["partial"] is False
    assert data["warning"] is None
    assert data["error"] is None


def test_absent_configured_bank_is_reported(client, monkeypatch):
    """An absent *configured* bank is a deployment problem the owner must see."""
    monkeypatch.setenv("HINDSIGHT_BANK", "hermes")
    docs = [talk_outbound("voice-talk-voice-000", "2026-08-17T13:00:00Z")]

    async def mock_get(self_arg, url, **kwargs):
        if _bank_of(url) == "hermes":
            return httpx.Response(404, json={"detail": "bank 'hermes' not found"})
        return httpx.Response(200, json={"items": docs, "total": len(docs)})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    data = client.get("/api/calls").json()
    assert len(data["calls"]) == 1
    assert data["partial"] is True
    assert "hermes" in data["warning"]


def test_store_503_error_returns_unreachable(client, monkeypatch):
    """Restored (round 3 deleted it): every bank 503 is an honest outage."""

    async def mock_get(self_arg, url, **kwargs):
        return httpx.Response(503, json={"detail": "Service Unavailable"})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    data = client.get("/api/calls").json()
    assert data["calls"] == []
    assert data["unreachable"] is True
    assert data["exists"] is False
    assert "503" in data["error"]


def test_unreachable_hindsight_honest_empty_state(client, monkeypatch):
    """Restored (absent since round 2): a refused connection is not an empty store.

    Also guards the message. `str(httpx.ConnectError(""))` is empty, which is how
    round 3 produced "bank 'voice' failed: ; bank 'hermes' failed: ".
    """

    async def mock_get(self_arg, url, **kwargs):
        raise httpx.ConnectError("")

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    data = client.get("/api/calls").json()
    assert data["calls"] == []
    assert data["unreachable"] is True
    assert data["exists"] is False
    assert "ConnectError" in data["error"]
    assert not data["error"].rstrip(")").rstrip().endswith(":")


def test_get_call_does_not_report_not_found_when_a_bank_is_down(client, monkeypatch):
    """A call in a bank that is down must not be reported as not existing."""

    async def mock_get(self_arg, url, **kwargs):
        if _bank_of(url) == "hermes":
            return httpx.Response(503, json={"detail": "hermes bank unavailable"})
        if "/documents/" in str(url):
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json={"items": [], "total": 0})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    res = client.get("/api/calls/voice-talk-hermes-004")
    # Not a 404: the app does not know that it does not exist.
    assert res.status_code == 200
    data = res.json()
    assert data["call"] is None
    assert data["partial"] is True
    assert "hermes" in data["error"]
    assert "may exist" in data["error"]


def test_get_call_not_found_when_every_bank_answered(client, monkeypatch):
    """When every bank answered and none holds it, "not found" is the truth."""

    async def mock_get(self_arg, url, **kwargs):
        if "/documents/" in str(url):
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json={"items": [], "total": 0})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    res = client.get("/api/calls/voice-talk-nope")
    assert res.status_code == 404
    data = res.json()
    assert data["error"] == "Call 'voice-talk-nope' not found"
    assert data["partial"] is False
    assert data["unreachable"] is False


# --------------------------------------------------------------------------
# 4. What was retained, relayed exactly
# --------------------------------------------------------------------------


def test_real_retainer_schema_metadata(client, monkeypatch):
    """Producers write platform, direction, target, date. Inbound target is empty."""
    outbound = talk_outbound("voice-talk-voice-000", "2026-08-17T12:34:56.789Z",
                             target="+61491570158")
    inbound = twilio_inbound("voice-twilio-voice-001", "2026-08-17T11:00:00Z")

    async def mock_get(self_arg, url, **kwargs):
        if "/documents" in str(url) and _bank_of(url) == "voice":
            return httpx.Response(200, json={"items": [outbound, inbound], "total": 2})
        return httpx.Response(200, json={"items": [], "total": 0})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    calls = client.get("/api/calls").json()["calls"]

    c1 = next(c for c in calls if c["call_id"] == "voice-talk-voice-000")
    assert c1["who"] == "+61491570158"
    # the document-level timestamp, not the date-only metadata.date
    assert c1["when"] == "2026-08-17T12:34:56.789Z"

    c2 = next(c for c in calls if c["call_id"] == "voice-twilio-voice-001")
    assert c2["who"] == "Not retained"


def test_date_only_timestamp_is_reported_as_date_only(client, monkeypatch):
    """If the store does not stamp `created_at`, the only timestamp retained is
    the producers' `%Y-%m-%d` date.

    It parses to midnight, so a screen that prints a clock time has invented one.
    The payload says which precision it holds so neither screen has to guess.
    """
    dated = drop_created_at(talk_outbound("voice-talk-voice-000", "2026-08-17T10:15:00Z"))
    undated = drop_created_at(cascade_outbound("voice-cascade-voice-001", "2026-08-17T11:25:00Z"))
    assert "created_at" not in dated and dated["metadata"]["date"] == "2026-08-17"
    assert "created_at" not in undated and "date" not in undated["metadata"]

    async def mock_get(self_arg, url, **kwargs):
        if "/documents" in str(url) and _bank_of(url) == "voice":
            return httpx.Response(200, json={"items": [dated, undated], "total": 2})
        return httpx.Response(200, json={"items": [], "total": 0})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    calls = {c["call_id"]: c for c in client.get("/api/calls").json()["calls"]}

    c1 = calls["voice-talk-voice-000"]
    assert c1["when"] == "2026-08-17"
    assert c1["when_precision"] == "date"
    # Neither screen renders this stamp as a clock time, but `start_ts` still
    # orders it against the calls that DO have one, so which midnight it means
    # has to be settled rather than left to the server's timezone.
    assert c1["start_ts"] == pytest.approx(1786924800.0)  # 2026-08-17T00:00:00Z

    # nothing at all was retained for the cascade document
    c2 = calls["voice-cascade-voice-001"]
    assert c2["when"] == ""
    assert c2["when_precision"] is None
    assert c2["start_ts"] is None


def test_full_timestamp_is_reported_as_such(client, monkeypatch):
    """The contrast: a store-stamped `created_at` keeps its clock time."""
    doc = talk_outbound("voice-talk-voice-000", "2026-08-17T10:15:00Z")

    async def mock_get(self_arg, url, **kwargs):
        if "/documents" in str(url) and _bank_of(url) == "voice":
            return httpx.Response(200, json={"items": [doc], "total": 1})
        return httpx.Response(200, json={"items": [], "total": 0})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    call = client.get("/api/calls").json()["calls"][0]
    assert call["when"] == "2026-08-17T10:15:00Z"
    assert call["when_precision"] == "datetime"


def test_a_naive_timestamp_is_resolved_once_as_utc(client, monkeypatch):
    """A `created_at` with no offset must not be left for the reader to guess.

    While `when` carried no zone, the API resolved it in the SERVER's timezone and
    the screen parsed the same string in the BROWSER's, so one call read as two
    clock times and, far enough apart, two calendar days. The payload settles it:
    `when` goes out with an explicit offset and `start_ts` agrees with it.

    Browser-level proof that the screen then lands on the retained day, in three
    timezones, is
    ``test_calls_matrix.test_the_screen_puts_a_naive_timestamp_on_the_retained_day``.
    """
    doc = talk_outbound("voice-talk-voice-000", "2026-08-17T23:30:00Z")
    doc["created_at"] = "2026-08-17T23:30:00"  # no offset at all

    async def mock_get(self_arg, url, **kwargs):
        if "/documents" in str(url) and _bank_of(url) == "voice":
            return httpx.Response(200, json={"items": [doc], "total": 1})
        return httpx.Response(200, json={"items": [], "total": 0})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    call = client.get("/api/calls").json()["calls"][0]
    assert call["when_precision"] == "datetime"
    # an explicit offset, so `new Date(when)` in any browser means one instant
    assert call["when"] == "2026-08-17T23:30:00+00:00"
    # ...and `start_ts` is that same instant, whatever timezone this test runs in
    assert call["start_ts"] == pytest.approx(1787009400.0)


def test_a_timestamp_that_carries_an_offset_is_left_alone(client, monkeypatch):
    """The contrast: a stamp that already states its zone is not rewritten."""
    doc = talk_outbound("voice-talk-voice-001", "2026-08-17T23:30:00Z")
    doc["created_at"] = "2026-08-17T23:30:00+10:00"

    async def mock_get(self_arg, url, **kwargs):
        if "/documents" in str(url) and _bank_of(url) == "voice":
            return httpx.Response(200, json={"items": [doc], "total": 1})
        return httpx.Response(200, json={"items": [], "total": 0})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    call = client.get("/api/calls").json()["calls"][0]
    assert call["when"] == "2026-08-17T23:30:00+10:00"
    assert call["start_ts"] == pytest.approx(1786973400.0)


def test_non_call_documents_in_the_shared_bank_are_not_listed_as_calls(client, monkeypatch):
    """The fallback bank is Hermes's general memory bank.

    A stored preference is not a phone call, and must not be rendered as one.
    """
    call = talk_outbound("voice-talk-hermes-000", "2026-08-17T13:00:00Z")
    memory = non_call_memory("hermes-pref-001", "2026-08-17T13:05:00Z")

    async def mock_get(self_arg, url, **kwargs):
        if _bank_of(url) == "hermes":
            return httpx.Response(200, json={"items": [call, memory], "total": 2})
        return httpx.Response(200, json={"items": [], "total": 0})

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    data = client.get("/api/calls").json()
    assert [c["call_id"] for c in data["calls"]] == ["voice-talk-hermes-000"]
    assert data["skipped"] == 1


def test_search_calls_recall_document_resolution(client, monkeypatch):
    """Recall search resolves a fact's document_id to the full call document.

    Hindsight recall returns extracted facts, not documents. The hit must open as
    the call it came from, with the document's verbatim text, and both banks must
    be recalled.
    """
    voice_doc = talk_outbound("voice-talk-voice-001", "2026-08-17T15:30:00Z")
    hermes_doc = cascade_outbound("voice-cascade-hermes-004", "2026-08-17T15:45:00Z")
    requested = []

    async def mock_post(self_arg, url, **kwargs):
        requested.append(str(url))
        bank = _bank_of(url)
        hits = {
            "voice": [{"id": "fact-v1", "document_id": voice_doc["id"],
                       "text": "coffee fragment from the voice bank"}],
            "hermes": [{"id": "fact-h1", "document_id": hermes_doc["id"],
                        "text": "coffee fragment from the hermes bank"}],
        }
        return httpx.Response(200, json={"results": hits.get(bank, [])})

    async def mock_get(self_arg, url, **kwargs):
        requested.append(str(url))
        text = str(url)
        if f"/banks/voice/documents/{voice_doc['id']}" in text:
            return httpx.Response(200, json=voice_doc)
        if f"/banks/hermes/documents/{hermes_doc['id']}" in text:
            return httpx.Response(200, json=hermes_doc)
        return httpx.Response(404)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    data = client.get("/api/calls?q=coffee").json()
    assert data["unreachable"] is False
    ids = sorted(c["call_id"] for c in data["calls"])
    assert ids == ["voice-cascade-hermes-004", "voice-talk-voice-001"]

    hit = next(c for c in data["calls"] if c["call_id"] == voice_doc["id"])
    # the document's verbatim text, not the recall fragment
    assert hit["transcript"] == voice_doc["original_text"]
    assert "coffee fragment" not in hit["transcript"]

    assert any("/banks/voice/memories/recall" in u for u in requested)
    assert any("/banks/hermes/memories/recall" in u for u in requested)
    # POST is the documented recall endpoint; the GET fallback only runs if it fails
    assert not any(u.endswith("/recall?q=coffee") for u in requested)


def test_get_banks_to_search_is_symmetric(monkeypatch):
    """Whichever bank is configured, both are queried, configured one first."""
    monkeypatch.setenv("HINDSIGHT_BANK", "hermes")
    assert hindsight_calls.get_banks_to_search() == ["hermes", "voice"]
    monkeypatch.setenv("HINDSIGHT_BANK", "voice")
    assert hindsight_calls.get_banks_to_search() == ["voice", "hermes"]
