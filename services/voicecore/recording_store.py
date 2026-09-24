"""Reader for the call-recording volume (ticket 07).

The bridges write; the dashboard reads. Both import THIS module so the layout can never
drift between producer and consumer — the same rule ``eventlog`` follows.

Two things live here and nowhere else:

* **Resolution.** A recording is found by CALL ID, never by a path taken from stored
  metadata. The metadata reference exists so a Call carries its recording (ticket 07's
  acceptance list), but a path that arrives from a document store is attacker-shaped
  input, and this module refuses to join it onto the root. The id is sanitised by the
  same ``_safe_call_id`` the writer used and looked up under the two-level date layout.

* **Description.** What the Calls screen needs to choose between a player, a one-line
  note about a failed capture, and nothing at all.

Serving the bytes is NOT here: the dashboard hands the resolved path to Starlette's
``FileResponse``, which already implements RFC 7233 range requests (the scrub bar
depends on them). A second implementation would only be a second thing to get wrong.
"""
import json
import logging
import os
from pathlib import Path

from .recording import DEFAULT_ROOT, _safe_call_id  # one definition of "safe id"

logger = logging.getLogger("voice.recording_store")

CONTENT_TYPE = "audio/ogg"


def root() -> Path:
    """The recordings volume. Read from the env on every call so a test (and the
    dashboard's own env) can point it at a tmpdir without reimporting."""
    return Path(os.environ.get("VOICE_RECORDINGS_DIR", DEFAULT_ROOT))


def _candidates(base: Path, safe: str):
    """Every place the writer could have put this call, newest layout first."""
    yield from sorted(base.glob(f"*/*/{safe}.opus"), reverse=True)


def find(call_id: str, base: Path = None) -> "Path | None":
    """The audio file for ``call_id``, or None. Never raises, never escapes ``base``."""
    safe = _safe_call_id(call_id)
    base = base if base is not None else root()
    try:
        for path in _candidates(base, safe):
            if path.is_file():
                return path
    except Exception:  # noqa: BLE001
        logger.warning("recording lookup failed for %s", call_id, exc_info=True)
    return None


def sidecar(call_id: str, base: Path = None) -> "dict | None":
    """The recording's sidecar document, or None when the call has none.

    A sidecar exists for FAILED and EMPTY captures too — that is how the screen can say
    "this call's recording failed" instead of silently looking like an old call.
    """
    safe = _safe_call_id(call_id)
    base = base if base is not None else root()
    try:
        for path in sorted(base.glob(f"*/*/{safe}.json"), reverse=True):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                logger.warning("unreadable recording sidecar at %s", path, exc_info=True)
                continue
            if isinstance(doc, dict):
                return doc
    except Exception:  # noqa: BLE001
        logger.warning("recording sidecar lookup failed for %s", call_id, exc_info=True)
    return None


def describe(call_id: str, base: Path = None) -> dict:
    """What the Calls screen needs to decide between a player, a note, and nothing.

    ``available`` is True ONLY when a playable file is on the volume right now. A
    reference in a Call's metadata is not evidence the file survived; the screen asks
    this, and this asks the disk.
    """
    path = find(call_id, base=base)
    doc = sidecar(call_id, base=base) or {}
    if path is None:
        status = doc.get("status")
        return {
            "available": False,
            # "failed" is the one case worth a sentence on the screen; "empty",
            # "disabled" and a missing sidecar all mean "no recording", which the
            # screen renders as nothing at all.
            "status": status if status in ("failed",) else None,
            "error": doc.get("error") if status == "failed" else None,
            "url": None,
            "duration_s": None,
            "size_bytes": None,
        }
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    return {
        "available": True,
        "status": "ok",
        "error": None,
        "url": f"/api/calls/{call_id}/recording",
        "duration_s": doc.get("duration_s"),
        "size_bytes": size,
        "sample_rate": doc.get("sample_rate"),
        "channels": doc.get("channels"),
        # Holes exist when the writer could not keep up. Saying so beats a recording
        # that is quietly short of the call it claims to be.
        "dropped_frames": doc.get("dropped_frames") or 0,
    }
