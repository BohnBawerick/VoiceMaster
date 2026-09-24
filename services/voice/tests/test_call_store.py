"""The pluggable call archive: the SQLite store, and the rule that picks a backend.

`voicecore.call_store` is where a call's document goes when no Hindsight is configured.
These tests pin the four things that make it safe to be the default:

* **the backend rule** - ``VOICE_ARCHIVE`` wins; otherwise a set ``HINDSIGHT_URL`` means
  Hindsight, so a deployment that already sets it changes nothing; otherwise SQLite;
* **the write seam** - `call_record.retain_call` -> `hindsight.retain_detached` still
  dispatches every archive write, the ``retain`` event record still lands, and the
  ticket-06 summariser still runs before the document is written;
* **the served shape** - what the SQLite store reads back has the keys the Hindsight store
  serves, so the dashboard reads either one with the same code;
* **fire-and-forget** - a write that fails is ``(False, reason)`` and a ``retain`` record
  saying so, never an exception into call teardown.
"""
import asyncio
import json
import sqlite3

import pytest

from voicecore import call_record
from voicecore import call_store
from voicecore import eventlog
from voicecore import hindsight


# -- the backend rule ----------------------------------------------------------------------


@pytest.mark.parametrize("env, expected", [
    ({}, "sqlite"),
    ({"HINDSIGHT_URL": ""}, "sqlite"),
    ({"HINDSIGHT_URL": "   "}, "sqlite"),
    # The load-bearing row: a deployment that sets HINDSIGHT_URL keeps Hindsight with no
    # new variable.
    ({"HINDSIGHT_URL": "http://hindsight.test:8888"}, "hindsight"),
    ({"VOICE_ARCHIVE": "sqlite", "HINDSIGHT_URL": "http://hindsight.test:8888"}, "sqlite"),
    ({"VOICE_ARCHIVE": "hindsight"}, "hindsight"),
    ({"VOICE_ARCHIVE": " SQLite "}, "sqlite"),
    ({"VOICE_ARCHIVE": ""}, "sqlite"),
])
def test_the_backend_rule(env, expected):
    assert call_store.backend(env) == expected


def test_an_unknown_backend_refuses_and_names_the_variable():
    with pytest.raises(ValueError, match="VOICE_ARCHIVE"):
        call_store.backend({"VOICE_ARCHIVE": "postgres"})


def test_a_writer_passes_the_url_it_was_configured_with():
    """The bridges read HINDSIGHT_URL into their config; the seam asks with that value."""
    assert call_store.backend({}, hindsight_url="http://hindsight.test:8888") == "hindsight"
    assert call_store.backend({"HINDSIGHT_URL": "http://x"}, hindsight_url="") == "sqlite"


def test_the_archive_sits_beside_the_event_log_unless_named(tmp_path):
    log = tmp_path / "events" / "voice_events.jsonl"
    assert call_store.sqlite_path({"VOICE_EVENTLOG_PATH": str(log)}) == \
        tmp_path / "events" / "calls.sqlite3"
    named = tmp_path / "elsewhere.db"
    assert call_store.sqlite_path({"VOICE_ARCHIVE_PATH": str(named),
                                   "VOICE_EVENTLOG_PATH": str(log)}) == named


# -- the store -----------------------------------------------------------------------------


def _write(path, doc_id="voice-twilio-CA1", content="Them: hello\nAI: hi there",
           metadata=None, tags=None):
    return asyncio.run(call_store.write_result(
        content=content, document_id=doc_id,
        metadata=metadata if metadata is not None else {
            "platform": "voice_twilio", "direction": "inbound", "outlet": "phone",
            "agent": "front-desk", "duration_s": 12.5},
        tags=tags if tags is not None else ["voice", "twilio", "inbound"], path=path))


def test_a_written_call_reads_back_in_the_served_shape(tmp_path):
    db = tmp_path / "calls.sqlite3"
    assert _write(db) == (True, None)

    docs = call_store.read_all(db)
    assert len(docs) == 1
    doc = docs[0]
    assert set(doc) == {"id", "content", "document_metadata", "tags", "created_at"}
    assert doc["id"] == "voice-twilio-CA1"
    assert doc["content"] == "Them: hello\nAI: hi there"
    # Stringified exactly as Hindsight's retain sends metadata.
    assert doc["document_metadata"]["duration_s"] == "12.5"
    assert doc["document_metadata"]["agent"] == "front-desk"
    assert doc["tags"] == ["voice", "twilio", "inbound"]
    assert doc["created_at"].endswith("+00:00")
    assert call_store.read_one("voice-twilio-CA1", db) == doc
    assert call_store.read_one("voice-twilio-nope", db) is None

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_search_matches_transcript_and_metadata_without_case(tmp_path):
    db = tmp_path / "calls.sqlite3"
    _write(db, doc_id="voice-a", content="Them: book a TABLE please")
    _write(db, doc_id="voice-b", content="Them: nothing here",
           metadata={"mission": "Confirm the Délivery window", "direction": "outbound"})
    _write(db, doc_id="voice-c", content="Them: unrelated")

    assert [d["id"] for d in call_store.search("table", db)] == ["voice-a"]
    assert [d["id"] for d in call_store.search("DÉLIVERY", db)] == ["voice-b"]
    assert call_store.search("absent words", db) == []


def test_a_missing_archive_is_empty_and_is_not_created_by_reading(tmp_path):
    db = tmp_path / "never-written.sqlite3"
    assert call_store.read_all(db) == []
    assert call_store.read_one("voice-x", db) is None
    assert call_store.search("x", db) == []
    assert not db.exists()


def test_a_failed_write_is_a_reason_not_an_exception(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a file where the archive's directory should be")
    ok, reason = _write(blocker / "calls.sqlite3")
    assert ok is False
    assert reason  # a sentence a human can read, never empty


def test_an_empty_transcript_is_refused_like_hindsight_refuses_it(tmp_path):
    ok, reason = _write(tmp_path / "calls.sqlite3", content="   ")
    assert (ok, reason) == (False, "empty transcript - nothing to retain")


# -- the write seam ------------------------------------------------------------------------


def _retain_events(log):
    return [json.loads(line) for line in log.read_text().splitlines()
            if line.strip() and json.loads(line).get("type") == "retain"]


def _drive(log, *, url, summariser=None, doc_id="voice-twilio-CAseam"):
    async def scenario():
        recorder = eventlog.CallRecorder(
            call_id="CAseam", mode="twilio", pipeline="realtime", direction="inbound",
            outlet="phone", path=str(log))
        status = call_record.retain_call(
            url=url, bank="voice", recorder=recorder,
            transcript=["Them: I need to move my booking to Friday", "AI: Done."],
            document_id=doc_id, platform="voice_twilio", lane="twilio",
            agent="front-desk", outcome="ok", summariser=summariser)
        if status != "dispatched":
            return status
        for _ in range(300):
            await asyncio.sleep(0.01)
            if log.exists() and _retain_events(log):
                break
        return status

    return asyncio.run(scenario())


def test_with_no_hindsight_a_call_lands_in_the_sqlite_archive(tmp_path, monkeypatch):
    db = tmp_path / "calls.sqlite3"
    monkeypatch.setenv("VOICE_ARCHIVE_PATH", str(db))
    monkeypatch.delenv("VOICE_ARCHIVE", raising=False)
    log = tmp_path / "events.jsonl"

    async def summariser(transcript):
        # Ticket 06: runs on the detached task BEFORE the document is written.
        assert not db.exists() or call_store.read_all(db) == []
        return "The caller moved a booking to Friday.", "ok"

    assert _drive(log, url="", summariser=summariser) == "dispatched"

    [event] = _retain_events(log)
    assert event["ok"] is True and event["err"] is None
    assert event["document_id"] == "voice-twilio-CAseam"
    assert event["bank"] is None   # a Hindsight bank name would be a false claim here
    [doc] = call_store.read_all(db)
    assert doc["document_metadata"]["summary"] == "The caller moved a booking to Friday."
    assert doc["document_metadata"]["agent"] == "front-desk"
    assert "voice" in doc["tags"]


def test_with_a_hindsight_url_the_call_goes_to_hindsight_not_sqlite(tmp_path, monkeypatch):
    db = tmp_path / "calls.sqlite3"
    monkeypatch.setenv("VOICE_ARCHIVE_PATH", str(db))
    monkeypatch.delenv("VOICE_ARCHIVE", raising=False)
    posted = []

    async def fake_retain_result(url, bank, **kwargs):
        posted.append((url, bank, kwargs["document_id"]))
        return True, None

    monkeypatch.setattr(hindsight, "retain_result", fake_retain_result)
    log = tmp_path / "events.jsonl"
    assert _drive(log, url="http://hindsight.test:8888") == "dispatched"

    assert posted == [("http://hindsight.test:8888", "voice", "voice-twilio-CAseam")]
    assert not db.exists()
    [event] = _retain_events(log)
    assert event["ok"] is True and event["bank"] == "voice"


def test_a_failing_sqlite_write_is_recorded_and_never_raises(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("VOICE_ARCHIVE_PATH", str(blocker / "calls.sqlite3"))
    monkeypatch.delenv("VOICE_ARCHIVE", raising=False)
    log = tmp_path / "events.jsonl"

    assert _drive(log, url="") == "dispatched"
    [event] = _retain_events(log)
    assert event["ok"] is False
    assert event["err"]


def test_a_misconfigured_backend_fails_the_retain_without_raising(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_ARCHIVE", "postgres")
    log = tmp_path / "events.jsonl"

    assert _drive(log, url="") == "failed"
    [event] = _retain_events(log)
    assert event["ok"] is False
    assert "VOICE_ARCHIVE" in event["err"]


def test_hindsight_chosen_without_a_url_is_skipped_not_sent_to_sqlite(tmp_path, monkeypatch):
    db = tmp_path / "calls.sqlite3"
    monkeypatch.setenv("VOICE_ARCHIVE_PATH", str(db))
    monkeypatch.setenv("VOICE_ARCHIVE", "hindsight")
    assert _drive(tmp_path / "events.jsonl", url="") == "skipped"
    assert not db.exists()


def test_a_cascade_call_with_no_hindsight_is_archived_to_sqlite(tmp_path, monkeypatch):
    """The cascade engine used to skip the retain when it had no Hindsight URL.

    With SQLite as the default archive an empty URL is the ordinary configuration, so the
    engine must hand the call to `call_record.retain_call` and let it decide. Driven through
    the real teardown and the real store, no patched seam.
    """
    from test_cascade_live import (FakeDeepgram, FakeRecorder, FakeTwilioWS,
                                   make_session, make_transport)
    db = tmp_path / "calls.sqlite3"
    monkeypatch.setenv("VOICE_ARCHIVE_PATH", str(db))
    monkeypatch.delenv("VOICE_ARCHIVE", raising=False)
    rec = FakeRecorder()
    session = make_session(FakeTwilioWS(), FakeDeepgram(), rec, transport=make_transport(),
                           hindsight_url="", retain_default=True)
    session.transcript.extend(["Them: is the shop open on Sunday", "AI: From ten."])

    async def run():
        await session.teardown(outcome="ok")
        for _ in range(300):
            await asyncio.sleep(0.01)
            if call_store.read_all(db):
                return

    asyncio.run(run())
    assert rec.finishes[0]["retain_status"] == "dispatched"
    [doc] = call_store.read_all(db)
    assert doc["id"] == "voice-cascade-CA-test"
    assert "cascade" in doc["tags"]
