"""The Calls screen's read side of the call archive.

The archive lives in one of two stores, and `voicecore.call_store.backend` says which:

* **SQLite** (the default): one file beside the event log, written by the bridges through
  `voicecore.call_store`. Read here in one go - there are no banks and no recall; search
  is a case-insensitive match over each transcript and its metadata values.
* **Hindsight** (when ``HINDSIGHT_URL`` is set, or ``VOICE_ARCHIVE=hindsight``): the
  configured bank plus the other known voice bank, with search through Hindsight's own
  recall. Everything below about banks, bounds and recall is about this store.

Both stores hand back documents in the SAME served shape (``id``, ``content``,
``document_metadata``, ``tags``, ``created_at``), so formatting, the call-document test,
sorting, filtering and paging below are one code path for both, and the response keys
are identical whichever store answered.

⚠️ THE STORE SERVES ``document_metadata``, NOT ``metadata``. ``metadata`` is the
key the retainers POST; a document Hindsight hands back has no such key. Read it
through ``doc_metadata`` (which takes either) and never with ``doc.get("metadata")``.
Reading the POST key made every screen render every retained field as "not
retained" while the store held it - see that function's docstring for the live
evidence, and `tests/test_live_document_shape.py` for the tests that pin it.

THE HONESTY RULE. Read the producers rather than this module's tests if you
doubt what a call document holds. Since ticket 05 all three retainers build
their metadata through ONE builder:

    services/voicecore/call_record.py           build_metadata / build_tags
    services/voice/server.py                    _maybe_retain            (phone, realtime)
    services/voicecore/cascade_live.py          teardown                 (phone/talk, cascade)
    services/talk-voice-bridge/realtime_bridge.py  run                   (talk, realtime)

That builder OMITS any field it was not given, so a key that is absent means
exactly one thing: nothing was recorded. This module relays that as ``None`` and
the UIs render "not retained". It never substitutes a value that reads like
data, and it never derives a claim about the call (finished cleanly / did not)
from the absence of a field.

Two absences are worth naming, because filling either would be a lie the screen
would repeat:

* **A pre-ticket-05 document** carries no outlet, mission, outcome or duration
  (and no agent either, except from the cascade retainer, which wrote one from
  the start). Those render as not retained. The Outlet in particular is NOT
  back-filled from ``platform``: "voice_twilio" says which transport carried the
  call, not which Outlet it was assigned to, and the two are different axes.
* **A summary** is written by the Agent that was on the call (ticket 06), but only
  when there was a real conversation to describe. ``summary_state`` says which
  absence this is; see ``services/voicecore/summary.py``. There is no "still
  coming" state to render: the summary is settled before the call's document is
  written, so a Call in the archive is a Call whose summary question is closed.

PAGING DECISION (deliberate, not accidental). Hindsight's documents endpoint
offers no ordering and no filtering over our metadata, so a newest-first list
across two banks cannot be produced by asking either bank for one page. This
module therefore fetches every document in every queried bank on every page
request, sorts in Python and slices. Page 13 of 250 really does read all 250
documents. That is accepted at the corpus size this dashboard serves (one phone
line, hundreds of documents) because it is the only way to get a truthful total
and a correct global order. It is bounded three ways -- a page cap per bank, a
wall-clock deadline for the whole fetch, and a stop as soon as a page returns no
document this request has not already seen (which is what a store that ignores
``offset`` does). When any bound is hit the response says so via ``partial``
instead of silently reporting a short total. If the corpus ever grows past a few
thousand documents this has to move to server-side ordering.
"""
import asyncio
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone

import httpx

from voicecore import call_store

logger = logging.getLogger("voice.hindsight_calls")

# No default store address: an unset HINDSIGHT_URL means "no Hindsight", and the archive is
# then the SQLite file (see `voicecore.call_store`).
DEFAULT_HINDSIGHT_URL = ""
DEFAULT_HINDSIGHT_BANK = "voice"

_NO_HINDSIGHT_URL = "Call archive misconfigured (VOICE_ARCHIVE is 'hindsight' but HINDSIGHT_URL is not set)"

# Bounds on the fetch-everything strategy documented above.
PAGE_LIMIT = 100
MAX_PAGES_PER_BANK = 50
FETCH_DEADLINE_S = 20.0
REQUEST_TIMEOUT_S = 5.0


def _now() -> float:
    """The fetch deadline's clock, as one seam.

    Tests that exercise the deadline drive this instead of sleeping: a test that
    shortens FETCH_DEADLINE_S to a fraction of a second and relies on wall-clock
    races process start-up, and failed 6 times in 10 on a loaded machine.
    """
    return time.monotonic()


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The filter value that means "the calls where this field was NOT retained" (s5).
# It cannot collide with a real value: an agent id matches `profiles._ID_RE`
# (`^[a-z0-9][a-z0-9._-]*$`) and an Outlet is one of `profiles.OUTLETS`, so neither
# can contain a space or a parenthesis. Without it the commonest bucket on the
# screen -- every call retained before this ticket -- would be unfilterable.
UNKNOWN_FILTER = "(not retained)"

# Per-bank query outcomes.
BANK_OK = "ok"          # the bank answered
BANK_ABSENT = "absent"  # the store answered 404: this bank does not exist
BANK_ERROR = "error"    # the bank could not be read


def get_hindsight_url() -> str:
    return os.environ.get("HINDSIGHT_URL", DEFAULT_HINDSIGHT_URL).rstrip("/")


def get_hindsight_bank() -> str:
    return os.environ.get("HINDSIGHT_BANK", DEFAULT_HINDSIGHT_BANK)


def get_banks_to_search() -> List[str]:
    """Return list of banks to query symmetrically."""
    primary = get_hindsight_bank()
    banks = [primary]
    secondary = "hermes" if primary == "voice" else "voice"
    if secondary not in banks:
        banks.append(secondary)
    return banks


def _describe_exc(exc: BaseException) -> str:
    """A transport failure the owner can read.

    ``str(httpx.ConnectError())`` is the empty string, which used to produce
    "bank 'voice' failed: " -- a sentence that trails off after the colon.
    """
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _when_precision(val: Any) -> Optional[str]:
    """How much of a timestamp the store actually held.

    ``metadata["date"]`` is a ``%Y-%m-%d`` string (server.py:1103,
    realtime_bridge.py:197). Parsing it yields midnight, and a screen that
    prints midnight has invented a time of day the store never held. Callers
    render a "date" value as a date and nothing more.
    """
    if val is None or val == "":
        return None
    if isinstance(val, str) and _DATE_ONLY_RE.match(val.strip()):
        return "date"
    return "datetime"


def _parse_ts(val: Any) -> Optional[float]:
    """Parse a timestamp into an epoch float for sorting and the legacy screen.

    A stamp with no UTC offset is assumed to be UTC. It used to be resolved in
    the SERVER's timezone, while the React screen parsed the same raw string
    with ``new Date()`` -- which resolves it in the BROWSER's. One call then had
    two clock times, and far enough apart, two calendar days. Nothing retained a
    zone, so the choice is arbitrary; what matters is that it is made once, here,
    and that ``_normalize_when`` hands the screens the same one.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str) and val.strip():
        try:
            return float(val)
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass
    return None


def _parse_duration(val: Any) -> Optional[float]:
    """A retained duration as a number, or None when there is nothing to read.

    ``call_record.build_metadata`` writes it as a one-decimal string because
    Hindsight stringifies every metadata value anyway. A value that will not parse
    is None -- not 0.0, which would render as a call that lasted no time at all.
    """
    if val is None or isinstance(val, bool):
        return None
    try:
        seconds = float(val)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return seconds


def _normalize_when(val: Any) -> Any:
    """Hand both screens one unambiguous instant, whatever shape the store sent.

    The two screens read different fields -- the legacy one renders ``start_ts``,
    the React one parses ``when`` -- so any stamp they could resolve differently
    has to be settled here or they disagree about the same call. Two shapes do:

    * **no UTC offset.** ``_parse_ts`` would resolve it in the SERVER's timezone
      and ``new Date()`` in the BROWSER's. Assumed UTC; the choice is arbitrary,
      making it once is not.
    * **an epoch number** (or a numeric string). ``_parse_ts`` accepts these
      explicitly, but ``new Date(1787010240.0)`` reads a bare number as
      MILLISECONDS and prints January 1970 for a 2026 call -- a value that reads
      like retained data, with the truth two lines away.

    Date-only stamps are returned untouched: they hold no clock time to anchor,
    and ``when_precision`` already tells both screens to render them as a date
    and nothing more.
    """
    if isinstance(val, bool):  # bool is an int; not a timestamp
        return val
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(float(val), timezone.utc).isoformat()
    if not isinstance(val, str) or not val.strip():
        return val
    text = val.strip()
    if _DATE_ONLY_RE.match(text):
        return val
    # Numeric strings are epochs before they are anything else, which is the
    # order `_parse_ts` already reads them in -- the two must not disagree.
    try:
        return datetime.fromtimestamp(float(text), timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return val
    if dt.tzinfo is not None:
        return val
    return dt.replace(tzinfo=timezone.utc).isoformat()


def doc_metadata(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The metadata off a Hindsight document, under the key the STORE serves it on.

    ``metadata`` is what we POST (`hindsight.retain_result` builds
    ``item["metadata"]``). It is NOT what we GET: every document the store hands
    back - from the list endpoint and from fetch-by-id, on the `voice` bank and on
    `hermes` - carries its fields under ``document_metadata`` and has no
    ``metadata`` key at all. Verified against the live store on 2026-08-19; a
    listed voice document's top-level keys are::

        bank_id content_hash created_at document_metadata id memory_unit_count
        retain_params tags text_length updated_at

    Reading the POST key cost the screens EVERY retained field - ticket 05's
    agent/outlet/outcome/duration, ticket 07's recording and ticket 06's
    summary - and rendered all of them as "not retained" while the store held
    them. The fixtures could not see it because they were built from the
    producers' POST bodies, which is the "fixture agreed with the code" failure
    this module's own docstring warns about.

    Both keys are accepted, ``document_metadata`` first: the store's shape wins,
    and a POST-shaped document (what our own tests and any future replay tool
    hand in) still reads.
    """
    for key in ("document_metadata", "metadata"):
        value = doc.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def format_call_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format a raw Hindsight document into a standard Call payload for the Calls API."""
    metadata = doc_metadata(doc)

    doc_id = doc.get("id") or doc.get("document_id") or ""
    content = doc.get("original_text") or doc.get("content") or doc.get("text") or ""

    # Prefer full document-level timestamp over date-only metadata.date
    when = _normalize_when(
        doc.get("created_at")
        or doc.get("timestamp")
        or doc.get("created_timestamp")
        or metadata.get("timestamp")
        or metadata.get("date")
        or ""
    )

    direction = metadata.get("direction") or "unknown"
    caller = metadata.get("caller") or ""
    target = metadata.get("target") or ""
    who_meta = metadata.get("who") or metadata.get("other_party") or ""

    if direction == "inbound":
        who = caller or target or who_meta or "Not retained"
    else:
        who = target or caller or who_meta or "Not retained"

    agent = metadata.get("agent") or ""
    # NOT `or metadata.get("platform")`. The platform says which transport carried
    # the call ("voice_twilio"); the Outlet says which channel it was ASSIGNED to
    # ("phone"). Reading one as the other put "voice_twilio" in an Outlet column on
    # every pre-ticket-05 row, which is a guess in a field the owner filters on.
    outlet = metadata.get("outlet") or ""
    mission = metadata.get("mission") or ""
    duration_s = _parse_duration(metadata.get("duration_s"))

    # Relayed if retained, None if not. Nothing is inferred from their absence:
    # a Hindsight document exists BECAUSE the call reached teardown with a
    # transcript, so "no summary in Hindsight" is not evidence of a bad call.
    outcome = metadata.get("outcome") or doc.get("outcome") or None
    summary = metadata.get("summary") or doc.get("summary") or None
    # Ticket 06: WHY there is no summary, which is a different fact from there being
    # none. "nothing_to_summarise" is a statement about the call (nobody said enough to
    # describe); "unavailable" is a gap in the record (the Agent was asked and could not
    # answer); no state at all means nobody was asked. The screen renders the three
    # differently, and none of them as prose that could be mistaken for a summary.
    summary_state = metadata.get("summary_state") or None
    # Ticket 07: the Call's reference to its own audio, relayed verbatim when a producer
    # wrote one. It is a POINTER, not proof -- /api/calls/{id} resolves playability
    # against the recordings volume, because a reference can outlive the file it names.
    recording_ref = metadata.get("recording") or None
    err = metadata.get("err") or metadata.get("error") or None

    return {
        "call_id": doc_id,
        "when": when,
        # "datetime", "date" (only metadata.date was retained -- no clock time
        # exists for this call), or None when nothing was retained at all.
        "when_precision": _when_precision(when),
        "direction": direction,
        "who": who,
        "caller": caller,
        "target": target,
        "agent": agent,
        "outlet": outlet,
        "platform": metadata.get("platform", ""),
        "mission": mission,
        "outcome": outcome,
        "summary": summary,
        "summary_state": summary_state,
        "recording_ref": recording_ref,
        "transcript": content,
        "mode": outlet or metadata.get("platform", ""),
        "start_ts": _parse_ts(when),
        # Retained since ticket 05; None on anything retained before it, and None
        # is rendered as not retained -- never 0 and never "-".
        "duration_s": duration_s,
        # Still retained by nobody: Hindsight holds one document per call, never
        # per-turn records.
        "num_turns": None,
        "err": err,
        # The legacy schema's "turns exist but no call summary was written".
        # Hindsight retains neither turns nor summaries, so this is unknowable
        # here: None, which is neither an accusation nor an all-clear.
        "incomplete": None,
    }


def _is_call_doc(doc: Dict[str, Any]) -> bool:
    """True for documents the voice retainers wrote.

    The fallback bank is Hermes's general memory bank; the documents in it that
    are not calls must not be listed as calls. All three retainers write a
    ``voice-`` document id, a ``voice`` tag, a ``voice_*`` platform and a
    direction, so any one of those identifies a call.
    """
    doc_id = doc.get("id") or doc.get("document_id") or ""
    tags = doc.get("tags") or []
    metadata = doc_metadata(doc)
    return bool(
        str(doc_id).startswith("voice-")
        or (isinstance(tags, list) and "voice" in tags)
        or str(metadata.get("platform", "")).startswith("voice")
        or "direction" in metadata
    )


async def _fetch_all_docs_from_bank(
    client: httpx.AsyncClient, base_url: str, bank: str, deadline: float
) -> Tuple[List[Dict[str, Any]], str, Optional[str], bool]:
    """Page through a bank's documents.

    Returns ``(docs, status, error_detail, truncated)``. ``status`` is one of
    BANK_OK, BANK_ABSENT (the store answered 404 for this bank) or BANK_ERROR.
    ``truncated`` means the bank answered but a bound stopped us before the end,
    so ``docs`` is a prefix of the bank, not the bank.
    """
    docs: List[Dict[str, Any]] = []
    seen_ids = set()
    offset = 0
    truncated = False

    for _ in range(MAX_PAGES_PER_BANK):
        if _now() >= deadline:
            return docs, BANK_OK, None, True
        try:
            resp = await client.get(
                f"{base_url}/v1/default/banks/{bank}/documents",
                params={"limit": PAGE_LIMIT, "offset": offset},
            )
        except Exception as exc:
            return docs, BANK_ERROR, _describe_exc(exc), truncated

        if resp.status_code == 404:
            # A bank that does not exist is not a store outage. Nothing has ever
            # written to the 'voice' bank, so under the shipping configuration
            # (HINDSIGHT_BANK=hermes) this is the expected answer for it.
            return docs, BANK_ABSENT, None, truncated
        if not resp.is_success:
            return docs, BANK_ERROR, f"HTTP {resp.status_code}", truncated

        try:
            data = resp.json()
        except Exception as exc:
            return docs, BANK_ERROR, f"unreadable JSON: {_describe_exc(exc)}", truncated

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("items") or data.get("documents") or []
        else:
            items = []
        if not isinstance(items, list):
            break

        fresh = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id") or item.get("document_id") or ""
            if item_id and item_id in seen_ids:
                continue
            if item_id:
                seen_ids.add(item_id)
            docs.append(item)
            fresh += 1

        if fresh == 0:
            # Either the bank is exhausted, or the store honours `limit`, ignores
            # `offset`, and would hand us page 1 forever. Stop either way -- and
            # if it handed back a FULL page with nothing new in it, it is the
            # second case and there is more history we cannot reach.
            if offset > 0 and len(items) >= PAGE_LIMIT:
                truncated = True
            break
        if len(items) < PAGE_LIMIT:
            break
        total_reported = data.get("total") if isinstance(data, dict) else None
        if isinstance(total_reported, int) and len(docs) >= total_reported:
            break
        offset += len(items)
    else:
        # Ran out of pages before the bank ran out of documents.
        truncated = True

    return docs, BANK_OK, None, truncated


def _bank_warnings(bank_states: List[Dict[str, Any]], primary: str) -> List[str]:
    """Sentences naming exactly what the owner is not being shown, and why."""
    warnings: List[str] = []
    for state in bank_states:
        if state["status"] == BANK_ERROR:
            warnings.append(
                f"Bank '{state['bank']}' could not be read ({state['error_detail']}). "
                "Calls retained there are not shown."
            )
        elif state["status"] == BANK_ABSENT and state["bank"] == primary:
            # An absent secondary bank is ordinary. An absent configured bank is
            # a deployment problem the owner needs to see.
            warnings.append(
                f"The configured bank '{primary}' does not exist in the Hindsight store."
            )
        elif state["truncated"]:
            if state["docs"]:
                warnings.append(
                    f"Only the first {len(state['docs'])} documents of bank "
                    f"'{state['bank']}' were read; older calls are not shown."
                )
            else:
                warnings.append(
                    f"Bank '{state['bank']}' was not read within the time limit; "
                    "no calls from it are shown."
                )
    return warnings


def _error_envelope(page: int, page_size: int, message: str) -> Dict[str, Any]:
    return {
        "calls": [],
        "total": 0,
        "page": page,
        "page_size": page_size,
        "has_more": False,
        "unreachable": True,
        "error": message,
        "warning": None,
        "partial": False,
        "exists": False,
        "skipped": 0,
        "agents": [],
        "outlets": [],
        "filters": {"agent": None, "outlet": None},
    }


async def _recall_bank(
    client: httpx.AsyncClient, url: str, bank: str, search_term: str
) -> Tuple[List[str], str, Optional[str]]:
    """Recall one bank. Returns (document_ids, status, error_detail)."""
    doc_ids: List[str] = []
    detail: Optional[str] = None

    def collect(payload: Any) -> None:
        results = []
        if isinstance(payload, dict):
            results = payload.get("results") or payload.get("items") or payload.get("memories") or []
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    doc_id = item.get("document_id") or item.get("doc_id") or item.get("id")
                    if doc_id:
                        doc_ids.append(doc_id)

    absent = False
    try:
        resp = await client.post(
            f"{url}/v1/default/banks/{bank}/memories/recall",
            json={"query": search_term, "q": search_term},
        )
        if resp.is_success:
            collect(resp.json())
            return doc_ids, BANK_OK, None
        if resp.status_code == 404:
            absent = True
        else:
            detail = f"HTTP {resp.status_code}"
    except Exception as exc:
        detail = _describe_exc(exc)

    # Older Hindsight builds only expose GET /recall.
    try:
        resp = await client.get(f"{url}/v1/default/banks/{bank}/recall", params={"q": search_term})
        if resp.is_success:
            collect(resp.json())
            return doc_ids, BANK_OK, None
        if resp.status_code == 404 and absent:
            return doc_ids, BANK_ABSENT, None
        if resp.status_code != 404:
            detail = f"HTTP {resp.status_code}"
    except Exception as exc:
        detail = detail or _describe_exc(exc)

    if absent and detail is None:
        return doc_ids, BANK_ABSENT, None
    return doc_ids, BANK_ERROR, detail or "no recall endpoint answered"


def _facet(calls: List[Dict[str, Any]], field: str) -> List[str]:
    """The values of ``field`` present in this corpus, sorted, plus the unknown
    bucket when some call has none.

    Built from the calls that were actually read, so the dropdown can only offer a
    filter that has something behind it. A hard-coded list of Agents would offer
    filters that return nothing and hide Agents the screen has never heard of.
    """
    values = sorted({(call.get(field) or "") for call in calls} - {""})
    if any(not call.get(field) for call in calls):
        values.append(UNKNOWN_FILTER)
    return values


def _matches(call: Dict[str, Any], field: str, wanted: Optional[str]) -> bool:
    if not wanted:
        return True
    value = call.get(field) or ""
    if wanted == UNKNOWN_FILTER:
        return value == ""
    return value == wanted


async def list_calls(
    page: int = 1, page_size: int = 20, q: Optional[str] = None,
    agent: Optional[str] = None, outlet: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch paged list of calls or search using Hindsight's own recall.

    One bank failing never discards what the other bank returned: the calls that
    were fetched are shown, with a warning naming the bank that was not read.
    Only a request where no bank answered at all is reported as unreachable.

    ``agent`` and ``outlet`` filter the result (s5). The store cannot filter over
    our metadata -- see the paging decision above -- so this happens here, after
    the fetch, which means the bounds and the warnings are exactly the ones an
    unfiltered request gets. ``agents``/``outlets`` in the response are the values
    present in what was READ, so a bounded read offers the filters it can honour
    and the `partial` warning still says the read was short.
    """
    try:
        store = call_store.backend()
    except ValueError as exc:
        return _error_envelope(max(1, page), max(1, min(100, page_size)),
                               f"Call archive misconfigured ({exc})")
    if store == call_store.SQLITE:
        return await _list_calls_sqlite(page=page, page_size=page_size, q=q,
                                        agent=agent, outlet=outlet)

    url = get_hindsight_url()
    primary = get_hindsight_bank()
    banks_to_try = get_banks_to_search()

    page = max(1, page)
    page_size = max(1, min(100, page_size))
    if not url:
        return _error_envelope(page, page_size, _NO_HINDSIGHT_URL)

    bank_states: List[Dict[str, Any]] = []
    all_raw_docs: List[Dict[str, Any]] = []
    unresolved_hits = 0
    deadline = _now() + FETCH_DEADLINE_S

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            if q and q.strip():
                search_term = q.strip()
                recalled_doc_ids: List[str] = []

                for bank in banks_to_try:
                    doc_ids, status, detail = await _recall_bank(client, url, bank, search_term)
                    recalled_doc_ids.extend(doc_ids)
                    bank_states.append(
                        {
                            "bank": bank,
                            "status": status,
                            "error_detail": detail,
                            "docs": [],
                            "truncated": False,
                        }
                    )

                for doc_id in list(dict.fromkeys(recalled_doc_ids)):
                    resolved = False
                    for bank in banks_to_try:
                        try:
                            resp = await client.get(
                                f"{url}/v1/default/banks/{bank}/documents/{doc_id}"
                            )
                        except Exception:
                            continue
                        if resp.is_success:
                            try:
                                doc_data = resp.json()
                            except Exception:
                                continue
                            if isinstance(doc_data, dict):
                                all_raw_docs.append(doc_data)
                                resolved = True
                                break
                    if not resolved:
                        # A recall hit whose document could not be read. Counted,
                        # never rendered as a call with no data in it.
                        unresolved_hits += 1
            else:
                for bank in banks_to_try:
                    docs, status, detail, truncated = await _fetch_all_docs_from_bank(
                        client, url, bank, deadline
                    )
                    bank_states.append(
                        {
                            "bank": bank,
                            "status": status,
                            "error_detail": detail,
                            "docs": docs,
                            "truncated": truncated,
                        }
                    )
                    all_raw_docs.extend(docs)
    except Exception as exc:
        logger.warning("Hindsight store unreachable: %s", exc)
        return _error_envelope(
            page, page_size, f"Hindsight store unreachable ({_describe_exc(exc)})"
        )

    answered = [s for s in bank_states if s["status"] in (BANK_OK, BANK_ABSENT)]
    failed = [s for s in bank_states if s["status"] == BANK_ERROR]

    if not answered:
        details = "; ".join(f"bank '{s['bank']}': {s['error_detail']}" for s in failed)
        return _error_envelope(page, page_size, f"Hindsight store error ({details})")

    warnings = _bank_warnings(bank_states, primary)

    if unresolved_hits:
        # On the search path `total` is "the hits we could resolve", so a hit
        # whose document could not be read makes the total short of the truth.
        # Counting it into `skipped` alone is not enough: the React screen -- the
        # only one with a search box -- does not render `skipped`, so the hit
        # would vanish without a word.
        warnings.append(
            f"{unresolved_hits} search hit"
            f"{'' if unresolved_hits == 1 else 's'} could not be read from the "
            f"store, so {'it is' if unresolved_hits == 1 else 'they are'} not "
            "shown and the count below is short by that many."
        )

    return _assemble_list(all_raw_docs, page=page, page_size=page_size,
                          agent=agent, outlet=outlet, warnings=warnings,
                          skipped=unresolved_hits)


async def _list_calls_sqlite(
    page: int, page_size: int, q: Optional[str],
    agent: Optional[str], outlet: Optional[str],
) -> Dict[str, Any]:
    """`list_calls` over the SQLite archive.

    One read of the whole file (the same fetch-everything decision as above, without
    the bounds, because a local file has no network to wait on). A missing file is an
    archive nothing has been written to yet: an empty list, not an error. A file that
    exists and cannot be read is an error envelope naming the file and the fault.
    """
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    path = call_store.sqlite_path()
    try:
        if q and q.strip():
            docs = await asyncio.to_thread(call_store.search, q.strip(), path)
        else:
            docs = await asyncio.to_thread(call_store.read_all, path)
    except Exception as exc:
        logger.warning("call archive unreadable (%s): %s", path, exc)
        return _error_envelope(
            page, page_size,
            f"Call archive could not be read ({path}: {_describe_exc(exc)})")
    return _assemble_list(docs, page=page, page_size=page_size, agent=agent,
                          outlet=outlet, warnings=[], skipped=0)


def _assemble_list(
    all_raw_docs: List[Any], *, page: int, page_size: int,
    agent: Optional[str], outlet: Optional[str], warnings: List[str], skipped: int,
) -> Dict[str, Any]:
    """Dedupe, keep call documents, sort, facet, filter and page - for either store.

    ``warnings`` are the store's sentences about what it could not read; ``skipped``
    starts at the count of hits the store could not resolve.
    """
    offset = (page - 1) * page_size
    calls = []
    seen_ids = set()
    for doc in all_raw_docs:
        if not isinstance(doc, dict):
            skipped += 1
            continue
        doc_id = doc.get("id") or doc.get("document_id") or ""
        if not doc_id:
            skipped += 1
            continue
        if doc_id in seen_ids:
            continue
        if not _is_call_doc(doc):
            skipped += 1
            continue
        seen_ids.add(doc_id)
        calls.append(format_call_doc(doc))

    calls.sort(key=lambda c: c.get("start_ts") or 0.0, reverse=True)

    # Facets come from the WHOLE read, before filtering: a dropdown that shrank to
    # the value you already picked could not be used to pick another one.
    agent_values = _facet(calls, "agent")
    outlet_values = _facet(calls, "outlet")

    agent = (agent or "").strip() or None
    outlet = (outlet or "").strip() or None
    if agent or outlet:
        calls = [c for c in calls
                 if _matches(c, "agent", agent) and _matches(c, "outlet", outlet)]

    total = len(calls)
    paged_calls = calls[offset : offset + page_size]
    has_more = (offset + page_size) < total

    return {
        "calls": paged_calls,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": has_more,
        "unreachable": False,
        "error": None,
        "warning": " ".join(warnings) if warnings else None,
        "partial": bool(warnings),
        "exists": True,
        "skipped": skipped,
        # s5: what the screen may offer as a filter, and what it is filtering by.
        "agents": agent_values,
        "outlets": outlet_values,
        "filters": {"agent": agent, "outlet": outlet},
    }


async def get_call(call_id: str) -> Dict[str, Any]:
    """Fetch single call verbatim transcript & metadata by call ID.

    A bank that could not be read is never reported to the owner as proof the
    call does not exist -- 404 from a bank means "not in this bank", but HTTP 503
    from a bank means "unknown", and the two must not be collapsed. Neither is
    an incomplete READ: the fallback scan below pages the whole bank, bounded
    exactly as the list is, and says "it may exist beyond what was read" when a
    bound stopped it. A single unpaged page of 100 used to answer "not found"
    for any call past the newest 100 documents -- a call the list screen had
    just rendered.

    Over the SQLite archive there are no banks: the document is in the file or it is
    not, and a file that cannot be read is said to be unreadable, never "not found".
    """
    try:
        store = call_store.backend()
    except ValueError as exc:
        return _detail_error_response(f"Call archive misconfigured ({exc})",
                                      unreachable=True)
    if store == call_store.SQLITE:
        return await _get_call_sqlite(call_id)

    url = get_hindsight_url()
    banks_to_try = get_banks_to_search()
    if not url:
        return _detail_error_response(_NO_HINDSIGHT_URL, unreachable=True)

    answered: set = set()
    bank_errors: Dict[str, str] = {}
    incomplete_scans: List[str] = []
    deadline = _now() + FETCH_DEADLINE_S

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
            # Pass 1: ask each bank for the document directly. One request per
            # bank, and it is the whole story for a store that has fetch-by-id.
            for bank in banks_to_try:
                try:
                    resp = await client.get(
                        f"{url}/v1/default/banks/{bank}/documents/{call_id}"
                    )
                except Exception as exc:
                    bank_errors[bank] = _describe_exc(exc)
                    continue
                if resp.is_success:
                    try:
                        doc = resp.json()
                    except Exception:
                        doc = None
                    if isinstance(doc, dict):
                        return _format_detail_response(format_call_doc(doc))
                    answered.add(bank)
                elif resp.status_code == 404:
                    # Either "not in this bank" or "no such endpoint". Pass 2
                    # tells them apart; a 404 alone proves neither.
                    pass
                else:
                    bank_errors[bank] = f"HTTP {resp.status_code}"

            # Pass 2: the store may expose no fetch-by-id at all. Scan the
            # bank's documents -- all of them, not the first page.
            for bank in banks_to_try:
                docs, status, detail, truncated = await _fetch_all_docs_from_bank(
                    client, url, bank, deadline
                )
                if status in (BANK_OK, BANK_ABSENT):
                    answered.add(bank)
                    bank_errors.pop(bank, None)
                    for doc in docs:
                        if doc.get("id") == call_id or doc.get("document_id") == call_id:
                            return _format_detail_response(format_call_doc(doc))
                    if truncated:
                        incomplete_scans.append(bank)
                elif bank not in answered:
                    bank_errors[bank] = detail or "could not be read"
    except Exception as exc:
        logger.warning("Hindsight store unreachable during call fetch: %s", exc)
        return _detail_error_response(
            f"Hindsight store unreachable ({_describe_exc(exc)})", unreachable=True
        )

    bank_failures = [
        f"bank '{bank}' could not be read ({detail})"
        for bank, detail in bank_errors.items()
        if bank not in answered
    ]

    if not answered:
        return _detail_error_response(
            "Hindsight store error (" + "; ".join(bank_failures) + ")", unreachable=True
        )

    if bank_failures:
        # The call was not in the banks that answered, but a bank could not be
        # read at all, so this is "unknown", not "does not exist".
        return _detail_error_response(
            f"Call '{call_id}' was not found in the banks that answered, and "
            + "; ".join(bank_failures)
            + ". It may exist there.",
            unreachable=False,
            partial=True,
        )

    if incomplete_scans:
        # Every bank answered, but a bound stopped the scan before the end of
        # one. What was read did not hold the call; what was not read might.
        named = ", ".join(f"'{bank}'" for bank in incomplete_scans)
        return _detail_error_response(
            f"Call '{call_id}' was not found in what could be read of "
            f"bank{'' if len(incomplete_scans) == 1 else 's'} {named}, which "
            "could not be read to the end. It may exist beyond what was read.",
            unreachable=False,
            partial=True,
        )

    return _detail_error_response(f"Call '{call_id}' not found", unreachable=False)


async def _get_call_sqlite(call_id: str) -> Dict[str, Any]:
    path = call_store.sqlite_path()
    try:
        doc = await asyncio.to_thread(call_store.read_one, call_id, path)
    except Exception as exc:
        logger.warning("call archive unreadable during call fetch (%s): %s", path, exc)
        return _detail_error_response(
            f"Call archive could not be read ({path}: {_describe_exc(exc)})",
            unreachable=True)
    if doc is None:
        return _detail_error_response(f"Call '{call_id}' not found", unreachable=False)
    return _format_detail_response(format_call_doc(doc),
                                   transcript_detail=SQLITE_TRANSCRIPT_DETAIL)


# What the detail says about where its transcript came from, per store.
HINDSIGHT_TRANSCRIPT_DETAIL = "Retained Hindsight verbatim document transcript"
SQLITE_TRANSCRIPT_DETAIL = "Retained verbatim call transcript (local call archive)"


def _detail_error_response(
    message: str, *, unreachable: bool, partial: bool = False
) -> Dict[str, Any]:
    return {
        "call": None,
        "summary": None,
        "turns": [],
        "turns_retained": False,
        "transcript": {"status": "none", "detail": message},
        "unreachable": unreachable,
        "partial": partial,
        "error": message,
        "skipped": 0,
        "incomplete": None,
    }


def _format_detail_response(
    c_doc: Dict[str, Any], transcript_detail: str = HINDSIGHT_TRANSCRIPT_DETAIL,
) -> Dict[str, Any]:
    """Construct a payload satisfying both new React UI and legacy calls.js UI contracts.

    The legacy contract is kept intact (ticket 15 retires that screen): the
    summary block is always present so the legacy detail keeps rendering mode,
    direction, target and start time. Its fields carry None where nothing was
    retained, and the legacy UI renders those as "not retained".
    """
    transcript_text = c_doc.get("transcript") or ""
    has_transcript = bool(transcript_text.strip())

    summary = {
        "call_id": c_doc["call_id"],
        "outcome": c_doc.get("outcome"),
        "mode": c_doc.get("outlet") or c_doc.get("platform") or "",
        "direction": c_doc.get("direction") or "unknown",
        # Raw retained values. The screen decides how to say "not retained"; the
        # API does not put its own wording into a field that holds a number.
        "caller": c_doc.get("caller") or "",
        "target": c_doc.get("target") or "",
        "start_ts": c_doc.get("start_ts"),
        # start_ts alone cannot say whether the store held a clock time; a
        # date-only stamp parses to midnight. The screen renders these instead.
        "when": c_doc.get("when"),
        "when_precision": c_doc.get("when_precision"),
        "duration_s": c_doc.get("duration_s"),
        "num_turns": c_doc.get("num_turns"),
        "num_tool_calls": None,
        "err": c_doc.get("err"),
    }

    transcript_obj = {
        "status": "ok" if has_transcript else "none",
        "transcript_in": transcript_text if has_transcript else None,
        "reply": "",
        "detail": transcript_detail if has_transcript
        else "no transcript retained for this call",
        "ref": c_doc["call_id"],
    }

    return {
        "call": c_doc,
        "summary": summary,
        "turns": [],
        # Hindsight retains one document per call, never per-turn records. An
        # empty list here means "not retained", not "this call had no turns".
        "turns_retained": False,
        "transcript": transcript_obj,
        "unreachable": False,
        "partial": False,
        "error": None,
        "skipped": 0,
        "incomplete": c_doc.get("incomplete"),
    }
