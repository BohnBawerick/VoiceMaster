"""Schedules: a Call that has not happened yet (ticket 11, VC14).

A Schedule is a time, an Agent, a target and a Mission. When its time comes the
phone rings, through the SAME placement the button uses (``place_call``), and
afterwards the Call is an ordinary Call on the Calls screen. This module is the
memory: the records, the time arithmetic and the one atomic primitive that makes
firing happen exactly once. The loop that consults it is ``scheduler.py``.

## The claim, and why everything goes through it

One file per Schedule under ``$VOICE_CONFIG_DIR/schedules/``, plus — at most once
per Schedule, ever — a sibling ``<id>.claim`` created with ``O_CREAT|O_EXCL``.
That create is the whole concurrency design:

    **Whoever creates the claim file owns this Schedule's terminal state.**

``O_CREAT|O_EXCL`` is atomic on the config volume (a local bind mount), so of any
number of workers, restarts or overlapping ticks, exactly one wins. Three
different deciders compete for the same claim:

  * **firing** — wins, dials, and writes ``placed`` or ``failed``;
  * **cancelling** — wins, and writes ``cancelled``; a Schedule cancelled this
    way can never be claimed to fire, so it cannot ring after being cancelled;
  * **missing it** — wins for a Schedule that came due while nothing was
    running and is now past the grace window, and writes ``failed``.

That is what settles the cancel-versus-due race honestly: the two callers race
for one file, the loser is told it lost, and the record says what actually
happened rather than what was asked for. A claim is never removed, so a Schedule
is claimable once in its life. Firing is therefore **at most once** by
construction, and exactly once whenever the process survives its own dial.

The one hard case the filesystem cannot decide: a claim written, then the process
dies before it can record the outcome. Nobody can know from here whether Twilio
got the dial, so ``interrupted_claims`` reports those (after
``VOICE_SCHEDULE_STALE_CLAIM_S``, longer than the dial timeout, so a claim held
by a live worker is never stolen) and the scheduler settles them ``failed``
saying exactly that. It does **not** re-dial: one attempt only (VC14).

## Time

``due_at`` is stored as an instant (UTC), and that instant alone decides when the
phone rings. The zone and the wall-clock time the owner named are stored beside
it as provenance and for display — never re-derived at fire time, because a
naive local time is not a promise a scheduler can keep: it means different
instants before and after a daylight-saving change, and one of those instants may
not exist at all. Resolution happens once, at creation, where a local time that
cannot be honoured can still be refused to someone's face:

  * a local time that **does not exist** (the spring-forward gap) is REFUSED —
    there is no instant to promise, and silently sliding the call an hour is a
    worse answer than saying so;
  * a local time that happens **twice** (the autumn-back overlap) resolves to the
    FIRST of the two and says so in the record — both are that wall clock, and
    the earlier one is what "3pm" means to the person who typed it. A caller who
    means the other one sends an explicit UTC offset instead.
"""
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from voicecore import profiles

DIRNAME = "schedules"

STATUS_PENDING = "pending"
STATUS_PLACED = "placed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
TERMINAL_STATUSES = frozenset({STATUS_PLACED, STATUS_FAILED, STATUS_CANCELLED})

# Claim intents, recorded in the claim file for diagnosis only. Nothing reads
# them back to make a decision: the claim's existence IS the decision.
INTENT_FIRE = "fire"
INTENT_CANCEL = "cancel"
INTENT_MISS = "miss"

_ID_RE = re.compile(r"^sch-[0-9a-f]{12}$")

# How late a Call may still be placed. A container restart is seconds; a NAS
# reboot is minutes; a Schedule an hour stale is not a call anyone wants placed
# unannounced, so past this it is a failure with an honest reason, never a
# surprise call.
DEFAULT_GRACE_S = 300.0

# A claim with no outcome is only assumed dead after this. It must stay well
# clear of the dial timeout (30s in place_call) so a claim held by a live worker
# is never taken away from it.
DEFAULT_STALE_CLAIM_S = 120.0


class ScheduleError(Exception):
    """A Schedule cannot be written or resolved, with the reason to show."""

    def __init__(self, status: int, detail: "list[str]"):
        self.status = status
        self.detail = list(detail)
        super().__init__("; ".join(self.detail))


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def directory(env=None) -> Path:
    return profiles.config_dir(env) / DIRNAME


def record_path(schedule_id: str, env=None) -> Path:
    return directory(env) / f"{schedule_id}.yaml"


def claim_path(schedule_id: str, env=None) -> Path:
    return directory(env) / f"{schedule_id}.claim"


def is_schedule_id(value) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def new_id() -> str:
    return f"sch-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """UTC, second resolution, ``Z`` — one spelling of an instant in the file."""
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def parse_iso(text: str) -> datetime:
    """Read an instant back. Raises ScheduleError rather than ValueError."""
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ScheduleError(422, [f"at: {text!r} is not an ISO-8601 date-time"]) from exc
    if parsed.tzinfo is None:
        raise ScheduleError(422, [f"at: {text!r} has no timezone"])
    return parsed.astimezone(timezone.utc)


def default_timezone(env=None) -> "str | None":
    """The zone a naive local time is read in when the caller names none.

    ``VOICE_TIMEZONE`` first, then the container's ``TZ`` (the deployment's compose file sets it).
    With neither, a naive time is refused rather than guessed — silently
    reading "3pm" as UTC would ring at 11pm in Perth.
    """
    env = os.environ if env is None else env
    for name in ("VOICE_TIMEZONE", "TZ"):
        raw = (env.get(name) or "").strip()
        if raw:
            return raw
    return None


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(422, [
            f"tz: {name!r} is not a known IANA time zone"]) from exc
    except Exception as exc:  # noqa: BLE001 — a broken tz database, not bad input
        raise ScheduleError(500, [
            f"tz: {name!r} could not be resolved: {exc}"]) from exc


def _duration_text(delta: timedelta) -> str:
    minutes = int(delta.total_seconds()) // 60
    return f"{minutes // 60}:{minutes % 60:02d}"


def _offset_text(moment: datetime) -> str:
    offset = moment.utcoffset() or timedelta(0)
    total = int(offset.total_seconds())
    sign = "-" if total < 0 else "+"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def resolve_due(at, tz=None, env=None) -> dict:
    """Turn what the caller asked for into an instant, once and for all.

    ``at`` is either an ISO-8601 date-time carrying an offset (already an
    instant) or a naive local one that ``tz`` — or the server default — reads.
    Returns the fields a record stores: ``due_at`` (the instant, UTC),
    ``timezone``, ``local_time`` (the wall clock as named) and ``utc_offset``,
    plus ``ambiguous_local_time: True`` when that wall clock happened twice.
    """
    if not isinstance(at, str) or not at.strip():
        raise ScheduleError(422, ["at: required — when this Call should be placed"])
    text = at.strip()

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScheduleError(422, [
            f"at: {at!r} is not an ISO-8601 date-time "
            f"(for example 2026-08-20T15:00 or 2026-08-20T15:00:00+08:00)"]) from exc

    if tz is not None and (not isinstance(tz, str) or not tz.strip()):
        raise ScheduleError(422, ["tz: must be an IANA time zone name"])
    zone_name = (tz or "").strip() or None

    if parsed.tzinfo is not None:
        # An instant was given. A zone alongside it is display provenance, and
        # has to agree with the offset or one of the two is a lie.
        instant = parsed.astimezone(timezone.utc)
        if zone_name is None:
            local = parsed
            zone_label = _offset_text(parsed)
        else:
            zone = _zone(zone_name)
            local = instant.astimezone(zone)
            if local.utcoffset() != parsed.utcoffset():
                raise ScheduleError(422, [
                    f"at: the offset {_offset_text(parsed)} is not {zone_name}'s "
                    f"offset ({_offset_text(local)}) at that moment — send one or "
                    f"the other, not both"])
            zone_label = zone_name
        return {
            "due_at": to_iso(instant),
            "timezone": zone_label,
            "local_time": local.replace(tzinfo=None, microsecond=0).isoformat(),
            "utc_offset": _offset_text(local),
        }

    if zone_name is None:
        zone_name = default_timezone(env)
    if not zone_name:
        raise ScheduleError(422, [
            "tz: required — 'at' has no UTC offset, so the zone its local time "
            "means has to be said (this service has no default zone configured)"])
    zone = _zone(zone_name)

    naive = parsed.replace(microsecond=0)
    first = naive.replace(tzinfo=zone)
    second = naive.replace(tzinfo=zone, fold=1)

    # A local time inside the spring-forward gap never happens: converting it
    # to an instant and back lands on a DIFFERENT wall clock.
    round_trip = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    if round_trip != naive:
        shift = round_trip - naive
        raise ScheduleError(422, [
            f"at: {naive.isoformat()} does not exist in {zone_name} — daylight "
            f"saving moves the clocks forward {_duration_text(shift)} that day and "
            f"that local time is inside the skipped hour (it would land at "
            f"{round_trip.strftime('%H:%M')}). Pick a time either side of the "
            f"change."])

    resolved = {
        "due_at": to_iso(first),
        "timezone": zone_name,
        "local_time": naive.isoformat(),
        "utc_offset": _offset_text(first),
    }
    if first.utcoffset() != second.utcoffset():
        # It happens twice. The earlier one is what the wall clock means the
        # first time it reads that; the record says so out loud.
        resolved["ambiguous_local_time"] = True
    return resolved


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, doc: dict) -> None:
    """tmp + fsync + os.replace. A Schedule that vanished in a power cut is a
    promise silently dropped, so the bytes are on the platter before the rename
    and the rename itself is durable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read(path: Path) -> "dict | None":
    try:
        doc = yaml.safe_load(path.read_text())
    except Exception:  # noqa: BLE001 — an unreadable record is reported, not raised
        return None
    return doc if isinstance(doc, dict) else None


def create(request, due: dict, env=None, label=None) -> dict:
    """Write one pending Schedule. ``request`` is a validated PlaceRequest."""
    record = {
        "id": new_id(),
        "status": STATUS_PENDING,
        "created_at": to_iso(now_utc()),
        "agent": request.agent,
        "to": request.to,
        "mission": request.mission,
        "disclose": bool(request.disclose),
    }
    if request.target_display:
        record["target_display"] = request.target_display
    if isinstance(label, str) and label.strip():
        record["label"] = label.strip()
    record.update(due)
    _atomic_write(record_path(record["id"], env), record)
    return record


def load(schedule_id: str, env=None) -> "dict | None":
    if not is_schedule_id(schedule_id):
        return None
    path = record_path(schedule_id, env)
    if not path.is_file():
        return None
    record = _read(path)
    if record is None or record.get("id") != schedule_id:
        return None
    return record


def load_all(env=None) -> list:
    """Every readable Schedule, soonest first, settled ones after pending ones.

    A record that will not parse is skipped rather than raising: one bad file
    must not take the screen down, and the file is still on disk to look at.
    """
    base = directory(env)
    if not base.is_dir():
        return []
    records = []
    for path in sorted(base.glob("sch-*.yaml")):
        record = _read(path)
        if record is None or not is_schedule_id(record.get("id")):
            continue
        if path.name != f"{record['id']}.yaml":
            continue
        records.append(record)
    upcoming = sorted((r for r in records if r.get("status") == STATUS_PENDING),
                      key=lambda r: r.get("due_at") or "")
    settled = sorted((r for r in records if r.get("status") != STATUS_PENDING),
                     key=lambda r: r.get("settled_at") or r.get("due_at") or "",
                     reverse=True)
    return upcoming + settled


def settle(schedule_id: str, status: str, env=None, **fields) -> "dict | None":
    """Write a terminal state. FIRST writer wins: an already-settled Schedule is
    left exactly as it was, so a late sweep can never overwrite the truth a fire
    recorded (or the other way round)."""
    record = load(schedule_id, env)
    if record is None:
        return None
    if record.get("status") in TERMINAL_STATUSES:
        return record
    record["status"] = status
    record["settled_at"] = to_iso(now_utc())
    for key, value in fields.items():
        if value is None:
            continue
        record[key] = value
    _atomic_write(record_path(schedule_id, env), record)
    return record


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------

def claim(schedule_id: str, intent: str, env=None) -> bool:
    """Take sole responsibility for this Schedule's ending. True if we got it.

    ``O_CREAT|O_EXCL`` — the kernel picks the winner. Everything about
    exactly-once firing, and about a cancel that races the due time, rests on
    this one call, so it is deliberately the only place in the app that decides
    who acts on a Schedule.
    """
    path = claim_path(schedule_id, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (f"intent: {intent}\npid: {os.getpid()}\n"
            f"claimed_at: {to_iso(now_utc())}\n")
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        os.write(fd, body.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def is_claimed(schedule_id: str, env=None) -> bool:
    return claim_path(schedule_id, env).exists()


def claim_age_s(schedule_id: str, env=None) -> "float | None":
    """Seconds since the claim was taken, or None if there is no claim."""
    path = claim_path(schedule_id, env)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return max(0.0, now_utc().timestamp() - stat.st_mtime)


# ---------------------------------------------------------------------------
# What the loop asks
# ---------------------------------------------------------------------------

def grace_s(env=None) -> float:
    return positive_float_env("VOICE_SCHEDULE_GRACE_S", DEFAULT_GRACE_S, env)


def stale_claim_s(env=None) -> float:
    return positive_float_env("VOICE_SCHEDULE_STALE_CLAIM_S",
                              DEFAULT_STALE_CLAIM_S, env)


def positive_float_env(name: str, default: float, env=None) -> float:
    """A duration knob, or a refusal that names the variable and the value.

    Unset is the default. Anything else must be a number greater than zero:
    a zero or negative window is not a configuration, it is a system that
    looks configured and cannot work (a grace of zero fails every Schedule
    whose due instant is not hit to the microsecond), and a typo silently
    falling back to the default is the same lie one step quieter. Both refuse.

    This is the ONLY parser for these knobs, so what the startup check
    validates and what the loop later reads cannot disagree.
    """
    env = os.environ if env is None else env
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ScheduleError(500, [
            f"{name}: {raw!r} is not a number — set it to a number of seconds "
            f"greater than zero, or unset it for the default ({default:g}s)"]) from None
    if value <= 0:
        raise ScheduleError(500, [
            f"{name}: {raw!r} — this is a window of time and it must be greater "
            f"than zero. Zero would fail every Schedule instead of placing it; "
            f"unset the variable for the default ({default:g}s), or switch the "
            f"scheduler off with VOICE_SCHEDULER_ENABLED=false if that is what "
            f"was meant."])
    return value


def pending(records) -> list:
    return [r for r in records if r.get("status") == STATUS_PENDING]


def due_now(records, now: datetime, env=None) -> list:
    """Pending Schedules whose time has come and that can still be placed."""
    grace = timedelta(seconds=grace_s(env))
    out = []
    for record in pending(records):
        due = due_at(record)
        if due is not None and due <= now <= due + grace:
            out.append(record)
    return out


def missed(records, now: datetime, env=None) -> list:
    """Pending Schedules whose time came and went while nothing was running."""
    grace = timedelta(seconds=grace_s(env))
    out = []
    for record in pending(records):
        due = due_at(record)
        if due is not None and now > due + grace:
            out.append(record)
    return out


def unresolvable(records) -> list:
    """Pending Schedules whose ``due_at`` cannot be read — a corrupted record.

    Reported, not ignored: a Schedule nobody can date will never come due, and
    a screen that lists it as "upcoming" forever is a lie.
    """
    return [r for r in pending(records) if due_at(r) is None]


def interrupted_claims(records, env=None) -> list:
    """Pending Schedules holding a claim old enough that its owner is gone."""
    limit = stale_claim_s(env)
    out = []
    for record in pending(records):
        age = claim_age_s(record["id"], env)
        if age is not None and age >= limit:
            out.append(record)
    return out


def next_due_at(records) -> "datetime | None":
    times = [t for t in (due_at(r) for r in pending(records)) if t is not None]
    return min(times) if times else None


def due_at(record) -> "datetime | None":
    raw = record.get("due_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)
