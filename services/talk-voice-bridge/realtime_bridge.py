"""Mode V — OpenAI Realtime (GA) bridge tied to the PulseAudio virtual devices.

This is the Mode V analogue of the Mode C Twilio bridge in ``services/voice/server.py``.
Instead of relaying G.711 μ-law frames over a Twilio Media Stream WebSocket, it pumps
raw PCM between the OpenAI Realtime GA session and the two PulseAudio null sinks that
entrypoint.sh sets up:

- caller audio  →  ``parec`` records ``talk_speaker.monitor``  →  ``input_audio_buffer.append``
- Robot's reply →  ``response.output_audio.delta``  →  ``pacat`` plays into ``talk_mic_sink``

The OpenAI session shape (GA), authentication (``Authorization: Bearer`` via
``additional_headers=``), event-name constants, tool-call round-trip and initial greeting
are all mirrored from the working Mode C sidecar. The ONLY deliberate divergence is the
audio format: Mode C uses ``{"type": "audio/pcmu"}`` (8 kHz μ-law for Twilio); Mode V uses
PCM16 @ 24 kHz because the Pulse pipes are s16le/mono/24000.
"""
import asyncio
import json
import logging
import time
from typing import Optional

import websockets

from voicecore import eventlog
import hermes
from voicecore import call_record
import outbound
from voicecore import profiles
from voicecore import cascade_config
from voicecore import recording
from voicecore import summary as call_summary
from approval import ApprovalStore
from audio import b64_to_pcm, pacat_cmd, parec_cmd, pcm_to_b64
from config import Config
from outbound import OutboundMission

logger = logging.getLogger("mode-v.realtime")

# Event types worth an INFO line (mirrors Mode C's LOG_EVENT_TYPES).
LOG_EVENT_TYPES = frozenset({
    "response.done",
    "response.created",
    "response.output_item.done",
    "input_audio_buffer.committed",
    "input_audio_buffer.speech_started",
    "input_audio_buffer.speech_stopped",
    "session.created",
    "session.updated",
    "error",
})

# ~66 ms of s16le/mono/24000 audio per append frame (48000 B/s). A read may return less.
_MIC_CHUNK_BYTES = 3200

# Events that carry a finished text ``transcript`` (outbound report-back only). Covers the
# callee's ASR and our agent's spoken output; the GA and beta output-transcript names differ,
# so both are listened for.
_TRANSCRIPT_EVENTS = frozenset({
    "conversation.item.input_audio_transcription.completed",  # callee (needs input transcription on)
    "response.output_audio_transcript.done",                  # agent (GA)
    "response.audio_transcript.done",                         # agent (beta fallback)
})

# Grace period to let subprocesses exit after terminate() before we stop waiting.
_PROC_TERM_TIMEOUT = 2.0


def _pcm_format(rate: int):
    """The Pulse pipes' audio shape, as the recorder's format descriptor (ticket 07).

    24 kHz is the configured default and has a shared constant; any other rate the
    operator sets still has to be recorded at THAT rate, so build a descriptor rather
    than silently mixing at 24k (which would play back at the wrong speed).
    """
    if rate == cascade_config.PCM_24K.sample_rate:
        return cascade_config.PCM_24K
    return cascade_config.AudioFormat(
        name=f"pcm_{rate}", sample_rate=rate, deepgram_encoding="linear16",
        bytes_per_sample=2)


def realtime_url(cfg: Config) -> str:
    """The full wss URL this bridge dials. cfg.openai_model already carries the
    profile>env>registry>coded precedence (see config.load), so with no profile this
    is byte-for-byte today's URL."""
    return f"wss://api.openai.com/v1/realtime?model={cfg.openai_model}"


class RealtimeBridge:
    """One OpenAI Realtime GA session bound to the Mode V Pulse pipes, plus two tools.

    Lifecycle: ``await run()`` opens the WS, wires the audio pumps + tool dispatch +
    idle watchdog, and blocks until teardown; ``await stop()`` (idempotent) tears
    everything down and may be called from another task (e.g. the plugin on hangup).
    """

    def __init__(self, cfg: Config, system_prompt: str, approval_store: ApprovalStore,
                 *, token_ctx: dict, mission: Optional[OutboundMission] = None,
                 profile=profiles.UNSET):
        # s3 rule 3 (TOCTOU): the per-call-setup path (CallSession.start) loads the
        # active profile ONCE and passes the snapshot (possibly None = no-profile) in;
        # cfg is already overlaid from the SAME snapshot. profiles.UNSET (the default,
        # for direct construction in tests/tools) means "resolve once at session send".
        self._profile = profile
        # s5: the profile ACTUALLY used for this session. `profile` may still be the UNSET
        # sentinel (direct construction in tests/tools), in which case _send_session_update
        # resolves one; the retained call record must name that one, or none at all.
        self._effective_profile = profile
        self._cfg = cfg
        self._system_prompt = system_prompt
        self._approvals = approval_store
        self._ctx = token_ctx
        # When set, this is an OUTBOUND call: the session is SANDBOXED (no tools, mission-only
        # instructions — see _send_session_update) and every turn's transcript is captured
        # here for the code-driven report-back on teardown (see run()). None => normal inbound.
        self._mission = mission
        self._transcript: list[str] = []

        self._ws = None
        self._parec: Optional[asyncio.subprocess.Process] = None
        self._pacat: Optional[asyncio.subprocess.Process] = None
        self._tasks: list[asyncio.Task] = []
        # Tool calls run OFF the receive loop (see _receive_from_openai) so a slow
        # Hermes backend turn can't stall the single WS consumer. Track them so
        # teardown can cancel any in-flight tool round-trip.
        self._tool_tasks: set[asyncio.Task] = set()
        # Serialize tool round-trips: each ends with a `response.create`, and OpenAI
        # allows only one active response at a time. If the model ever emits parallel
        # function calls, the extra tasks queue on this lock (they never block the
        # receive loop) instead of racing two `response.create`s into an error.
        self._tool_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._last_activity = 0.0
        # A2 barge-in: the assistant audio item currently playing (from audio-delta events)
        # and how many ms of it we've played (PCM16 @ audio_rate → 48 B/ms). On caller
        # speech_started we drop buffered playback and truncate the model's context to played_ms.
        self._current_item_id: Optional[str] = None
        self._played_ms: float = 0.0
        # Idea 1: gate so the filler and result response.create can't overlap (OpenAI allows one
        # active response at a time). Set = idle; the receive loop clears on response.created, sets
        # on response.done. Starts idle.
        self._response_idle = asyncio.Event()
        self._response_idle.set()
        self._running = False
        self._stopped = False
        self._teardown_task: Optional[asyncio.Task] = None
        # Idea 2: structured per-call/per-turn event log. call_id + caller come from the session
        # token context; direction/mode identify this surface for a future dashboard.
        # s16 + s5: this bridge IS the Talk Outlet - the same constant it resolves its
        # Agent with, recorded rather than derived from the transport or the direction.
        self._recorder = eventlog.CallRecorder(
            call_id=str(self._ctx.get("token", "")), mode="talk", pipeline="realtime",
            direction="outbound" if self._mission is not None else "inbound",
            outlet=profiles.OUTLET_TALK,
            caller=str(self._ctx.get("caller", "")),
            target=(self._mission.target_display if self._mission is not None else ""))
        # Ticket 07: both legs of this call, teed into a stereo Opus file. Capture is
        # ARMED in run() (so a bridge that is built but never runs starts no writer) and
        # closed in _teardown(); until then this is the no-op object, so the audio pumps
        # below have exactly ONE shape whether or not a recording is possible.
        self._recording = recording.NullRecording()
        self._recording_result = None

    def _agent_id(self) -> "str | None":
        """The Agent this session actually ran as, or None when there was none (s5).

        None is not a placeholder for "we could not be bothered to look" - a Talk call with
        no assignment on this Outlet genuinely has no Agent identity, and the archive says
        so by carrying no agent field at all rather than a stand-in string.
        """
        profile = self._effective_profile
        if profile is profiles.UNSET or profile is None:
            return None
        return getattr(profile, "agent_id", None) or None

    def _summariser(self):
        """Ticket 06: the post-call summariser for THIS call's Agent, or None when off.

        Resolves the gateway through the SAME seam the in-call hermes_agent tool uses
        (`hermes_profile` -> gateway URL), so the summary is written by the Agent that was
        on the call. An unknown profile yields no gateway, which the summariser reports as
        `unavailable` - it never silently borrows another Agent's backend.
        """
        profile = None if self._effective_profile is profiles.UNSET else self._effective_profile
        return call_summary.make_summariser(
            gateway_url=hermes.gateway_url_for_profile(
                hermes.hermes_profile_name(profile), self._cfg.hermes_gateway_url),
            token=self._cfg.hermes_gateway_token)

    # -- public entrypoints --------------------------------------------------

    async def run(self) -> None:
        """Open the Realtime session and run until idle/hangup/error. Blocks."""
        if self._stopped:
            raise RuntimeError("RealtimeBridge is single-use")
        if self._running:
            logger.warning("run() called while already running — ignoring")
            return
        if not self._cfg.openai_api_key:
            logger.error("OPENAI_API_KEY not set — cannot open Realtime session")
            return
        self._running = True
        self._last_activity = time.monotonic()

        # Ticket 07: the Pulse pipes carry s16le/mono at cfg.audio_rate. start() never
        # raises and never returns None.
        self._recording = recording.start(
            call_id=str(self._ctx.get("token", "")), outlet=profiles.OUTLET_TALK,
            direction="outbound" if self._mission is not None else "inbound",
            fmt=_pcm_format(self._cfg.audio_rate))

        url = realtime_url(self._cfg)
        # s5: how this call ENDED, recorded rather than assumed. The except below is the
        # only thing that changes it; before this ticket every call recorded outcome="ok",
        # including the ones a raised exception tore down.
        outcome = "ok"
        call_err = None
        try:
            # GA Realtime API: NO "OpenAI-Beta: realtime=v1" header (the beta shape was
            # retired 2026-05-12 and now hard-errors with `beta_api_shape_disabled`).
            # additional_headers= requires websockets>=14 (16.0 is installed).
            async with websockets.connect(
                url,
                additional_headers={"Authorization": f"Bearer {self._cfg.openai_api_key}"},
            ) as ws:
                self._ws = ws
                await self._send_session_update(ws)
                await self._send_initial_greeting(ws)

                rate = self._cfg.audio_rate
                self._parec = await asyncio.create_subprocess_exec(
                    *parec_cmd(rate), stdout=asyncio.subprocess.PIPE)
                self._pacat = await asyncio.create_subprocess_exec(
                    *pacat_cmd(rate), stdin=asyncio.subprocess.PIPE)

                self._tasks = [
                    asyncio.create_task(self._pump_mic(ws), name="mode-v-mic"),
                    asyncio.create_task(self._receive_from_openai(ws), name="mode-v-recv"),
                    asyncio.create_task(self._idle_watchdog(), name="mode-v-watchdog"),
                ]
                # Any task that ends (parec EOF, WS close, idle timeout) sets the event.
                await self._stop_event.wait()
        except Exception as exc:  # noqa: BLE001
            outcome = "error"
            call_err = f"{type(exc).__name__}: {exc}"
            logger.exception("Realtime session failed")
        finally:
            await self.stop()
            logger.info("Realtime session ended")
            # Outbound only: deliver the captured transcript to the owner AFTER teardown, in
            # code (never via the on-call model). deliver_transcript swallows its own errors,
            # so a failed report can't wedge the run task / slot reconciliation.
            if self._mission is not None:
                await outbound.deliver_transcript(self._cfg, self._mission, self._transcript)
            # Idea 4: retain BOTH directions into the call archive (fire-and-forget, so this
            # never blocks teardown / slot reconciliation).
            # Ticket 07: _teardown() already closed the capture (it owns the lifecycle so
            # a stop() that never reaches here still closes it); finish() is idempotent,
            # so this is just how the result reaches the metadata.
            rec = self._recording_result or await recording.finish_async(self._recording)
            retain_status, doc_id = "skipped", None
            if self._cfg.retain_enabled and self._transcript:
                doc_id = f"voice-talk-{self._recorder.call_id}"
                # s5 (ticket 05): shared builder, so this lane's archive rows carry the same
                # fields the phone lanes' do.
                retain_status = call_record.retain_call(
                    url=self._cfg.hindsight_url, bank=self._cfg.hindsight_bank,
                    recorder=self._recorder, transcript=self._transcript,
                    document_id=doc_id, platform="voice_talk", lane="talk",
                    agent=self._agent_id(),
                    mission=(self._mission.brief if self._mission is not None else None),
                    outcome=outcome,
                    recording=rec.ref,   # ticket 07: one additive field
                    summariser=self._summariser())   # ticket 06: the second
                if retain_status != "dispatched":
                    doc_id = None
            try:
                self._recorder.finish(outcome=outcome, transcript_ref=doc_id,
                                      retain_status=retain_status, err=call_err,
                                      recording_ref=rec.ref,
                                      recording_status=rec.status)
            except Exception:  # noqa: BLE001
                logger.exception("eventlog finish failed")

    async def stop(self) -> None:
        """Cancel tasks, terminate parec/pacat, close the WS.

        Idempotent AND awaitable-complete: the first caller starts the teardown as a
        single task; any concurrent/later caller awaits that same task, so ``await stop()``
        always means "teardown has actually finished" (Task 9's CallSession reuses the
        Pulse devices for the next call and relies on this).
        """
        if self._teardown_task is None:
            self._teardown_task = asyncio.create_task(self._teardown())
        await self._teardown_task

    async def _teardown(self) -> None:
        self._stopped = True
        self._stop_event.set()

        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

        # Cancel any in-flight tool round-trip (Hermes HTTP call / owner-approval wait)
        # so a hung backend can't keep the session half-alive after hangup. Snapshot
        # first — the done-callback mutates the set as tasks finish.
        tool_tasks = list(self._tool_tasks)
        for task in tool_tasks:
            task.cancel()
        if tool_tasks:
            await asyncio.gather(*tool_tasks, return_exceptions=True)
        self._tool_tasks.clear()

        for name in ("_parec", "_pacat"):
            proc = getattr(self, name)
            if proc is not None and proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=_PROC_TERM_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("%s did not exit after terminate() — killing", name)
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                except ProcessLookupError:
                    pass
                except Exception:  # noqa: BLE001
                    logger.exception("Error terminating %s", name)
            setattr(self, name, None)

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                logger.debug("Error closing OpenAI WS", exc_info=True)
            self._ws = None
        # Ticket 07: the audio is over, so close the capture here — teardown is the ONE
        # path every ending funnels through. Bounded, non-raising, and the writer-thread
        # join happens off the event loop.
        self._recording_result = await recording.finish_async(self._recording)
        self._running = False

    # -- audio pumps ---------------------------------------------------------

    async def _pump_mic(self, ws) -> None:
        """Record caller audio from talk_speaker.monitor and stream it to OpenAI."""
        if self._parec is None or self._parec.stdout is None:
            logger.error("Mic pump started without a parec stdout — aborting")
            self._stop_event.set()
            return
        try:
            while True:
                chunk = await self._parec.stdout.read(_MIC_CHUNK_BYTES)
                if not chunk:
                    logger.info("parec reached EOF — mic pump stopping")
                    break
                if ws.state.name != "OPEN":
                    break
                await ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": pcm_to_b64(chunk),
                }))
                # Ticket 07: tee the caller's leg AFTER the forward — capture never sits
                # between parec and the model.
                self._recording.caller_audio(chunk)
        except asyncio.CancelledError:
            raise
        except websockets.ConnectionClosed:
            # Ordinary server-side hangup racing the check→send window — not an error.
            logger.info("OpenAI WS closed during mic append — mic pump stopping")
        except Exception:  # noqa: BLE001
            logger.exception("Mic pump failed")
        finally:
            self._stop_event.set()

    async def _play(self, b64_delta: str) -> None:
        """Write one Robot-audio delta into pacat's stdin (→ talk_mic_sink).

        A dead/broken pacat means Robot has gone silent, so tear the call down
        promptly rather than limping on until the idle watchdog fires.
        """
        proc = self._pacat
        if proc is None or proc.stdin is None or proc.returncode is not None:
            self._stop_event.set()
            return
        try:
            proc.stdin.write(b64_to_pcm(b64_delta))
            await proc.stdin.drain()
            # Ticket 07: record what actually reached pacat, after it reached pacat.
            self._recording.agent_audio(b64_delta, item_id=self._current_item_id)
        except (BrokenPipeError, ConnectionResetError):
            logger.warning("pacat pipe closed — tearing down")
            self._stop_event.set()
        except Exception:  # noqa: BLE001
            logger.exception("Playback write failed")
            self._stop_event.set()

    async def _handle_barge_in(self, ws) -> None:
        """A2: the caller started talking over Robot. Drop buffered playback and truncate the
        model's context to what was actually played, so Robot stops promptly and its transcript
        matches what the caller heard. Reset the tracking first so a second speech_started before
        the next delta is a no-op."""
        item_id, played_ms = self._current_item_id, self._played_ms
        self._current_item_id = None
        self._played_ms = 0.0
        self._response_idle.set()  # Idea 1: truncated response is over — release the filler/result gate
        # Ticket 07: _flush_playback drops everything queued in pacat and the Pulse
        # buffer, so the recording must drop the same tail — the caller never heard it.
        self._recording.agent_truncate(played_ms)
        await self._flush_playback()
        try:
            await ws.send(json.dumps({
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": 0,
                "audio_end_ms": int(played_ms),
            }))
        except Exception:  # noqa: BLE001
            logger.exception("Barge-in truncate failed")

    async def _flush_playback(self) -> None:
        """Kill and respawn pacat to drop audio already queued in its stdin / the Pulse buffer.
        Reassign self._pacat before the next _play so playback keeps working for the rest of the
        call. A failed respawn leaves _pacat None → the next _play tears the call down (as today)."""
        old = self._pacat
        self._pacat = None
        if old is not None and old.returncode is None:
            try:
                old.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(old.wait(), timeout=_PROC_TERM_TIMEOUT)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        try:
            self._pacat = await asyncio.create_subprocess_exec(
                *pacat_cmd(self._cfg.audio_rate), stdin=asyncio.subprocess.PIPE)
        except Exception:  # noqa: BLE001
            logger.exception("pacat respawn failed — playback may be dead")
            self._pacat = None

    # -- OpenAI receive loop -------------------------------------------------

    async def _receive_from_openai(self, ws) -> None:
        """Consume OpenAI events: relay audio, dispatch tools, feed the watchdog."""
        try:
            async for raw in ws:
                ev = json.loads(raw)
                etype = ev.get("type", "")

                if etype in LOG_EVENT_TYPES:
                    logger.info("OpenAI: %s", etype)

                if etype == "error":
                    logger.error("OpenAI error: %s", ev.get("error", {}))
                elif etype == "input_audio_buffer.speech_started":
                    self._last_activity = time.monotonic()  # reset idle watchdog
                    if self._current_item_id is not None:
                        await self._handle_barge_in(ws)  # A2: caller talked over Robot
                elif etype == "input_audio_buffer.speech_stopped":
                    self._recorder.on_speech_stopped(time.monotonic())  # TTFB clock starts
                elif etype == "response.created":
                    self._response_idle.clear()  # Idea 1: a response is now in flight
                elif etype == "response.done":
                    self._recorder.on_response_done(
                        usage=(ev.get("response") or {}).get("usage"), ts=time.time())
                    self._response_idle.set()  # Idea 1: filler/result gate — response finished
                    self._current_item_id = None  # A2: turn finished — nothing to truncate
                    self._played_ms = 0.0
                elif etype in _TRANSCRIPT_EVENTS:
                    # Transcript capture for BOTH directions now (input transcription is enabled
                    # for every session — see _send_session_update). "Them" = the other party's
                    # ASR; "AI" = what our agent said. Fed to the outbound report-back AND the
                    # Idea 4 memory retain on teardown.
                    text = (ev.get("transcript") or "").strip()
                    if text:
                        who = "Them" if "input_audio_transcription" in etype else "AI"
                        self._transcript.append(f"{who}: {text}")
                elif etype == "response.function_call_arguments.done":
                    # Dispatch the tool OFF the receive loop, as a tracked task. The
                    # Hermes backend call can take many seconds (up to hermes_timeout),
                    # and a guest approval blocks even longer. Awaiting it *here* would
                    # stall this single WS consumer: the inbound frame queue fills, the
                    # websockets library applies backpressure and stops reading the
                    # socket — which also stalls pong handling, so OpenAI's keepalive
                    # ping times out and drops the call (observed live: a call died with
                    # `sent 1011 (internal error) keepalive ping timeout` mid-tool).
                    # Running it as a task keeps the loop draining audio + control
                    # frames throughout the backend turn.
                    task = asyncio.create_task(
                        self._handle_tool_guarded(ws, ev), name="mode-v-tool")
                    self._tool_tasks.add(task)
                    task.add_done_callback(self._tool_tasks.discard)
                elif etype in ("response.output_audio.delta", "response.audio.delta") \
                        and ev.get("delta"):
                    # A2: track the playing item + how much we've played, for truncate on barge-in.
                    item_id = ev.get("item_id")
                    if item_id and item_id != self._current_item_id:
                        self._current_item_id = item_id
                        self._played_ms = 0.0
                    try:
                        self._played_ms += len(b64_to_pcm(ev["delta"])) / 48.0
                    except Exception:  # noqa: BLE001
                        pass
                    self._recorder.on_audio_delta(time.monotonic())  # first-after-speech = TTFB
                    await self._play(ev["delta"])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("OpenAI receive loop ended")
        finally:
            self._stop_event.set()

    # -- idle watchdog -------------------------------------------------------

    async def _idle_watchdog(self) -> None:
        """Backstop teardown: tear down if no caller speech for cfg.idle_timeout seconds.

        The authoritative hangup detection lives in the plugin; this only guards against
        a wedged/abandoned call the plugin never told us about.
        """
        try:
            while not self._stop_event.is_set():
                remaining = self._cfg.idle_timeout - (time.monotonic() - self._last_activity)
                if remaining <= 0:
                    logger.info("Idle for %.0fs — tearing down", self._cfg.idle_timeout)
                    break
                await asyncio.sleep(min(remaining, 5.0))
        except asyncio.CancelledError:
            raise
        finally:
            self._stop_event.set()

    # -- tool dispatch -------------------------------------------------------

    async def _handle_tool_guarded(self, ws, ev) -> None:
        """Run _handle_tool with error isolation (it now runs as its own task).

        A stray tool error (bad ctx, backend hiccup, future bug) is logged and the
        call continues; cancellation during teardown propagates cleanly.
        """
        try:
            async with self._tool_lock:
                await self._handle_tool(ws, ev)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Tool dispatch failed — continuing call")

    async def _handle_tool(self, ws, ev) -> None:
        name = ev.get("name", "")
        call_id = ev.get("call_id", "")
        try:
            args = json.loads(ev.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {}
        # Keep the idle watchdog off our back for the whole (possibly long) tool turn:
        # if the caller waits in silence for the backend, no speech events arrive to
        # reset _last_activity. Refresh on entry and again after each blocking step.
        self._last_activity = time.monotonic()
        if name == "request_owner_approval":
            summary = args.get("summary", "").strip() or "an unspecified action"
            logger.info("Tool call: request_owner_approval(%r)", summary[:200])
            try:
                aid = self._approvals.create(token=self._ctx["token"],
                                             caller=self._ctx["caller"], summary=summary)
            except RuntimeError:
                await self._tool_result(ws, call_id, "An approval is already in progress."); return
            # The approval wait blocks this receive loop, so no speech events refresh the
            # idle watchdog while we wait on the owner. Refresh it around the wait so a slow
            # owner reply can't trip the idle backstop mid-approval (config keeps
            # idle_timeout > approval_timeout as a second guard).
            self._last_activity = time.monotonic()
            verdict = await self._approvals.await_verdict(aid, timeout=self._cfg.approval_timeout)
            self._last_activity = time.monotonic()
            logger.info("Owner approval verdict: %s", verdict)
            msg = {"approved": "The owner approved. Perform the action now with hermes_agent, then tell the guest.",
                   "denied":   "The owner denied this. Tell the guest you can't do it.",
                   "timeout":  "The owner didn't respond in time. Tell the guest to try again later."}[verdict]
            await self._tool_result(ws, call_id, msg); return
        # hermes_agent
        instruction = args.get("instruction", "") or ev.get("arguments", "")
        logger.info("Tool call: hermes_agent(%r)", instruction[:200])
        # s11a c4: route to the gateway for THIS call's hermes_profile (inbound AND
        # outbound share this seam). Unknown profile → fail HONESTLY: never silent-fallback
        # to the default backend, or a misconfigured profile's tool calls hit the wrong agent.
        _prof = None if self._profile is profiles.UNSET else self._profile
        gateway_url = hermes.gateway_url_for_profile(
            hermes.hermes_profile_name(_prof), self._cfg.hermes_gateway_url)
        if gateway_url is None:
            logger.error("hermes_agent: unknown hermes_profile %r — refusing fallback dispatch",
                         hermes.hermes_profile_name(_prof))
            await self._tool_result(
                ws, call_id,
                "Sorry, I can't reach the backend for this call right now."); return
        started = time.monotonic()
        res_task = asyncio.create_task(hermes.call_hermes_agent(
            instruction, gateway_url=gateway_url,
            token=self._cfg.hermes_gateway_token, timeout=self._cfg.hermes_timeout))
        filler_fired = False
        try:
            done, _ = await asyncio.wait({res_task}, timeout=self._cfg.filler_debounce_ms / 1000.0)
            if res_task not in done:
                # Slow turn — fill the dead air so Robot isn't mute while the backend works.
                await self._create_response(ws, {
                    "instructions": f"Say exactly, warmly and briefly: '{self._cfg.filler_text}'"})
                filler_fired = True
            result = await res_task
        except asyncio.CancelledError:
            res_task.cancel()
            raise
        self._last_activity = time.monotonic()  # backend may have taken many seconds
        logger.info("Hermes result (%.1fs, filler=%s): %r",
                    self._last_activity - started, filler_fired, result[:300])
        self._recorder.on_tool_call("hermes_agent", (self._last_activity - started) * 1000.0, ok=True)
        await self._tool_result(ws, call_id, result)

    async def _tool_result(self, ws, call_id: str, output_text: str) -> None:
        """Return a function result to OpenAI and trigger a new spoken response."""
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": output_text,
            },
        }))
        await self._create_response(ws)

    async def _create_response(self, ws, response_payload=None) -> None:
        """Send response.create after waiting for any active response to finish (OpenAI allows one
        at a time). Claims the slot (clear) before sending so a follow-up can't race the receive
        loop's response.created; the wait is time-boxed so a missed response.done can't wedge us."""
        try:
            await asyncio.wait_for(self._response_idle.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("response_idle wait timed out — creating response anyway")
        self._response_idle.clear()
        msg = {"type": "response.create"}
        if response_payload is not None:
            msg["response"] = response_payload
        await ws.send(json.dumps(msg))

    # -- session setup -------------------------------------------------------

    async def _send_session_update(self, ws) -> None:
        """Configure the OpenAI Realtime session (GA shape, PCM16 @ audio_rate).

        Mirrors Mode C's `_send_session_update` structure exactly (GA `session.type`,
        `output_modalities`, nested `audio.input`/`audio.output`, format *objects*,
        server_vad, voice under `audio.output.voice`). Only the codec differs: Mode C
        uses `{"type": "audio/pcmu"}` (Twilio μ-law); Mode V uses raw PCM16 at the Pulse
        pipe rate.
        """
        # s1 voice profiles: cfg already carries voice/model/transcription/silence with
        # profile>env>registry>coded precedence (config.load / overlay_profile). The
        # knobs WITHOUT env vars (vad threshold/prefix) and the persona overlay resolve
        # here, at the single session.update construction site. No profile selected =>
        # profile is None, every value below is exactly the pre-profile constant, and
        # no config file is ever touched. s3: the snapshot threaded in at construction
        # (CallSession.start's ONE load) wins; profiles.UNSET resolves once right here.
        if self._profile is profiles.UNSET:
            profile = profiles.load_effective_profile(
                "outbound" if self._mission is not None else "inbound",
                outlet=profiles.OUTLET_TALK)
        else:
            profile = self._profile
        self._effective_profile = profile
        instructions = (self._system_prompt if profile is None
                        else profile.compose_instructions(self._system_prompt))
        vad_threshold = 0.5 if profile is None else profile.resolve(
            ("vad", "threshold"), None, 0.5)
        vad_prefix_ms = 300 if profile is None else profile.resolve(
            ("vad", "prefix_padding_ms"), None, 300)
        # GA PCM16 format object — UNCONFIRMED; Task 8 loopback must verify this key/enum
        # (the `audio/pcm` type + `rate`). Mode C only exercises `audio/pcmu`, so this exact
        # shape has never been round-tripped against a live GA session. Used for both
        # input and output below.
        pcm_format = {"type": "audio/pcm", "rate": self._cfg.audio_rate}
        input_block = {
            "format": pcm_format,
            "turn_detection": {
                "type": "server_vad",
                "threshold": vad_threshold,
                "prefix_padding_ms": vad_prefix_ms,
                # Lowered from 500 → cfg.vad_silence_ms (default 250) to cut ~250 ms
                # of end-of-turn wait off every reply. Env-tunable via
                # TALK_VOICE_VAD_SILENCE_MS. See Config.vad_silence_ms.
                "silence_duration_ms": self._cfg.vad_silence_ms,
            },
        }
        # OUTBOUND sandbox: NO tools (the on-call model has no channel to reach Hermes or the
        # owner's data — the core guardrail, enforced in code not prompt). INBOUND keeps the
        # full hermes.TOOLS backend reach. Caller-side ASR is enabled for BOTH directions below.
        # s3 guardrails.on_call_tools — boolean, FAIL-CLOSED, inbound-inert: only a
        # selected profile carrying a literal true restores the INBOUND tool set (same
        # hermes.TOOLS object, same tool_choice) on an outbound session; false/absent/
        # no-profile keep the sandbox cut byte-identical. Inbound never reads the field.
        if self._mission is not None:
            if profile is not None and profile.on_call_tools:
                # s11a c3: outbound exports hermes_agent ONLY (the inbound-guest
                # request_owner_approval flow is meaningless to a party we dialled).
                tools, tool_choice = hermes.OUTBOUND_TOOLS, "auto"
            else:
                tools, tool_choice = [], "none"
        else:
            tools, tool_choice = hermes.TOOLS, "auto"
        # s11a c2 (L1): tell the model it HOLDS hermes_agent exactly when tools are open on
        # an outbound call — the persona/context base never mentions it, so without this the
        # model carries a tool it was never told about. Inbound already describes it (SOUL).
        if self._mission is not None and tools:
            instructions = f"{instructions}\n\n{hermes.CAPABILITY_STANZA}"
        # Idea 4: input transcription on for BOTH directions now, so inbound calls can be
        # captured and retained to memory (was outbound-only for the report-back).
        input_block["transcription"] = {"model": self._cfg.transcription_model}
        session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "output_modalities": ["audio"],
                "instructions": instructions,
                "audio": {
                    "input": input_block,
                    "output": {
                        "format": pcm_format,
                        "voice": self._cfg.openai_voice,
                    },
                },
                "tools": tools,
                "tool_choice": tool_choice,
            },
        }
        logger.info("Sending session.update (GA API, PCM16 @ %d Hz, %s)",
                    self._cfg.audio_rate, "OUTBOUND sandbox / no tools" if self._mission else "inbound")
        await ws.send(json.dumps(session_update))

    async def _send_initial_greeting(self, ws) -> None:
        """Inject an opener so Robot speaks first without waiting for the other party.

        Inbound: greet the caller. Outbound: we dialled THEM, so open the mission the moment
        they pick up (the mission brief in the instructions says who we are and why we're calling).
        """
        opener = ("(The call just connected — the other party has answered. Open the conversation "
                  "now: greet them briefly and get to the point of your mission.)"
                  if self._mission is not None else
                  "(The call just connected. Greet the caller warmly and briefly.)")
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": opener,
                }],
            },
        }))
        await ws.send(json.dumps({"type": "response.create"}))
        logger.info("Sent initial greeting trigger")
