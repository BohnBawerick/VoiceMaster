"""The loop that rings the phone at the appointed time (ticket 11, VC14).

Everything durable lives in ``schedules.py``; this is only the part that wakes
up. It holds NO state about which Schedules it has fired — that state is on the
disk, in the claim, which is why a restart is not an event this module has to
handle specially. There is exactly one pass, ``tick``, and startup is just the
first one:

  * a Schedule due while the process was down, one due during startup and one
    due mid-run all arrive at the same ``tick`` through the same door;
  * two workers ticking at the same moment both call ``schedules.claim`` and
    exactly one of them dials, because the kernel says so;
  * a Schedule already fired carries a claim forever, so no later tick, restart
    or second process can fire it again.

**One attempt only** (VC14). Nothing here re-dials: a dial that was refused, a
bridge that could not be reached, a Call nobody answered and a Schedule that
came due while the app was down all end as ``failed`` with the reason written
down, and are never picked up again. Retry and escalation are deliberately out
of scope (O2) — the honest failures are what a retry policy should be designed
against later.

**The Call travels the manual path.** ``tick`` builds the same body the Place-a-
call screen POSTs and hands it to ``place_call.place_from_request`` — the same
validation, the same 409 for an Agent that cannot run, the same one-shot dial
that binds the Agent snapshot to this call_id and leaves the Outlet's
last-known-good alone. There is no scheduled-call code path to rot.
"""
import asyncio
import logging
import os

import place_call
import schedules

logger = logging.getLogger("voice.scheduler")

ENABLED_ENV = "VOICE_SCHEDULER_ENABLED"
TICK_ENV = "VOICE_SCHEDULE_TICK_S"

# The longest the loop sleeps when nothing is due soon. It is not the firing
# resolution: the loop sleeps until the next due instant when that is sooner,
# and a newly created Schedule wakes it immediately.
DEFAULT_MAX_SLEEP_S = 15.0
MIN_SLEEP_S = 0.05

# What place_call gives a dial before it gives up (httpx timeout). Named here
# because the stale-claim window has to stay clear of it.
DIAL_TIMEOUT_S = 30.0


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get(ENABLED_ENV) or "true").strip().lower() not in (
        "0", "false", "no", "off")


def max_sleep_s(env=None) -> float:
    return schedules.positive_float_env(TICK_ENV, DEFAULT_MAX_SLEEP_S, env)


def validate_knobs(env=None) -> None:
    """Read every scheduler knob once, so a bad one is a refusal at startup.

    A duration set to zero, to a negative, or to something that is not a number
    used to fall back to the default without a word: the operator gets a system
    that reads as configured and does not do what the variable says. Reading
    them here, through the SAME accessors the loop uses, turns that into a
    container that will not start and a log line naming the variable.

    It runs even when the scheduler is disabled: a knob that will be honoured
    the moment somebody flips VOICE_SCHEDULER_ENABLED back on has to be right
    before then, not after the first Schedule is missed.
    """
    grace = schedules.grace_s(env)
    stale = schedules.stale_claim_s(env)
    max_sleep_s(env)
    if stale <= DIAL_TIMEOUT_S:
        # Not a refusal: it is a legitimate, if brave, choice. But a stale
        # window inside the dial timeout can take a claim off a worker that is
        # still on the phone, and the Schedule then records a failure for a
        # call that was placed.
        logger.warning(
            "VOICE_SCHEDULE_STALE_CLAIM_S is %.0fs, at or below the %.0fs dial "
            "timeout — a claim can be declared dead while its dial is still in "
            "flight, and that Schedule would then be recorded failed even though "
            "the call went out", stale, DIAL_TIMEOUT_S)
    if grace < max_sleep_s(env):
        logger.warning(
            "VOICE_SCHEDULE_GRACE_S is %.0fs, shorter than the %.0fs the loop "
            "may sleep for — a Schedule can fall past its grace window between "
            "two passes and be recorded missed without being placed",
            grace, max_sleep_s(env))


class Scheduler:
    """One pass at a time over the Schedules on disk.

    ``transport_get`` is how the injected httpx transport reaches the dial: the
    app hands over ``lambda: app.state.transport`` so the scheduler places calls
    through exactly what the HTTP route places them through (a mock in tests,
    the real network in production).
    """

    def __init__(self, *, transport_get=None, env=None):
        self._transport_get = transport_get or (lambda: None)
        self._env = env
        self._task = None
        self._wake = asyncio.Event()
        self._stopping = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="voice-scheduler")

    async def stop(self) -> None:
        """Stop ticking. A dial already in flight is awaited, never abandoned
        half-placed: cancelling mid-dial is precisely how a claim ends up with
        no outcome."""
        self._stopping = True
        task, self._task = self._task, None
        if task is None:
            return
        self._wake.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=60.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def nudge(self) -> None:
        """A Schedule was created or cancelled — look again now."""
        self._wake.set()

    # -- the loop ----------------------------------------------------------

    async def _run(self) -> None:
        logger.info("Scheduler running — schedules in %s",
                    schedules.directory(self._env))
        while not self._stopping:
            # Cleared BEFORE the pass, not after: a Schedule created while a
            # tick is running must still wake the next wait, or it waits out
            # the whole ceiling for no reason.
            self._wake.clear()
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — a bad tick must not end the loop
                logger.exception("Scheduler tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._sleep_s())
            except asyncio.TimeoutError:
                pass

    def _sleep_s(self) -> float:
        ceiling = max_sleep_s(self._env)
        try:
            records = schedules.load_all(self._env)
        except Exception:  # noqa: BLE001
            return ceiling
        # Claimed Schedules are somebody else's to settle (or are waiting out
        # the stale-claim window), so they are not something to wake up for —
        # counting them would spin this loop at its floor until they expire.
        soonest = schedules.next_due_at(
            [r for r in schedules.pending(records)
             if not schedules.is_claimed(r["id"], self._env)])
        if soonest is None:
            return ceiling
        remaining = (soonest - schedules.now_utc()).total_seconds()
        return max(MIN_SLEEP_S, min(ceiling, remaining))

    async def tick(self) -> list:
        """One pass. Returns what it settled, for tests and for the log."""
        records = schedules.load_all(self._env)
        now = schedules.now_utc()
        settled = []

        for record in schedules.due_now(records, now, self._env):
            outcome = await self._fire(record)
            if outcome is not None:
                settled.append(outcome)

        for record in schedules.missed(records, now, self._env):
            outcome = self._settle_missed(record, now)
            if outcome is not None:
                settled.append(outcome)

        for record in schedules.unresolvable(records):
            outcome = self._settle(record, schedules.STATUS_FAILED, reason=(
                "this Schedule has no readable time on it, so it can never come "
                "due — it was not placed"), intent=schedules.INTENT_MISS)
            if outcome is not None:
                settled.append(outcome)

        for record in schedules.interrupted_claims(records, self._env):
            # The claim is already taken (by the process that died holding it),
            # so this is the one settlement that does not compete for it.
            outcome = schedules.settle(
                record["id"], schedules.STATUS_FAILED, env=self._env,
                reason=("the app stopped while this Call was being placed — "
                        "whether the phone rang is not known here, and it was "
                        "not tried again (one attempt only)"))
            if outcome is not None and outcome.get("status") == schedules.STATUS_FAILED:
                logger.warning("Schedule %s: claim with no outcome, settled failed",
                               record["id"])
                settled.append(outcome)

        return settled

    # -- the three endings -------------------------------------------------

    async def _fire(self, record) -> "dict | None":
        schedule_id = record["id"]
        if not schedules.claim(schedule_id, schedules.INTENT_FIRE, self._env):
            return None  # cancelled, already fired, or another worker has it
        body = place_body(record)
        try:
            placed = await place_call.place_from_request(
                body, transport=self._transport_get())
        except place_call.PlaceRejected as exc:
            logger.warning("Schedule %s failed to place: %s", schedule_id, exc.reason)
            return schedules.settle(schedule_id, schedules.STATUS_FAILED,
                                    env=self._env, reason=exc.reason,
                                    failure_status=exc.status)
        except Exception as exc:  # noqa: BLE001 — never leave a claim unanswered
            logger.exception("Schedule %s failed to place", schedule_id)
            return schedules.settle(
                schedule_id, schedules.STATUS_FAILED, env=self._env,
                reason=f"unexpected error while placing this Call: "
                       f"{type(exc).__name__}: {exc}")
        logger.info("Schedule %s placed a call to %s as %s (call_id=%s)",
                    schedule_id, record.get("to"), record.get("agent"),
                    placed.get("call_id"))
        return schedules.settle(schedule_id, schedules.STATUS_PLACED,
                                env=self._env,
                                call_id=placed.get("call_id"),
                                call_sid=placed.get("call_sid"))

    def _settle_missed(self, record, now) -> "dict | None":
        late = int((now - schedules.due_at(record)).total_seconds())
        return self._settle(record, schedules.STATUS_FAILED, intent=schedules.INTENT_MISS,
                            reason=(f"nothing was running when this Call came due and it "
                                    f"is now {late}s late, past the "
                                    f"{int(schedules.grace_s(self._env))}s grace window — "
                                    f"it was not placed, and it was not tried again "
                                    f"(one attempt only)"))

    def _settle(self, record, status, *, intent, **fields) -> "dict | None":
        if not schedules.claim(record["id"], intent, self._env):
            return None
        logger.warning("Schedule %s settled %s: %s", record["id"], status,
                       fields.get("reason"))
        return schedules.settle(record["id"], status, env=self._env, **fields)


def place_body(record) -> dict:
    """The Schedule, as the body a person would have POSTed to place it now.

    This is the whole of the scheduled path's knowledge about placing a call:
    it turns a stored Schedule back into a Place-a-call request and hands it to
    the one placement. Anything else the manual path does is done by the
    manual path's own code, on this body.
    """
    return {
        "agent": record.get("agent"),
        "to": record.get("to"),
        "mission": record.get("mission"),
        "disclose": bool(record.get("disclose")),
        "target_display": record.get("target_display") or "",
    }
