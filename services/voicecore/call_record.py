"""The ONE builder of a retained call's archive metadata (s5, ticket 05).

Before this module the three retainers each hand-rolled their own metadata dict and the
three disagreed (two wrote a date-only `date`, one wrote an `agent`, none wrote an outlet,
a mission, an outcome or a duration). The Calls screen therefore had nothing to show and
nothing to filter on. Every retainer now calls `retain_call` here, so the archive holds one
shape and the reader has one contract to read.

TWO RULES, BOTH LOAD-BEARING
============================

**1. Never guess a field.** A value this module is not GIVEN is left out of the document
entirely. It is never defaulted to something plausible - not an empty string that renders
as a dash, not "unknown" as a literal, not an outlet inferred from the direction or the
transport. A reader that finds no key knows exactly one thing: nothing was recorded. That
is what the screens render. This project has twice shipped a surface that stated something
untrue, and both times the value came from a default that looked like data.

**2. Retention is fire-and-forget.** `retain_call` dispatches and returns; it never awaits
the store. A hung or broken store cannot wedge or drop a live call, because the call is
the product and the archive is a by-product. When the write eventually fails, the failure is
written to the event log as a `retain` record (`CallRecorder.record_retain`) and logged at
WARNING - visible, not swallowed.

**3. The summary is a by-product of a by-product** (ticket 06). When a lane passes a
`summariser`, it runs INSIDE that same detached task, before the document is written, so
the Call reaches the archive already settled - there is no "summary still coming" row for
a screen to spin on. The wait is bounded and total (`_fill_summary`): a summariser that
fails, hangs, errors or returns nothing costs the document its `summary` key and nothing
else. The transcript, the recording reference and every other field are written complete.
`voicecore/summary.py` holds the rest, including why an absent summary says WHY it is
absent.
"""
import asyncio
import functools
import logging
from datetime import datetime, timezone

from . import call_store
from . import hindsight
from . import summary as call_summary

logger = logging.getLogger("voice.call_record")

# A mission brief is free text an owner wrote; it can be a paragraph. Archive metadata
# values are strings, and a table cell is not the place for one. Cut, with the cut VISIBLE:
# a silently shortened brief would read as the whole brief.
MISSION_MAX = 500
# Ticket 06: a summary is meant to be readable in a table cell. The Agent is asked for two
# or three sentences; a model that ignores that is cut, VISIBLY, like a mission brief.
SUMMARY_MAX = 600
TRUNCATION_MARK = " [truncated]"


def _clean(value) -> "str | None":
    """A non-blank string, or None. None is the whole vocabulary for "not recorded"."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _iso_utc(epoch: float) -> "str | None":
    try:
        return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def build_metadata(*, platform: str, outlet: str = None, direction: str = None,
                   target: str = None, caller: str = None, agent: str = None,
                   mission: str = None, outcome: str = None,
                   duration_s: float = None, started_at: float = None,
                   recording: str = None, summary: str = None,
                   summary_state: str = None) -> dict:
    """The metadata for one retained call. Absent keys mean "not recorded" (rule 1).

    ``platform`` is the only mandatory field: it is what `_is_call_doc` in the dashboard
    reader uses to tell a call document from the ordinary memories that share the store.
    """
    meta = {"platform": platform}
    # ``recording`` (ticket 07) is the Call's pointer to its own audio, relative to the
    # recordings volume. Rule 1 applies to it like everything else: a call whose capture
    # failed, or that predates the feature, records NO key rather than a path that names
    # a file nobody can play. The dashboard resolves playability against the volume
    # anyway - this is the Call carrying its own reference, not the proof it survived.
    for key, value in (("outlet", outlet), ("direction", direction), ("target", target),
                       ("caller", caller), ("agent", agent), ("outcome", outcome),
                       ("recording", recording)):
        cleaned = _clean(value)
        if cleaned is not None:
            meta[key] = cleaned

    brief = _clean(mission)
    if brief is not None:
        if len(brief) > MISSION_MAX:
            brief = brief[:MISSION_MAX].rstrip() + TRUNCATION_MARK
        meta["mission"] = brief

    if duration_s is not None:
        try:
            meta["duration_s"] = f"{float(duration_s):.1f}"
        except (TypeError, ValueError):
            pass

    if started_at is not None:
        stamp = _iso_utc(started_at)
        if stamp is not None:
            meta["timestamp"] = stamp

    apply_summary(meta, summary=summary, summary_state=summary_state)
    return meta


def apply_summary(meta: dict, *, summary: str = None, summary_state: str = None) -> dict:
    """Write ticket 06's two summary fields onto a metadata dict, in place.

    Separate from `build_metadata` only because of WHEN the two are known: the rest of the
    metadata is built synchronously in the call's teardown (so a recorder that cannot be
    read fails the retain loudly, right there), while the summary arrives later, inside the
    detached task. Same module, same rules, one place that decides the shape.

    Rule 1 still holds - a summary nobody wrote leaves NO ``summary`` key, and a lane with
    summarisation switched off leaves no ``summary_state`` either ("nobody was asked" is
    not the same fact as "the Agent could not answer").

    The two fields are also kept CONSISTENT here, because a document that carries prose
    under a state saying there is none - or a `written` state with nothing under it - would
    make the Calls screen argue with itself. The text is what actually survived; the state
    is made to agree with it.
    """
    text = _clean(summary)
    if text is not None and len(text) > SUMMARY_MAX:
        text = text[:SUMMARY_MAX].rstrip() + TRUNCATION_MARK
    state = _clean(summary_state)

    if text is not None:
        meta["summary"] = text
        state = call_summary.STATE_WRITTEN
    elif state == call_summary.STATE_WRITTEN:
        state = call_summary.STATE_UNAVAILABLE

    if state is not None:
        meta["summary_state"] = state
    return meta


def build_tags(*, lane: str, direction: str = None, outlet: str = None,
               agent: str = None) -> list:
    """Tags for one retained call, deduplicated, order preserved.

    ``"voice"`` first: the dashboard reader treats it as one of the marks of a call
    document. Unknown values contribute no tag rather than an empty one.
    """
    tags = ["voice"]
    for value in (lane, direction, outlet, agent):
        cleaned = _clean(value)
        if cleaned is not None and cleaned not in tags:
            tags.append(cleaned)
    return tags


def _elapsed_s(recorder) -> "float | None":
    """How long the call has run, or None if the recorder cannot say."""
    try:
        return round(recorder.elapsed_s(), 1)
    except Exception:  # noqa: BLE001
        return None


def _record_retain(recorder, **kwargs) -> None:
    """Write the retain outcome to the event log, if the recorder can take it."""
    try:
        recorder.record_retain(**kwargs)
    except Exception:  # noqa: BLE001
        logger.warning("could not record the retain outcome in the event log", exc_info=True)


async def _fill_summary(metadata: dict, transcript, summariser, timeout_s: float) -> None:
    """Ask the Agent for this call's summary and write it onto the metadata (ticket 06).

    Awaited by `hindsight.retain_detached`'s ``prepare`` hook - on the detached task, after
    the call has ended - so it is off the call path by construction. It is also TOTAL: a
    summariser that fails, hangs, errors, returns nonsense or is not even a coroutine
    function costs the document its ``summary`` key and nothing else, because a summary is
    worth exactly zero calls. That is the enforcement point for ticket 06's first bar;
    `summary.summarise_call` promising not to raise is not enough on its own.
    """
    try:
        summary, state = await asyncio.wait_for(summariser(transcript), timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.warning("call summary timed out after %.0fs - retaining without one", timeout_s)
        summary, state = None, call_summary.STATE_UNAVAILABLE
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.warning("call summary failed - retaining without one", exc_info=True)
        summary, state = None, call_summary.STATE_UNAVAILABLE
    apply_summary(metadata, summary=summary, summary_state=state)


def retain_call(*, url: str, bank: str, recorder, transcript, document_id: str,
                platform: str, lane: str, agent: str = None, mission: str = None,
                outcome: str = None, recording: str = None, summariser=None,
                summary_timeout_s: float = None) -> str:
    """Dispatch one call's transcript into the archive. Returns the retain status.

    Fire-and-forget by construction (rule 2): the retain runs as a detached task, this
    returns immediately, and the caller writes its call record and finishes teardown
    without ever awaiting the store. The eventual result lands in the event log via
    ``recorder.record_retain``.

    NOTHING here raises into the call path. Every retainer calls this from a teardown
    `finally:`, so an exception escaping would take down the very teardown that ends the
    call cleanly. A failure to even DISPATCH is logged, recorded and returned as
    ``"failed"`` - never propagated.

    ``"skipped"`` means nothing was dispatched (no transcript to store, or Hindsight was
    chosen with ``VOICE_ARCHIVE`` but no ``url`` names one). ``"dispatched"`` means the write
    was handed to the background task - NOT that it succeeded. The `retain` event record
    carries that.

    Where the document goes is `call_store.backend`'s call: ``url``/``bank`` are the
    Hindsight coordinates and are ignored when the SQLite archive is active, so an empty
    ``url`` is the ordinary configuration, not a reason to skip.

    ``summariser`` (ticket 06, optional) is an async ``(transcript) -> (summary, state)``
    built by the lane from the Agent that was on the call - see `summary.make_summariser`.
    Passing None means summarisation is off for this lane, and the document then says
    nothing about a summary at all. Note the metadata is still built HERE, synchronously,
    so a recorder that cannot be read still fails the retain loudly at the call site
    rather than silently inside a background task.
    """
    try:
        lines = [line for line in (transcript or []) if line]
        if not lines:
            return "skipped"
        store = call_store.backend(hindsight_url=url)
        if store == call_store.HINDSIGHT and not (url or "").strip():
            logger.warning("call archive is Hindsight but no HINDSIGHT_URL is set - "
                           "not retaining (doc=%s)", document_id)
            return "skipped"
        # The event record's ``bank`` names a Hindsight bank; the SQLite archive has none.
        bank = bank if store == call_store.HINDSIGHT else None

        metadata = build_metadata(
            platform=platform,
            outlet=getattr(recorder, "outlet", None),
            direction=getattr(recorder, "direction", None),
            target=getattr(recorder, "target", None),
            caller=getattr(recorder, "caller", None),
            agent=agent, mission=mission, outcome=outcome,
            duration_s=_elapsed_s(recorder),
            started_at=getattr(recorder, "start_ts", None),
            recording=recording,
        )
        tags = build_tags(lane=lane, direction=getattr(recorder, "direction", None),
                          outlet=getattr(recorder, "outlet", None), agent=agent)

        def _settled(ok, reason):
            _record_retain(recorder, ok=ok, document_id=document_id, bank=bank,
                           reason=reason)

        # Ticket 06: the summary is asked for on the DETACHED retain task, before the
        # document is written - never here, and never on the call path. `summariser` is
        # None when summarisation is off for this lane, and then the document says nothing
        # about a summary at all, because nobody was asked.
        prepare = None
        if summariser is not None:
            timeout_s = (call_summary.timeout_from_env() if summary_timeout_s is None
                         else summary_timeout_s)
            prepare = functools.partial(_fill_summary, metadata, lines, summariser,
                                        timeout_s)

        hindsight.retain_detached(
            url, bank, content="\n".join(lines), document_id=document_id,
            metadata=metadata, tags=tags, on_result=_settled, prepare=prepare)
        return "dispatched"
    except Exception as exc:  # noqa: BLE001
        reason = str(exc).strip() or exc.__class__.__name__
        logger.warning("retain could not be dispatched (bank=%s, doc=%s): %s",
                       bank, document_id, reason, exc_info=True)
        _record_retain(recorder, ok=False, document_id=document_id, bank=bank,
                       reason=f"retain was never dispatched: {reason}")
        return "failed"
