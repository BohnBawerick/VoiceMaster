"""Mode V — CallSession orchestrator.

Owns the single-call lock and the one active RealtimeBridge for the sidecar. There is
at most one live call at a time (a Nextcloud Talk 1:1/group room the ai-agent user has
joined); this module is the sole place that enforces that invariant.

No OCS/participant POLLING lives here — the plugin (running inside the Hermes gateway)
owns human-hangup detection and tells us to tear down via ``stop()`` (wired to the
sidecar's ``POST /call/stop``). This module only reacts to the bridge ending on its own
(idle watchdog, WS close, parec EOF, error) and to an explicit stop request, and makes
sure the single-call lock and the browser's call membership are always released
afterwards — regardless of which of those triggered teardown first.

Teardown does make TWO one-shot OCS calls (s15d): after leaving, it asks Nextcloud
whether the call actually ended and, if not, ends it. That is verification, not polling —
clicking hang-up is best-effort and s14b shipped a state where it silently failed, leaving
a call that blocked every later fire and got rejoined on the next boot.
"""
import asyncio
import functools
import logging
import os
import time
from typing import Optional

import cascade_bridge
import config
import hermes
import outbound
from voicecore import lkg
from voicecore import profiles
from approval import ApprovalStore
from browser import TalkBrowser
from config import Config
from outbound import OutboundMission
from realtime_bridge import RealtimeBridge

logger = logging.getLogger("mode-v.session")


class CallSession:
    """Serializes calls through one Playwright browser + one Realtime bridge at a time."""

    def __init__(self, cfg: Config, browser: TalkBrowser, approvals: ApprovalStore):
        self._cfg = cfg
        self._browser = browser
        self._approvals = approvals

        self._lock = asyncio.Lock()
        self._active_token: Optional[str] = None
        self._trust: Optional[str] = None
        self._bridge: Optional[RealtimeBridge] = None
        # s8 (LKG): the profile + direction of the call currently in the slot, stashed
        # at bridge launch so teardown can record it as the Talk outlet's last-known-
        # good once the call has actually run.
        self._call_profile = None
        self._call_direction: Optional[str] = None
        self._run_task: Optional[asyncio.Task] = None
        self._teardown_task: Optional[asyncio.Task] = None
        self._reconcile_task: Optional[asyncio.Task] = None
        self._duration_task: Optional[asyncio.Task] = None   # s16 c3c: hard call ceiling
        # Monotonic call-slot generation. Bumped by every start() that claims the slot
        # AND by every teardown. It lets an in-flight start() detect that a concurrent
        # stop()/teardown superseded it while it was awaiting join_call() — closing the
        # mid-join race that would otherwise launch an orphan bridge (busy=False but a
        # live RealtimeBridge.run() task). See start().
        self._generation = 0
        # Why the LAST start() returned False, as {"code", "detail"} — or None after a
        # success. s14b: start() has four distinct False paths and used to return a bare
        # bool, so /call/outbound answered every one of them with an identical
        # {"placed": false} + 409. voice-control then labelled all of them "mode-v is
        # BUSY", which sent a live-campaign diagnosis chasing a phantom busy state while
        # the real KeyError sat in the container log. Diagnosability, not control flow.
        self._last_start_failure: Optional[dict] = None
        # s15d rejoin gate: tokens THIS bridge ended, and when. An external poller (the
        # agent's nextcloud_talk plugin) offers us calls it sees as active; after a
        # teardown that Nextcloud hasn't caught up with — or a restart mid-teardown — that
        # offer is the stale call we just ended. Rejoining it produced a convincing
        # realtime conversation that was briefly scored as the first Talk CASCADE pass
        # (a rejoin is an INBOUND join, so it runs whatever the Talk Outlet's inbound
        # Agent is - never the outbound cascade Agent the fire was meant to prove). A
        # cooldown, not a ban: a genuine callback to the same 1:1 room must still connect.
        self._recently_ended: dict = {}
        self._rejoin_cooldown_s = float(
            os.environ.get("TALK_VOICE_REJOIN_COOLDOWN_S", "90"))
        # s16 c3c: LANE-AGNOSTIC hard ceiling on one call, enforced here so BOTH pipelines
        # inherit it. Distinct from cfg.idle_timeout, which only RealtimeBridge can enforce
        # (the cascade lane has no usable idleness signal — parec streams silence forever,
        # so frame arrival means nothing and "no speech" would hang up on a quiet callee).
        # Sized "longer than any real call, shorter than a wedge nobody notices": s14b's
        # wedged slot reached 2955.9s before a human intervened. Reaching it is ALWAYS a
        # defect signal, never a normal ending.
        # Read from env HERE rather than added to Config on purpose: Config's surface is
        # pinned byte-for-byte by the d36113c parity goldens (tests/test_parity.py), and
        # this is a session-lifecycle tunable, not a bridge knob — same reasoning and same
        # idiom as _rejoin_cooldown_s above (s15d).
        self._max_call_s = float(os.environ.get("TALK_VOICE_MAX_CALL_S", "1800"))

    # -- read-only status (used by /health) -----------------------------------

    @property
    def last_start_failure(self) -> Optional[dict]:
        return self._last_start_failure

    @property
    def busy(self) -> bool:
        return self._active_token is not None

    @property
    def active_token(self) -> Optional[str]:
        return self._active_token

    @property
    def trust(self) -> Optional[str]:
        return self._trust

    # -- public entrypoints ----------------------------------------------------

    async def start(self, token: str, trust: str, caller: str, caller_display: str,
                    mission: Optional[OutboundMission] = None) -> bool:
        """Claim the single-call slot, join/place the Talk call, and start the Realtime bridge.

        ``mission`` selects OUTBOUND: we ``start_call()`` (ring the other party) instead of
        ``join_call()`` (answer a ringing call), and the bridge runs SANDBOXED — mission-only
        prompt, zero tools (see build_outbound_prompt / RealtimeBridge). ``mission=None`` is the
        unchanged inbound path. Either way there is at most ONE live call: an outbound call
        contends for the same single slot as inbound (rejected with False if one is active).

        Returns False (without disturbing any live call) if a call is already active.
        Every failure path after claiming the slot releases it before returning/raising,
        so a failed join or bridge construction never strands the lock.

        Mid-join race: the browser join/start is the only ``await`` between claiming the slot
        and launching the bridge, so it is the only window in which a concurrent
        ``stop()``/teardown can run (the plugin polls for hangup independently of this
        call's response). If that happens, teardown will have released the lock, cleared
        the state, and bumped ``_generation`` out from under us. We snapshot our generation
        right after acquiring the lock and re-check it the instant the join/start returns;
        a mismatch means we were superseded, so we launch NO bridge and touch NO shared
        state (teardown already owns cleanup) — guaranteeing no orphan bridge.
        """
        if self._lock.locked():
            logger.warning("start(%s) rejected — a call is already active (%s)", token, self._active_token)
            self._last_start_failure = {
                "code": "busy",
                "detail": f"a call is already active ({self._active_token})"}
            return False

        # s15d: refuse to rejoin a call we just ended (see _recently_ended). Outbound is
        # always a fresh dial, so the gate only applies to offered/inbound joins.
        if mission is None and self._recently_ended_by_us(token):
            logger.warning("start(%s) refused — this bridge ended that call moments ago; "
                           "declining to rejoin a stale room", token)
            self._last_start_failure = {
                "code": "recently_ended",
                "detail": f"this bridge ended call {token} within the last "
                          f"{self._rejoin_cooldown_s:.0f}s — declining to rejoin a stale "
                          f"room (a rejoin always runs realtime and can look like a pass)"}
            return False

        self._last_start_failure = None
        await self._lock.acquire()
        self._generation += 1
        my_gen = self._generation
        self._active_token = token
        self._trust = trust

        try:
            if mission is not None:
                await self._browser.start_call(token)   # outbound: ring the other party
            else:
                await self._browser.join_call(token)    # inbound: answer a ringing call
        except Exception as exc:
            logger.exception("%s(%s) failed", "start_call" if mission else "join_call", token)
            self._last_start_failure = {
                "code": "join_failed",
                "detail": f"{'start_call' if mission else 'join_call'}({token}) failed: "
                          f"{type(exc).__name__}: {exc}"}
            # Leave any half-joined call and free the slot — but only if a teardown didn't
            # race in during the failing join (in which case it owns cleanup already).
            await self._free_slot_if_owned(my_gen, leave_browser=True)
            return False

        # Mid-join race guard: a stop()/teardown ran during join_call() and already tore
        # the slot down. Do NOT build/launch a bridge (that is the orphan-bridge bug), and
        # do NOT release the lock or clear state — teardown (or a newer start that has since
        # claimed the slot) owns all of that now.
        if self._generation != my_gen or self._active_token != token:
            logger.warning("start(%s) superseded during join (gen %s→%s) — no bridge launched",
                           token, my_gen, self._generation)
            self._last_start_failure = {
                "code": "superseded",
                "detail": f"a concurrent teardown superseded this start during join "
                          f"(generation {my_gen}→{self._generation})"}
            return False

        # From here to `return True` there are NO awaits, so no concurrent stop() can
        # interleave — the run task is launched atomically with the state that reflects it.
        try:
            # s3 rule 3 (TOCTOU): ONE activation resolution per call setup — env
            # VOICE_AGENT wins, else the active.yaml pointer for this direction. The
            # snapshot overlays the base Config AND is threaded into the bridge, so
            # URL and session.update can never mix two reads. A broken selected
            # profile/pointer raises ProfileError -> the except below refuses the
            # call (slot freed, no bridge) — loud, never an env fallback.
            # s16: this bridge IS the Talk Outlet, so every resolution is the
            # talk outlet's assignment. s8 (LKG): a broken assignment answers with
            # the last-known-good snapshot instead of refusing, loudly (event-log
            # record + warning + the dashboard keeps showing the slot broken);
            # with no snapshot the loud refusal is unchanged.
            direction = "outbound" if mission is not None else "inbound"
            profile = lkg.resolve(direction, outlet=profiles.OUTLET_TALK)
            call_cfg = config.overlay_profile(self._cfg, profile)
            # Lane dispatch (s12b): a cascade profile runs the CascadeBridge (STT→LLM→TTS
            # over parec/pacat); realtime runs the RealtimeBridge. All expose run()/stop()
            # and ride the SAME single-slot lock + generation reconcile below.
            # VC24: an INBOUND cascade profile reaching here is the direct Hermes lane
            # (activation refuses every other inbound cascade; the check below holds the
            # same rule a second time at the door). It is for the OWNER only: a guest
            # keeps exactly what a guest had, the Realtime lane with the owner-approval
            # loop, because the direct lane hands the caller Hermes's own tools and has
            # no approval step to put in front of them.
            direct_inbound = (profile is not None and profile.pipeline == "cascade"
                              and mission is None)
            if direct_inbound and not profiles.is_hermes_direct(profile.doc):
                raise profiles.ProfileError(
                    "outside-vendor cascade pipeline is outbound-only - no inbound bridge")
            if direct_inbound and trust != "owner":
                logger.info("Call %s: caller trust is %r, not owner - agent '%s' is the "
                            "direct Hermes lane, so this call stays on the Realtime lane "
                            "with the bridge's own defaults", token, trust, profile.agent_id)
                profile, call_cfg, direct_inbound = None, self._cfg, False

            def realtime_bridge(rt_cfg, rt_profile):
                prompt = hermes.build_system_prompt(
                    self._cfg.config_dir, trust=trust, caller_display=caller_display)
                # caller here feeds the owner-facing approval DM ("Guest {caller} asks: …"),
                # so prefer the friendly display name, falling back to the actorId.
                return RealtimeBridge(rt_cfg, prompt, self._approvals,
                                      token_ctx={"token": token,
                                                 "caller": caller_display or caller},
                                      mission=None, profile=rt_profile)

            if direct_inbound:
                bridge = cascade_bridge.DirectLaneBridge(
                    call_cfg, profile, token=token, caller=caller_display or caller,
                    # Hermes unreachable at pickup: the Realtime lane answers with the
                    # bridge's own defaults. The direct Agent's knobs are an ElevenLabs
                    # voice and a Hermes route, which mean nothing to OpenAI.
                    realtime_factory=lambda: realtime_bridge(self._cfg, None))
            elif profile is not None and profile.pipeline == "cascade":
                bridge = cascade_bridge.CascadeBridge(call_cfg, profile, mission,
                                                      token=token)
            elif mission is not None:
                # OUTBOUND base prompt (s11a L1): tools-aware - containment sandbox only
                # for a mission-only, persona-less, tools-off call; a persona OR
                # on_call_tools yields the no-containment base. Persona + hermes_agent
                # capability stanza are appended downstream in _send_session_update.
                prompt = outbound.outbound_base_prompt(profile, mission)
                bridge = RealtimeBridge(call_cfg, prompt, self._approvals,
                                        token_ctx={"token": token, "caller": caller_display or caller},
                                        mission=mission, profile=profile)
            else:
                bridge = realtime_bridge(call_cfg, profile)
            self._bridge = bridge
            self._call_profile = profile
            self._call_direction = direction
            self._run_task = asyncio.create_task(bridge.run(), name=f"mode-v-bridge-{token}")
            # s16 c3c: LANE-AGNOSTIC hard duration cap. Deliberately NOT an idle watchdog:
            # RealtimeBridge can measure idleness because its WS delivers discrete speech
            # events, but the cascade lane cannot — parec streams silence forever, so
            # frame-arrival is meaningless, and treating "no detected speech" as idle would
            # hang up on a quiet callee mid-listen. A wall-clock ceiling needs no activity
            # signal at all, so it works identically for both pipelines and bounds the slot
            # unconditionally. This is a BACKSTOP: the plugin's hangup detection (c3b) is
            # the real detector and ends calls in ~seconds; this only catches the case
            # where every detector failed, which is exactly what s14b hit (duration_s
            # 2955.9 and climbing until a human intervened).
            self._duration_task = asyncio.create_task(
                self._duration_cap(token, my_gen), name=f"mode-v-maxdur-{token}")
            # Bind THIS call's generation to the callback so a late reconcile carries the
            # generation of the call it belongs to (not whatever is live when it fires).
            self._run_task.add_done_callback(functools.partial(self._on_bridge_done, call_gen=my_gen))
        except Exception as exc:
            # Prompt build / bridge construction failed (no run task exists yet, so no
            # orphan). Tear the browser back out and free the slot.
            logger.exception("bridge setup failed for %s — tearing down", token)
            self._last_start_failure = {
                "code": "setup_failed",
                "detail": f"bridge setup failed: {type(exc).__name__}: {exc}"}
            await self._free_slot_if_owned(my_gen, leave_browser=True)
            return False

        logger.info("Call %s started (trust=%s, caller=%s)", token, trust, caller)
        return True

    async def _duration_cap(self, token: str, call_gen: int) -> None:
        """Tear the slot down once a call has run longer than any real call should (s16 c3c).

        Generation-checked like every other deferred action here: a cap armed for call N
        must never tear down call N+1 that happens to occupy the slot when it fires.
        """
        try:
            await asyncio.sleep(self._max_call_s)
        except asyncio.CancelledError:
            raise
        if self._generation != call_gen or self._active_token != token:
            # NOT the only guard: `_teardown(expected_gen=...)` independently refuses a
            # stale generation, and verifying that (s16, sabotage) showed call B survives
            # even with this branch removed. Kept deliberately as defence in depth AND
            # because it is what stops a stale ceiling from logging the alarming
            # "exceeded max_call_duration" line below against a call that ended normally —
            # a false defect signal in exactly the log an operator reads after a wedge.
            logger.debug("duration cap for %s is stale (gen %s != %s) — ignoring",
                         token, call_gen, self._generation)
            return
        logger.warning(
            "call %s exceeded max_call_duration (%.0fs) — forcing teardown. This is a "
            "BACKSTOP: reaching it means hangup detection failed, which is a defect worth "
            "investigating, not a normal call ending.",
            token, self._max_call_s)
        await self._teardown(expected_gen=call_gen)

    async def stop(self, token: str) -> None:
        """Tear down the active call if ``token`` matches. No-op otherwise.

        Idempotent: safe to call concurrently with (or after) the bridge's own
        done-callback reconciliation — both paths funnel through ``_teardown()``.
        """
        if self._active_token != token:
            logger.info("stop(%s) ignored — active token is %s", token, self._active_token)
            return
        await self._teardown()

    # -- reconciliation ----------------------------------------------------------

    def _on_bridge_done(self, task: asyncio.Task, *, call_gen: int) -> None:
        """Fires when bridge.run() ends for ANY reason (idle timeout, hangup, error).

        ``call_gen`` is the generation of the call this bridge belonged to, bound at
        task-creation time. Passing it as ``expected_gen`` means a LATE reconcile from a
        finished call A can never clobber a live call B that has since claimed the slot
        (its generation won't match) — and it also naturally no-ops the redundant reconcile
        that fires after an explicit ``stop()`` has already torn A down. The reconcile task
        is retained (not fire-and-forget) with a done-callback so a teardown that raises
        can't be swallowed as an unretrieved-task warning and silently wedge the slot.
        """
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            logger.error("bridge.run() ended with an exception: %s", exc)
        self._reconcile_task = asyncio.create_task(
            self._teardown(expected_gen=call_gen), name="mode-v-reconcile")
        self._reconcile_task.add_done_callback(self._on_reconcile_done)

    def _on_reconcile_done(self, task: asyncio.Task) -> None:
        """Surface (and recover from) a reconcile teardown that raised.

        A teardown that raised BEFORE releasing the lock would otherwise wedge the
        single-call slot for the life of the process, so on unexpected failure we
        defensively force-free the slot.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logger.error("reconcile teardown raised — force-freeing the slot: %s", exc)
        if self._duration_task is not None:                  # s16 c3c
            self._duration_task.cancel()
            self._duration_task = None
        self._bridge = None
        self._call_profile = None
        self._call_direction = None
        self._run_task = None
        self._teardown_task = None
        self._active_token = None
        self._trust = None
        if self._lock.locked():
            self._lock.release()

    async def _teardown(self, expected_gen: Optional[int] = None) -> None:
        """Stop the bridge, leave the call, release the lock. Idempotent + awaitable-complete.

        ``expected_gen`` (passed only by the bridge-done reconcile) no-ops a superseded
        teardown: if a newer call already owns the slot, its generation won't match and we
        must not touch it. The explicit ``stop()`` path passes no ``expected_gen`` so it
        always applies to the current active call.
        """
        if expected_gen is not None and expected_gen != self._generation:
            logger.info("stale reconcile (gen %s != %s) — a newer call owns the slot; skipping",
                        expected_gen, self._generation)
            return
        if self._teardown_task is None:
            self._teardown_task = asyncio.create_task(self._teardown_impl())
        await self._teardown_task

    async def _teardown_impl(self) -> None:
        # Bump the generation FIRST, synchronously before any await, so a start() that is
        # currently suspended in join_call() sees the mismatch the moment it resumes and
        # declines to launch a bridge into a slot we're tearing down.
        self._generation += 1
        # s16 c3c: the ceiling belongs to the call being torn down — cancel it here rather
        # than letting it wake up later and find a stranger in the slot (the generation
        # check would no-op it, but leaving it pending leaks a task per call).
        if self._duration_task is not None:
            self._duration_task.cancel()
            self._duration_task = None
        bridge = self._bridge
        if bridge is not None:
            try:
                await bridge.stop()
            except Exception:
                logger.exception("bridge.stop() raised during teardown")

        # s8 (LKG): the call RAN with the snapshot stashed at launch - record it as the
        # Talk outlet's last-known-good for its direction. A call is only ever torn
        # down after its bridge existed, so this is never an activation-time write.
        # VC24: a call the pickup check moved onto the Realtime lane did NOT run on the
        # assigned Agent, so that Agent is not what just completed a call.
        # Ticket 21: nor did a cascade call whose STT was lost for good (it went deaf).
        if (bridge is not None and self._call_profile is not None
                and not getattr(bridge, "fell_back", False)
                and not getattr(bridge, "stt_lost", False)):
            lkg.record(profiles.OUTLET_TALK, self._call_direction,
                       self._call_profile, call_id=self._active_token or "")

        await self._teardown_browser_only()

        self._bridge = None
        self._call_profile = None
        self._call_direction = None
        self._run_task = None
        self._teardown_task = None
        token = self._active_token
        self._active_token = None
        self._trust = None
        if self._lock.locked():
            self._lock.release()
        logger.info("Call %s torn down — slot free", token)

    async def _teardown_browser_only(self, token: "Optional[str]" = None) -> None:
        """Leave the call — and VERIFY it actually ended (s15d).

        Clicking hang-up is best-effort: the JS click can miss, and `leave_call` then only
        navigates away, which does not leave a Talk call. s14b session 1 shipped exactly
        that state — Nextcloud showed `hasCall=True` with both participants in-call while
        the bridge declared the slot free, so every later fire 409'd and a restart rejoined
        the stale call and manufactured false cascade evidence.

        So: leave, then ask Nextcloud. If a call is still live, end it over OCS (as a
        moderator, for everyone — our own API session cannot hang up the BROWSER's
        session, which is what 404'd during the incident). Never raises: a wedged
        Nextcloud must not cost us the call slot.
        """
        try:
            await self._browser.leave_call()
        except Exception:
            logger.exception("browser.leave_call() failed during teardown")

        token = token or self._active_token
        if not token:
            return
        # Remember it BEFORE the verification round-trip: if we crash or restart midway,
        # the gate is what stops a boot-time rejoin of a call we were ending.
        self._mark_ended(token)
        try:
            state = await outbound.room_call_state(self._cfg, token)
            if state.get("hasCall"):
                logger.warning("Call %s still active after leave_call() — ending it over "
                               "OCS (the hang-up click did not take)", token)
                await outbound.end_call(self._cfg, token, everyone=True)
        except Exception:
            logger.exception("post-leave call-state verification failed for %s", token)

    def _mark_ended(self, token: str) -> None:
        self._recently_ended[token] = time.monotonic()

    def _recently_ended_by_us(self, token: str) -> bool:
        ts = self._recently_ended.get(token)
        if ts is None:
            return False
        if time.monotonic() - ts > self._rejoin_cooldown_s:
            self._recently_ended.pop(token, None)
            return False
        return True

    async def _free_slot_if_owned(self, my_gen: int, *, leave_browser: bool) -> None:
        """Best-effort cleanup for a failed start() — only while this start still owns the slot.

        Generation is re-checked around the (awaiting) browser leave: a stop() that races
        in during cleanup bumps the generation and takes over teardown, so we must not then
        release a lock a newer call may already have re-acquired.
        """
        if self._generation != my_gen:
            return  # a teardown / newer start owns the slot now
        if leave_browser:
            await self._teardown_browser_only()
        if self._generation != my_gen:
            return  # re-check after the await
        self._bridge = None
        self._call_profile = None
        self._call_direction = None
        self._run_task = None
        self._active_token = None
        self._trust = None
        if self._lock.locked():
            self._lock.release()
