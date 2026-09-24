"""The Calls API over the built-in SQLite call archive.

With no ``HINDSIGHT_URL`` (and no ``VOICE_ARCHIVE``), the bridges write each call into a
SQLite file beside the event log (`voicecore.call_store`) and this dashboard reads it back.
Every document here is written by the REAL producer path - `call_record.build_metadata`
and `build_tags`, then `call_store.write_result` - and read through the real endpoints, so
the round trip is the one a deployment runs, not a fixture that agrees with the reader.

Pinned here: list, detail and search round-trip; the filters and facets work unchanged; a
missing file is an empty archive; an unreadable file is an honest error; and a set
``HINDSIGHT_URL`` still sends the reader to Hindsight, never to the file.
"""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

import app as voice_app
from voicecore import call_record
from voicecore import call_store


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """The SQLite archive the conftest points at, with no Hindsight configured."""
    db = tmp_path / "calls.sqlite3"
    monkeypatch.setenv("VOICE_ARCHIVE_PATH", str(db))
    monkeypatch.delenv("HINDSIGHT_URL", raising=False)
    monkeypatch.delenv("VOICE_ARCHIVE", raising=False)
    return db


@pytest.fixture
def client():
    return TestClient(voice_app.create_app())


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The SQLite read path makes no HTTP request at all."""
    async def refuse(self, url, **kwargs):
        raise AssertionError(f"unexpected network call to {url}")
    monkeypatch.setattr(httpx.AsyncClient, "get", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "post", refuse)


def _retain(db, doc_id, transcript, *, direction="inbound", agent="front-desk",
            outlet="phone", mission=None, started_at=1_780_000_000.0):
    metadata = call_record.build_metadata(
        platform="voice_twilio", outlet=outlet, direction=direction,
        caller="+15550100" if direction == "inbound" else None,
        target="+15550199" if direction == "outbound" else None,
        agent=agent, mission=mission, outcome="ok", duration_s=42.0,
        started_at=started_at)
    tags = call_record.build_tags(lane="twilio", direction=direction, outlet=outlet,
                                  agent=agent)
    ok, reason = asyncio.run(call_store.write_result(
        content=transcript, document_id=doc_id, metadata=metadata, tags=tags, path=db))
    assert (ok, reason) == (True, None)


def test_a_written_call_is_listed_and_opened(archive, client):
    _retain(archive, "voice-twilio-CA1", "Them: hello\nAI: hi, front desk")
    _retain(archive, "voice-twilio-CA2", "Them: book a table\nAI: done",
            direction="outbound", agent="booker", mission="Book a table for two")

    body = client.get("/api/calls").json()
    assert body["unreachable"] is False and body["error"] is None
    assert body["total"] == 2
    ids = {c["call_id"] for c in body["calls"]}
    assert ids == {"voice-twilio-CA1", "voice-twilio-CA2"}
    by_id = {c["call_id"]: c for c in body["calls"]}
    assert by_id["voice-twilio-CA2"]["mission"] == "Book a table for two"
    assert by_id["voice-twilio-CA2"]["agent"] == "booker"
    assert by_id["voice-twilio-CA1"]["outlet"] == "phone"
    assert by_id["voice-twilio-CA1"]["duration_s"] == 42.0
    assert by_id["voice-twilio-CA1"]["when_precision"] == "datetime"
    assert body["agents"] == ["booker", "front-desk"]

    detail = client.get("/api/calls/voice-twilio-CA2").json()
    assert detail["error"] is None
    assert detail["call"]["transcript"] == "Them: book a table\nAI: done"
    assert detail["transcript"]["status"] == "ok"
    assert detail["summary"]["direction"] == "outbound"


def test_the_response_keys_are_the_hindsight_ones(archive, client, monkeypatch):
    """The screen reads one contract; which store answered must not change its keys."""
    _retain(archive, "voice-twilio-CA1", "Them: hello\nAI: hi")
    sqlite_keys = set(client.get("/api/calls").json())

    async def empty_store(self, url, **kwargs):
        return httpx.Response(200, json={"items": [], "total": 0})
    monkeypatch.setattr(httpx.AsyncClient, "get", empty_store)
    monkeypatch.setenv("HINDSIGHT_URL", "http://hindsight.test:8888")
    assert set(client.get("/api/calls").json()) == sqlite_keys


def test_search_matches_transcripts_and_metadata(archive, client):
    _retain(archive, "voice-twilio-CA1", "Them: my BOILER is broken\nAI: sending someone")
    _retain(archive, "voice-twilio-CA2", "Them: hi\nAI: hello", direction="outbound",
            agent="booker", mission="Confirm the delivery window")

    hits = client.get("/api/calls", params={"q": "boiler"}).json()
    assert [c["call_id"] for c in hits["calls"]] == ["voice-twilio-CA1"]
    hits = client.get("/api/calls", params={"q": "DELIVERY"}).json()
    assert [c["call_id"] for c in hits["calls"]] == ["voice-twilio-CA2"]
    assert client.get("/api/calls", params={"q": "nothing like it"}).json()["total"] == 0


def test_filters_and_paging_run_over_the_archive(archive, client):
    for i in range(5):
        _retain(archive, f"voice-twilio-CA{i}", f"Them: call {i}\nAI: ok",
                agent="front-desk" if i % 2 else "booker", started_at=1_780_000_000.0 + i)

    body = client.get("/api/calls", params={"agent": "booker"}).json()
    assert body["total"] == 3
    assert {c["agent"] for c in body["calls"]} == {"booker"}
    page = client.get("/api/calls", params={"page": 2, "page_size": 2}).json()
    assert page["total"] == 5 and len(page["calls"]) == 2 and page["has_more"] is True


def test_a_missing_archive_is_empty_not_an_error(archive, client):
    assert not archive.exists()
    body = client.get("/api/calls").json()
    assert body["unreachable"] is False and body["error"] is None
    assert body["calls"] == [] and body["total"] == 0

    detail = client.get("/api/calls/voice-twilio-nope").json()
    assert detail["unreachable"] is False
    assert detail["error"] == "Call 'voice-twilio-nope' not found"
    assert not archive.exists()


def test_an_unreadable_archive_is_an_honest_error(archive, client):
    archive.write_bytes(b"this is not a sqlite database, it is junk" * 20)

    body = client.get("/api/calls").json()
    assert body["unreachable"] is True
    assert "could not be read" in body["error"]
    assert str(archive) in body["error"]

    detail = client.get("/api/calls/voice-twilio-CA1").json()
    assert detail["unreachable"] is True
    assert "could not be read" in detail["error"]
    assert "not found" not in detail["error"]


def test_a_hindsight_url_sends_the_reader_to_hindsight(archive, client, monkeypatch):
    """The live deployment's case: HINDSIGHT_URL is set, so the file is never read."""
    _retain(archive, "voice-twilio-LOCAL", "Them: only in sqlite\nAI: yes")
    asked = []

    async def store(self, url, **kwargs):
        asked.append(str(url))
        return httpx.Response(200, json={"items": [], "total": 0})
    monkeypatch.setattr(httpx.AsyncClient, "get", store)
    monkeypatch.setenv("HINDSIGHT_URL", "http://hindsight.test:8888")

    body = client.get("/api/calls").json()
    assert body["total"] == 0
    assert asked and all(u.startswith("http://hindsight.test:8888/") for u in asked)


def test_a_misconfigured_archive_is_said_plainly(archive, client, monkeypatch):
    monkeypatch.setenv("VOICE_ARCHIVE", "postgres")
    body = client.get("/api/calls").json()
    assert body["unreachable"] is True and "VOICE_ARCHIVE" in body["error"]
    detail = client.get("/api/calls/voice-twilio-CA1").json()
    assert "VOICE_ARCHIVE" in detail["error"]
