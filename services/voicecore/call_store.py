"""Where a call's archive document is kept: the built-in SQLite store, or Hindsight.

The call archive (ticket 05) is one document per call: the transcript as ``content``, the
metadata `call_record.build_metadata` wrote, and the tags `call_record.build_tags` wrote.
That document can live in one of two places:

* ``sqlite`` - a single SQLite file next to the event log. Built in, needs no other
  service, and is the default. The bridges write it; the dashboard reads it.
* ``hindsight`` - a Hindsight memory bank, written by `hindsight.retain_result` and read
  by the dashboard's `hindsight_calls`. Hindsight also distils each call into durable
  memory facts, which is why a deployment that runs Hindsight keeps using it.

THE BACKEND RULE (`backend`). ``VOICE_ARCHIVE`` decides when it is set, and it must name
one of the two; anything else refuses loudly instead of quietly archiving somewhere the
operator did not ask for. Unset, a configured ``HINDSIGHT_URL`` means Hindsight, so a
deployment that already sets it keeps archiving exactly where it did with no change. With
neither, calls go to SQLite.

The SQLite side keeps Hindsight's contracts on purpose, so nothing downstream branches on
more than where the bytes live:

* the write returns ``(ok, reason)`` and never raises, like `hindsight.retain_result`;
* metadata values are stringified exactly as Hindsight's retain sends them;
* a document read back has the keys the Hindsight store serves (``id``, ``content``,
  ``document_metadata``, ``tags``, ``created_at``), so the dashboard's formatting,
  filtering, sorting and paging run unchanged over either store.

The file is a multi-UID rendezvous, like the event log: two bridges running as different
users both write it. It is therefore made group/other writable when this module creates
it, and SQLite gives its ``-wal``/``-shm`` side files the database file's own mode.
"""
import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import eventlog

logger = logging.getLogger("voice.call_store")

ARCHIVE_ENV = "VOICE_ARCHIVE"
ARCHIVE_PATH_ENV = "VOICE_ARCHIVE_PATH"

SQLITE = "sqlite"
HINDSIGHT = "hindsight"
BACKENDS = (SQLITE, HINDSIGHT)

DB_FILENAME = "calls.sqlite3"
BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    document_id TEXT PRIMARY KEY,
    content     TEXT NOT NULL,
    metadata    TEXT NOT NULL,
    tags        TEXT NOT NULL,
    created_at  TEXT NOT NULL
)
"""


def backend(env=None, *, hindsight_url=None) -> str:
    """``"sqlite"`` or ``"hindsight"`` - where call documents are written and read.

    ``hindsight_url`` lets a writer pass the URL it was configured with (which it read from
    ``HINDSIGHT_URL``); left as None, the environment is read. Raises ValueError, naming
    the variable, when ``VOICE_ARCHIVE`` holds anything but the two backend names.
    """
    env = os.environ if env is None else env
    raw = (env.get(ARCHIVE_ENV) or "").strip()
    if raw:
        chosen = raw.lower()
        if chosen not in BACKENDS:
            raise ValueError(
                f"{ARCHIVE_ENV} must be 'sqlite' or 'hindsight', not {raw!r}")
        return chosen
    url = env.get("HINDSIGHT_URL") if hindsight_url is None else hindsight_url
    return HINDSIGHT if (url or "").strip() else SQLITE


def sqlite_path(env=None) -> Path:
    """The SQLite archive file: ``VOICE_ARCHIVE_PATH``, else beside the event log.

    The bridges (writers) and the dashboard (reader) share the event log's directory in
    deployment, so the default puts the archive where both already look.
    """
    env = os.environ if env is None else env
    raw = (env.get(ARCHIVE_PATH_ENV) or "").strip()
    if raw:
        return Path(raw)
    eventlog_path = env.get("VOICE_EVENTLOG_PATH") or eventlog.DEFAULT_PATH
    return Path(eventlog_path).parent / DB_FILENAME


def describe(env=None) -> str:
    """A short phrase naming the active archive, for sentences an operator reads."""
    try:
        chosen = backend(env)
    except ValueError as exc:
        return f"a misconfigured archive ({exc})"
    if chosen == HINDSIGHT:
        return "the Hindsight store"
    return f"the local call archive ({sqlite_path(env)})"


# -- write ---------------------------------------------------------------------------------


def _connect_for_write(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(_SCHEMA)
    except Exception:
        conn.close()
        raise
    if not existed:
        try:
            os.chmod(path, 0o666)
        except OSError:  # not ours to chmod - the database itself is usable
            logger.warning("call archive created but chmod failed (path=%s)", path)
    return conn


def _write_sync(path: Path, *, content: str, document_id: str, metadata, tags) -> None:
    meta = {k: str(v) for k, v in (metadata or {}).items()}
    created_at = datetime.now(timezone.utc).isoformat()
    conn = _connect_for_write(path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO calls (document_id, content, metadata, tags, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(document_id) DO UPDATE SET content = excluded.content, "
                "metadata = excluded.metadata, tags = excluded.tags",
                (document_id, content, json.dumps(meta, ensure_ascii=False),
                 json.dumps(list(tags or []), ensure_ascii=False), created_at))
    finally:
        conn.close()


async def write_result(*, content: str, document_id: str, metadata: dict = None,
                       tags: list = None, path=None) -> "tuple[bool, str | None]":
    """Store one call document in the SQLite archive. Returns ``(ok, reason)``.

    The same contract as `hindsight.retain_result`: ``reason`` is None on success and a
    short human-readable failure otherwise, and this never raises. The file work runs on
    a worker thread so a slow or locked database never blocks the event loop.
    """
    if not content or not content.strip():
        return False, "empty transcript - nothing to retain"
    try:
        target = Path(path) if path is not None else sqlite_path()
        await asyncio.to_thread(_write_sync, target, content=content,
                                document_id=document_id, metadata=metadata, tags=tags)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        reason = str(e).strip() or e.__class__.__name__
        logger.warning("call archive write failed (doc=%s): %s", document_id, reason)
        return False, reason
    logger.info("call archive write ok (doc=%s, %d chars)", document_id, len(content))
    return True, None


# -- read ----------------------------------------------------------------------------------


def _connect_for_read(path: Path) -> sqlite3.Connection:
    """Open an existing archive without ever creating one.

    Read-write first, because a reader of a WAL database may need to set up the shared
    memory file; read-only as the fallback for a reader that cannot write the directory.
    """
    uri = Path(path).resolve().as_uri()
    try:
        conn = sqlite3.connect(f"{uri}?mode=rw", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(f"{uri}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def _loads(raw, fallback):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _as_served(row) -> dict:
    """A row in the shape the Hindsight store serves a document."""
    document_id, content, metadata, tags, created_at = row
    return {
        "id": document_id,
        "content": content,
        "document_metadata": _loads(metadata, {}),
        "tags": _loads(tags, []),
        "created_at": created_at,
    }


def _query(path: Path, sql: str, params=()) -> list:
    if not Path(path).exists():
        return []
    conn = _connect_for_read(path)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'calls'").fetchone()
        if not has_table:
            return []
        return [_as_served(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


_COLUMNS = "document_id, content, metadata, tags, created_at"


def read_all(path=None) -> list:
    """Every stored call document. A missing file is an empty archive, not an error.

    Raises (sqlite3.Error, OSError) when the file exists but cannot be read; the caller
    decides how to say so.
    """
    target = Path(path) if path is not None else sqlite_path()
    return _query(target, f"SELECT {_COLUMNS} FROM calls ORDER BY created_at DESC")


def read_one(document_id: str, path=None):
    """One stored call document, or None when the archive does not hold it."""
    target = Path(path) if path is not None else sqlite_path()
    rows = _query(target, f"SELECT {_COLUMNS} FROM calls WHERE document_id = ?",
                  (document_id,))
    return rows[0] if rows else None


def search(query: str, path=None) -> list:
    """The documents whose transcript or any metadata value contains ``query``.

    Case-insensitive (``casefold``, so it holds beyond ASCII). Done over the whole read
    rather than in SQL because the corpus is one line's calls and the dashboard already
    reads all of it to sort and page.
    """
    needle = (query or "").strip().casefold()
    docs = read_all(path)
    if not needle:
        return docs
    hits = []
    for doc in docs:
        haystack = [doc.get("content") or ""]
        haystack.extend(str(v) for v in doc["document_metadata"].values())
        if any(needle in text.casefold() for text in haystack):
            hits.append(doc)
    return hits
