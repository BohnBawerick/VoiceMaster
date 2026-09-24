"""
Talk voice plugin-side coordination: call detection, trust classification, and
the owner-approval relay between Nextcloud Talk and the audio sidecar (the
VoiceMaster Talk voice bridge, ``services/talk-voice-bridge``).

The gateway platform abstraction is message-oriented and has no call primitive, so
audio never touches this module - the sidecar owns that. This module owns:

  * noticing a room start/stop a call (``call_active`` + ``TransitionTracker``),
  * deciding whether we auto-answer it (``eligible`` - same trigger semantics as
    text: 1:1 answered automatically, groups only if allowlisted),
  * classifying the caller as OWNER/GUEST from the room's participant list
    (``classify_call_trust``, built on ``transport.speaker_tag`` so ownership is
    determined by the exact same actorId-based rule as the text path),
  * noticing a human hanging up mid-call (``call_has_other_human``),
  * relaying the sidecar's guest-escalation approval prompts into the owner's
    home Talk room and posting the owner's verdict back (``VoiceCoordinator``).

Pure predicates below have zero imports beyond ``transport``'s pure constants/
helpers -- no gateway, no network -- so they're unit-tested directly (see
``tests/test_voice_calls.py``). ``VoiceCoordinator`` is the async integration
piece: it has no unit test (mirrors ``TalkClient``'s async loops, verified live)
but must import cleanly with no gateway dependency -- httpx is only touched
inside its async methods, on the ``httpx.AsyncClient`` the injected ``TalkClient``
already constructed and authenticated (``client._client``).

Wired into the adapter in a later step, gated by ``TALK_VOICE_SIDECAR_URL`` --
unset means this module is never instantiated and the text path is untouched.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

try:                                   # deployed inside Hermes: plugins.nextcloud_talk.voice_calls
    from .transport import ROOM_TYPE_ONE_TO_ONE, SPREED, speaker_tag
except ImportError:                    # flat/test context: `import voice_calls` with nextcloud_talk/ on sys.path
    from transport import ROOM_TYPE_ONE_TO_ONE, SPREED, speaker_tag

logger = logging.getLogger("nextcloud_talk.voice_calls")

# Tunables (promoted from inline literals). ~20s OCS reads mirror TalkClient's calls.
DEFAULT_POLL_INTERVAL = 3.0     # seconds between room/approval polls
OCS_TIMEOUT = 20.0              # OCS + sidecar control-API requests
CALL_START_TIMEOUT = 45.0      # /call/start blocks through the sidecar's full Playwright
                               #   call-join (goto + up to ~15s for the Join control + click);
                               #   must exceed that worst case, else a slow-but-successful join
                               #   times out here, the token is forgotten, and hangup detection
                               #   is permanently lost to the sidecar's 90s idle backstop.
HEALTH_TIMEOUT = 10.0          # sidecar /health probe - shorter than OCS_TIMEOUT so a wedged
                               #   sidecar can't stall the watch loop for a full OCS timeout
HANGUP_GONE_THRESHOLD = 2      # consecutive "gone" participant reads before we treat a call as
                               #   hung up (a single empty read can be a transient OCS glitch)


# --- Pure call-state predicates (no client, no gateway -- unit-testable) ----

def call_active(room: dict) -> bool:
    """True iff Talk reports a live call in this room (``hasCall`` or a non-zero ``callFlag``)."""
    if room.get("hasCall"):
        return True
    return bool(room.get("callFlag"))


def eligible(room: dict, *, mode: str, allowlist: list) -> bool:
    """Same auto-answer gate as text's trigger_mode, minus per-message @mention (there's no
    message to mention us in -- a ringing call is the trigger). An allowlisted room is always
    eligible regardless of mode (mirrors ``TalkClient``'s room allowlist)."""
    token = room.get("token")
    if allowlist and token in allowlist:
        return True
    if mode == "all":
        return True
    if mode == "oneToOneOnly":
        return room.get("type") == ROOM_TYPE_ONE_TO_ONE
    # "smart" (default): 1:1 calls answered automatically; groups only if allowlisted above.
    return room.get("type") == ROOM_TYPE_ONE_TO_ONE


def classify_call_trust(participants: list, *, our_user: str, owner_set: set) -> tuple:
    """Pick the first other human in the call and classify them OWNER/GUEST.

    Reuses ``speaker_tag`` so call trust and chat trust are governed by the exact same
    actorId-based rule (identity is server-derived, not spoofable via displayName).
    Returns ``("guest", "", "")`` if no other human is found (defensive default -- the
    caller shouldn't be able to hang up mid-classification and grant themselves trust).
    """
    for p in participants:
        actor_type = p.get("actorType")
        if actor_type is not None and actor_type != "users":
            continue                                    # skip bots / anonymous guests
        actor_id = p.get("actorId") or ""
        if not actor_id or actor_id == our_user:
            continue                                     # skip ourselves
        if not p.get("inCall"):
            continue                                     # not actually in the call yet/anymore
        display = p.get("displayName") or actor_id
        _, is_owner = speaker_tag(actor_id, display, owner_set)
        return ("owner" if is_owner else "guest"), actor_id, display
    return "guest", "", ""


def call_has_other_human(participants: list, *, our_user: str) -> bool:
    """True iff someone other than us is still marked ``inCall``."""
    for p in participants:
        actor_id = p.get("actorId") or ""
        if actor_id and actor_id != our_user and p.get("inCall"):
            return True
    return False


def _is_room_gone(exc: "BaseException") -> bool:
    """Does this exception mean the Talk room no longer exists? (s16 c3)

    Deliberately structural rather than string-matching: httpx raises
    ``HTTPStatusError`` carrying a response, and ``_fetch_participants`` calls
    ``raise_for_status()``. 404 (deleted) and 403/401 (we were removed from it, so it is
    gone AS FAR AS WE CAN SEE) are terminal; 5xx and timeouts are transient and must not
    end a live call. The RuntimeError arm keeps the plugin's own test doubles honest
    without loosening this into a substring search over arbitrary messages."""
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    if status is None and isinstance(exc, RuntimeError):
        text = str(exc)
        for code in (404, 403, 401):
            if f"HTTP {code}" == text.strip():
                return True
        return False
    return status in (404, 403, 401)


class TransitionTracker:
    """Diffs successive room-list snapshots into ("start"/"stop", token) events.

    Only rooms that are BOTH ``call_active`` and ``eligible`` count as "active" for
    our purposes -- a call ringing in a group room we don't auto-answer never
    generates a "start". Stateful by design: one instance per watch loop.

    ``forget()``/``reset()`` are the supported ways to drop tracked tokens -- retry and
    reconcile logic must go through them rather than touching the internal set, so the
    "re-emit as start on the next diff" contract stays explicit and encapsulated.
    """

    def __init__(self, mode: str, allowlist: list) -> None:
        self.mode = mode
        self.allowlist = allowlist
        self._active: set = set()

    @property
    def active(self) -> set:
        """A snapshot copy of the currently-tracked active tokens (safe to iterate/mutate)."""
        return set(self._active)

    def forget(self, token) -> None:
        """Drop a single token so the next ``diff`` re-emits it as a fresh ``("start", token)``.

        Used when a start attempt didn't actually connect (sidecar 409/busy or error): the
        room may still be call-active, so forgetting it lets the retry contract re-fire
        ``start`` on the next poll instead of stranding the caller until a health reconcile."""
        self._active.discard(token)

    def adopt(self, token) -> None:
        """Mark a token active WITHOUT emitting a start (s16 c3).

        The inverse of ``forget``: used when the sidecar is already on this call (an
        outbound dial it placed itself), so the room must be tracked as active in order
        for the eventual clear to diff into a ``stop`` - but re-announcing it as a
        ``start`` would just retry a join that is already in progress."""
        self._active.add(token)

    def reset(self) -> None:
        """Clear ALL tracked-active tokens (e.g. after a sidecar self-teardown reconcile).

        Any still-active rooms re-emit as ``start`` on the next diff, not ``stop``."""
        self._active.clear()

    def diff(self, rooms: list) -> list:
        current = {
            room["token"] for room in rooms
            if room.get("token") and call_active(room) and eligible(room, mode=self.mode, allowlist=self.allowlist)
        }
        stops = sorted(self._active - current)
        starts = sorted(current - self._active)
        self._active = current
        # Deterministic order: tear down what's ending before announcing what's starting.
        return [("stop", tok) for tok in stops] + [("start", tok) for tok in starts]


# --- Async integration: plugin <-> sidecar coordination ---------------------

class VoiceCoordinator:
    """Watches Talk for calls, tells the audio sidecar to join/leave them, and relays
    guest-escalation approvals into the owner's home room.

    Reuses the already-authenticated ``TalkClient`` for ALL HTTP -- both OCS calls
    (``client._client`` against ``client.base_url``) and sidecar control-API calls
    (same client, absolute ``sidecar_url``, which overrides ``base_url`` per request) --
    so no second httpx client/lifecycle is created here. Posting replies into Talk goes
    through ``client.send`` (handles chunking + reference-id echo-suppression already).
    """

    def __init__(
        self,
        client,
        sidecar_url: str,
        *,
        owner_set: set,
        home: str,
        trigger_mode: str,
        allowlist: list,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._client = client
        self.sidecar_url = sidecar_url.rstrip("/")
        self.owner_set = owner_set
        self.home = home
        self.trigger_mode = trigger_mode
        self.allowlist = allowlist
        self.poll_interval = poll_interval

        self._tracker = TransitionTracker(mode=trigger_mode, allowlist=allowlist)
        self._watch_task: Optional[asyncio.Task] = None
        self._approval_task: Optional[asyncio.Task] = None
        self._hangup_tasks: dict = {}
        self._seen_present: set = set()          # tokens where a human has been observed in-call
        self._last_approval_id: Optional[str] = None

        self.pending: Optional[dict] = None       # {"approval_id", "token"} once relayed; adapter reads this

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Spawn the watch + approval-relay loops. Idempotent no-op if already started."""
        if self._watch_task is None:
            self._watch_task = asyncio.create_task(self.watch_loop(), name="voice-watch")
        if self._approval_task is None:
            self._approval_task = asyncio.create_task(self.approval_loop(), name="voice-approval")

    async def stop(self) -> None:
        """Cancel every loop we own (watch, approval, per-call hangup watchers) and await them."""
        tasks = [self._watch_task, self._approval_task, *self._hangup_tasks.values()]
        for task in tasks:
            if task:
                task.cancel()
        for task in tasks:
            if task:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._watch_task = None
        self._approval_task = None
        self._hangup_tasks.clear()

    # -- call detection ----------------------------------------------------------

    async def watch_loop(self) -> None:
        while True:
            try:
                await self._poll_rooms()
                await self._reconcile_sidecar_health()
            except asyncio.CancelledError:
                raise
            except Exception as e:                          # noqa: BLE001
                logger.warning("voice watch_loop error: %s", e)
            await asyncio.sleep(self.poll_interval)

    async def _poll_rooms(self) -> None:
        r = await self._client._client.get(f"{SPREED}/v4/room", timeout=OCS_TIMEOUT)
        r.raise_for_status()
        rooms = r.json().get("ocs", {}).get("data", [])
        for action, token in self._tracker.diff(rooms):
            if action == "start":
                await self._start_call(token)
            else:
                await self._stop_call(token)

    async def _fetch_participants(self, token: str) -> list:
        r = await self._client._client.get(f"{SPREED}/v4/room/{token}/participants", timeout=OCS_TIMEOUT)
        r.raise_for_status()
        return r.json().get("ocs", {}).get("data", [])

    async def _start_call(self, token: str) -> None:
        try:
            participants = await self._fetch_participants(token)
        except Exception as e:                              # noqa: BLE001
            logger.warning("participants fetch failed for %s: %s", token, e)
            participants = []

        trust, caller, caller_display = classify_call_trust(
            participants, our_user=self._client.our_actor_id, owner_set=self.owner_set,
        )
        self._seen_present.discard(token)

        try:
            r = await self._client._client.post(
                f"{self.sidecar_url}/call/start",
                json={"token": token, "trust": trust, "caller": caller, "caller_display": caller_display},
                timeout=CALL_START_TIMEOUT,
            )
        except Exception as e:                              # noqa: BLE001
            logger.warning("call/start POST failed for %s: %s", token, e)
            # Never connected - drop it so the next poll retries it as a fresh "start".
            self._tracker.forget(token)
            return

        if r.status_code == 409:
            # s16 c3: a 409 means "busy" - but busy with WHOSE call? Two very different
            # states share one status code, and treating them alike wedged every outbound
            # call in s14b (see tests/test_s16_outbound_wedge.py).
            #
            #   busy with SOMEONE ELSE's call -> transient. Retry contract: forget the
            #       token so the next diff re-emits it as "start", and a second caller
            #       connects the moment the first ends.
            #   busy with THIS room           -> the STEADY STATE of an outbound call the
            #       bridge placed itself. Forgetting it here was the defect: the token
            #       left `_active`, so `stops = _active - current` could never contain it,
            #       so the cleared room emitted no ("stop", token), so /call/stop was never
            #       posted and the slot stayed busy until a human intervened.
            if await self._sidecar_is_on(token):
                logger.info("call/start for %s declined -- sidecar is already on THIS call "
                            "(outbound); adopting it for hangup detection", token)
                self._adopt(token)
                return
            logger.info("call/start for %s declined -- sidecar busy with another call", token)
            self._tracker.forget(token)
            return
        if r.status_code != 200:
            logger.warning("call/start for %s returned %s: %s", token, r.status_code, r.text[:200])
            self._tracker.forget(token)                     # didn't join - allow a retry next poll
            return

        logger.info("call %s joined (trust=%s, caller=%s)", token, trust, caller)
        self._hangup_tasks[token] = asyncio.create_task(
            self.hangup_loop(token), name=f"voice-hangup-{token}"
        )

    async def _sidecar_is_on(self, token: str) -> bool:
        """Is the sidecar's ACTIVE call this very room? (s16 c3)

        Distinguishes the two meanings of a 409 from `/call/start`. Fails CLOSED: if the
        probe errors we return False and keep the old forget-and-retry contract, because
        wrongly adopting a room the sidecar is not on would suppress a legitimate retry.
        """
        try:
            r = await self._client._client.get(
                f"{self.sidecar_url}/health", timeout=HEALTH_TIMEOUT)
            if r.status_code != 200:
                return False
            return (r.json() or {}).get("active_token") == token
        except Exception as e:                              # noqa: BLE001
            logger.warning("sidecar /health probe failed for %s: %s", token, e)
            return False

    def _adopt(self, token: str) -> None:
        """Track a call the sidecar placed itself (outbound) as if we had joined it.

        Two things the 409 path previously skipped, both required to end the call:
        keeping the token in the tracker (so the cleared room emits a ``stop``) and arming
        the hangup watcher (so a human leaving is noticed before the room even clears).
        Idempotent - the 409 recurs on every poll for the life of the call."""
        self._tracker.adopt(token)
        if token not in self._hangup_tasks:
            self._hangup_tasks[token] = asyncio.create_task(
                self.hangup_loop(token), name=f"voice-hangup-{token}")

    async def _stop_call(self, token: str) -> None:
        task = self._hangup_tasks.pop(token, None)
        if task:
            task.cancel()
        await self._post_call_stop(token)

    async def _post_call_stop(self, token: str) -> None:
        try:
            await self._client._client.post(
                f"{self.sidecar_url}/call/stop", json={"token": token}, timeout=OCS_TIMEOUT
            )
        except Exception as e:                              # noqa: BLE001
            logger.warning("call/stop POST failed for %s: %s", token, e)

    # -- hangup detection ----------------------------------------------------------

    async def hangup_loop(self, token: str) -> None:
        """While ``token``'s call is active: once a human has been seen in-call and then is
        no longer, tell the sidecar to leave (the human hung up).

        Debounce: a single "gone" reading can be a transient OCS glitch mid-call, so require
        ``HANGUP_GONE_THRESHOLD`` consecutive "gone" observations before tearing down; any
        "present" read (or the call actually ending) resets the streak."""
        gone_streak = 0
        while True:
            try:
                await asyncio.sleep(self.poll_interval)
                participants = await self._fetch_participants(token)
                present = call_has_other_human(participants, our_user=self._client.our_actor_id)
                if present:
                    self._seen_present.add(token)
                    gone_streak = 0
                elif token in self._seen_present:
                    gone_streak += 1
                    if gone_streak >= HANGUP_GONE_THRESHOLD:
                        logger.info("human left call %s (%d consecutive gone) -- stopping",
                                    token, gone_streak)
                        self._seen_present.discard(token)
                        await self._post_call_stop(token)
                        self._hangup_tasks.pop(token, None)
                        return
            except asyncio.CancelledError:
                raise
            except Exception as e:                          # noqa: BLE001
                # s16 c3: a DELETED room is not an error to log and shrug at - it is the
                # strongest possible "the call is over" signal, and swallowing it here let
                # the loop spin forever against a room that no longer exists. Only the
                # gone-shaped failures count toward the streak; a transient 5xx/timeout
                # must NOT tear down a healthy call, so everything else keeps the old
                # log-and-continue behaviour.
                if _is_room_gone(e) and token in self._seen_present:
                    gone_streak += 1
                    logger.info("room %s is gone (%s) -- counting toward hangup (%d/%d)",
                                token, type(e).__name__, gone_streak, HANGUP_GONE_THRESHOLD)
                    if gone_streak >= HANGUP_GONE_THRESHOLD:
                        self._seen_present.discard(token)
                        await self._post_call_stop(token)
                        self._hangup_tasks.pop(token, None)
                        return
                else:
                    logger.warning("hangup_loop error for %s: %s", token, e)

    # -- approval relay ----------------------------------------------------------

    async def approval_loop(self) -> None:
        while True:
            try:
                await self._poll_approval()
            except asyncio.CancelledError:
                raise
            except Exception as e:                          # noqa: BLE001
                logger.warning("voice approval_loop error: %s", e)
            await asyncio.sleep(self.poll_interval)

    async def _poll_approval(self) -> None:
        r = await self._client._client.get(f"{self.sidecar_url}/voice/pending-approval", timeout=OCS_TIMEOUT)
        r.raise_for_status()
        pending = r.json().get("pending")

        if not pending:
            if self.pending is not None:
                # The sidecar resolved/timed the approval out on its own (e.g. the
                # approval_timeout backstop) -- stop the adapter from intercepting
                # approve/deny replies for a request that's no longer live.
                self.pending = None
            return

        approval_id = pending.get("approval_id")
        if not approval_id or approval_id == self._last_approval_id:
            return                                          # already relayed this one

        # Record the pending slot FIRST - an owner reply can then resolve it even if the
        # relay send below fails - and only advance _last_approval_id AFTER a successful
        # send. If client.send raises (transport.send raises on a transient Talk 5xx /
        # network error), we leave _last_approval_id unchanged so the NEXT poll retries the
        # relay, instead of dedup-swallowing the prompt and silently denying the guest at
        # the sidecar's approval timeout.
        self.pending = {"approval_id": approval_id, "token": pending.get("token")}

        if not self.home:
            # No home room configured - we can never relay this; advance the marker so we
            # don't re-warn every poll (home is fixed for the process lifetime).
            logger.warning("approval %s pending but no home room configured -- cannot relay", approval_id)
            self._last_approval_id = approval_id
            return

        caller = pending.get("caller") or "someone"
        summary = pending.get("summary") or "an action"
        try:
            await self._client.send(
                self.home,
                f"\U0001F514 Guest {caller} on a voice call asks: {summary}\nReply 'approve' or 'deny'.",
            )
        except Exception as e:                              # noqa: BLE001
            logger.warning("failed to relay approval prompt to home room (will retry next poll): %s", e)
            return                                          # leave _last_approval_id unchanged -> retry
        self._last_approval_id = approval_id                # relayed OK - don't re-relay this id

    async def resolve_approval(self, decision: str) -> None:
        """Post the owner's verdict back to the sidecar and clear ``self.pending``.

        Called by the adapter's message pre-filter when an owner reply in the home
        room matches an approve/deny pattern while an approval is outstanding.
        """
        if not self.pending:
            return
        approval_id = self.pending["approval_id"]
        try:
            await self._client._client.post(
                f"{self.sidecar_url}/voice/approval-verdict",
                json={"approval_id": approval_id, "decision": decision},
                timeout=OCS_TIMEOUT,
            )
        except Exception as e:                              # noqa: BLE001
            logger.warning("approval-verdict POST failed: %s", e)
        finally:
            self.pending = None

    # -- sidecar self-teardown reconciliation ----------------------------------

    async def _reconcile_sidecar_health(self) -> None:
        """If the sidecar tore a call down on its own (idle timeout, bridge error) our
        tracker can be left thinking a call is still active, permanently blocking new
        calls from being detected as "start" transitions. Poll /health and, if it
        reports idle while we still track an active token, clear our side too."""
        try:
            r = await self._client._client.get(f"{self.sidecar_url}/health", timeout=HEALTH_TIMEOUT)
            r.raise_for_status()
            health = r.json()
        except Exception as e:                              # noqa: BLE001
            logger.warning("sidecar health check failed: %s", e)
            return

        active = self._tracker.active
        if health.get("busy") or not active:
            return

        for token in active:
            task = self._hangup_tasks.pop(token, None)
            if task:
                task.cancel()
        self._tracker.reset()
        self._seen_present.difference_update(active)
        logger.info("reconciled stale active call(s) %s -- sidecar reports idle", sorted(active))
