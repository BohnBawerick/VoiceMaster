"""Mode V — cascade lane bridge (s12b).

The Talk sidecar's SECOND wire skin for the shared cascade turn engine
(``cascade_live.CascadeLiveSession``). Where mode-c drives that engine over a Twilio
media-stream WebSocket, the Talk lane drives it over PulseAudio: ``parec`` records the
caller (``talk_speaker.monitor``) as PCM16/24k, ``pacat`` plays the agent reply into
``talk_mic_sink``. The engine itself is one module (``voicecore.cascade_live``) both
lanes import; only the wire differs.

Two pieces:

- **PacatWire** — the ``wire`` seam impl (``emit_frame`` / ``emit_mark`` / ``emit_clear``
  + ``events()``). It owns parec+pacat and, crucially, **synthesizes playback-done**:
  pacat has no Twilio-style ``mark`` echo ("remote finished playing"), so we model the
  residual pacat buffer and enqueue a ``("mark", seq)`` event when the just-spoken
  utterance has actually drained. The drain delay is ``max(0, audio_written_s −
  elapsed_since_first_frame)`` — the real residual, NOT a fixed lead (which is wrong
  under both slow TTS and Pulse buffering). The mark carries the engine's burst number,
  and the ENGINE ignores a mark that is not the current burst's, the same guard the
  Twilio wire relies on (there is no AEC on the null-sinks, so ``_playing`` gating is
  the only echo guard). A newer utterance's first frame also cancels a stale drain here.

- **CascadeBridge** — ``run()`` / ``stop()`` surface parity with ``RealtimeBridge`` so
  ``CallSession`` can dispatch to either by ``profile.pipeline``. It constructs the engine
  (``audio_format=PCM_24K``, ``wire=PacatWire``) mirroring mode-c's ``_run_cascade_call``,
  and owns teardown ORCHESTRATION: the engine self-emits its ONE call record + at most one
  retain (D8), the wire kills parec/pacat, and outbound transcript delivery happens in code
  (never via the on-call model). End of turn comes from the STT provider
  (``cascade_live.open_stt``), the same as on the phone: the PC Smart-Turn endpoint this
  lane used to consult is gone (ticket 21).
"""
import asyncio
import functools
import logging
import os
import time
from typing import Optional

from voicecore import cascade_config
from voicecore import cascade_live
from voicecore import eventlog
from voicecore import hermes_voice
from voicecore import lkg
from voicecore import profiles
from voicecore import recording
from voicecore import summary as call_summary
import hermes
import outbound
from voicecore import turn_detect
from audio import pacat_cmd, parec_cmd
from config import Config

logger = logging.getLogger("mode-v.cascade")

# Match the realtime lane's process-termination budget so teardown timing is uniform.
_PROC_TERM_TIMEOUT = 2.0


class CascadeBridgeError(RuntimeError):
    """The cascade lane cannot run this profile (unbuildable config, unwired stage).

    Raised at construction so ``CallSession.start`` refuses the call loudly rather than
    dialing a half-wired pipeline — the mode-v peer of mode-c's ``_run_cascade_call``
    ``websocket.close()`` refusals (no silent provider remap)."""


# ------------------------------------------------------------------- PacatWire --
class PacatWire:
    """The Talk wire skin: parec (ingress) + pacat (egress) over PulseAudio null-sinks.

    Frames from parec are re-chunked to one 20 ms format-frame per media event so the VAD
    sees the exact per-frame cadence mode-c gets from Twilio. Playback-done is synthesized
    (see module docstring). Subprocess spawning is injectable (``spawn``) so units drive
    fake, paced parec/pacat without a live PulseAudio server.
    """

    def __init__(self, *, audio_format, spawn=asyncio.create_subprocess_exec,
                 clock=time.monotonic, sleep=asyncio.sleep):
        self._fmt = audio_format
        self._spawn = spawn
        self._clock = clock
        self._sleep = sleep       # drain timer seam (deterministic in units)
        self._rate = audio_format.sample_rate
        self._bytes_per_s = audio_format.sample_rate * audio_format.bytes_per_sample
        self._frame_bytes = audio_format.frame_bytes

        self._parec: Optional[asyncio.subprocess.Process] = None
        self._pacat: Optional[asyncio.subprocess.Process] = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None

        # Playback-done synthesis state. ``_utt_counter`` is bumped on the FIRST frame of
        # each utterance (not at emit_mark) so a drain-mark scheduled for utterance N is a
        # no-op the instant utterance N+1's first frame lands — closing the mid-utt-2
        # self-interrupt window that a mark-at-emit-time counter would miss.
        self._utt_counter = 0
        self._utt_first_mono: Optional[float] = None
        self._utt_bytes = 0
        self._drain_task: Optional[asyncio.Task] = None
        self._closed = False

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        """Spawn parec/pacat and start the ingress framer. Call once before the engine run."""
        self._parec = await self._spawn(
            *parec_cmd(self._rate), stdout=asyncio.subprocess.PIPE)
        self._pacat = await self._spawn(
            *pacat_cmd(self._rate), stdin=asyncio.subprocess.PIPE)
        self._reader_task = asyncio.create_task(self._read_parec(), name="cascade-v-parec")

    async def _read_parec(self) -> None:
        """Frame parec stdout into ``frame_bytes`` (20 ms) media events, then ``stop`` on EOF."""
        buf = b""
        try:
            while True:
                chunk = await self._parec.stdout.read(self._frame_bytes)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= self._frame_bytes:
                    await self._queue.put(("media", buf[:self._frame_bytes]))
                    buf = buf[self._frame_bytes:]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("parec read failed")
        finally:
            if buf and not self._closed:
                await self._queue.put(("media", buf))     # partial tail frame at EOF
            await self._queue.put(("stop", None))

    async def events(self):
        """Async-iterate ``("media", pcm) | ("mark", seq) | ("stop", None)`` — the merged
        stream of paced parec frames, synthesized drain-marks, and the stop sentinel."""
        while True:
            kind, payload = await self._queue.get()
            yield (kind, payload)
            if kind == "stop":
                return

    # -- egress verbs --------------------------------------------------------
    async def emit_frame(self, frame: bytes) -> None:
        """Write one TTS frame into pacat. A dead pacat raises → the engine tears down
        (a dead egress means the agent has gone silent — never soldier on mutely)."""
        proc = self._pacat
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise RuntimeError("pacat is dead — cannot play agent audio")
        if self._utt_first_mono is None:      # first frame of a new utterance
            self._utt_counter += 1
            self._utt_first_mono = self._clock()
            self._utt_bytes = 0
        self._utt_bytes += len(frame)
        try:
            proc.stdin.write(frame)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise RuntimeError("pacat pipe closed") from exc

    async def emit_mark(self, seq: int) -> None:
        """Schedule the synthesized playback-done for the utterance that just finished
        streaming. Drain delay = residual pacat buffer = ``max(0, written_s − elapsed_s)``.
        Keyed to ``_utt_counter`` so a newer utterance retires this mark."""
        my_utt = self._utt_counter
        if self._utt_first_mono is None:
            residual = 0.0
        else:
            written_s = self._utt_bytes / self._bytes_per_s
            elapsed_s = self._clock() - self._utt_first_mono
            residual = max(0.0, written_s - elapsed_s)
        self._utt_first_mono = None            # close the utterance; next frame reopens
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()          # utterances are sequential — one timer live
        self._drain_task = asyncio.create_task(
            self._drain_mark(my_utt, residual, seq), name="cascade-v-drain")

    async def _drain_mark(self, my_utt: int, residual: float, seq: int) -> None:
        try:
            if residual > 0:
                await self._sleep(residual)
        except asyncio.CancelledError:
            return
        # No-op if a newer utterance started (its first frame bumped the counter) or we're
        # tearing down. This is the same "generation guard" idea CallSession uses on the slot.
        if self._closed or my_utt != self._utt_counter:
            return
        await self._queue.put(("mark", seq))

    async def emit_clear(self) -> None:
        """Barge: drop queued + buffered pacat audio (kill+respawn) and cancel the pending
        synthetic mark — the interrupted utterance never 'finished playing'."""
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
        await self._flush_pacat()

    async def _flush_pacat(self) -> None:
        """Kill and respawn pacat to drop audio already queued in its stdin / Pulse buffer,
        so a barge stops playback within ~one pacing window (mirrors realtime _flush_playback)."""
        old = self._pacat
        self._pacat = None
        if old is not None and old.returncode is None:
            await self._terminate(old, "pacat(flush)")
        try:
            self._pacat = await self._spawn(
                *pacat_cmd(self._rate), stdin=asyncio.subprocess.PIPE)
        except Exception:  # noqa: BLE001
            logger.exception("pacat respawn failed — playback may be dead")
            self._pacat = None

    # -- teardown ------------------------------------------------------------
    async def aclose(self) -> None:
        """Idempotent: stop the drain timer + framer, unblock ``events()``, kill parec+pacat."""
        self._closed = True
        for task in (self._drain_task, self._reader_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._drain_task, self._reader_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._drain_task = None
        self._reader_task = None
        # Ensure a consumer still awaiting events() unblocks even if the reader was
        # cancelled mid-read (its finally may not have run).
        await self._queue.put(("stop", None))
        for name in ("_parec", "_pacat"):
            proc = getattr(self, name)
            if proc is not None and proc.returncode is None:
                await self._terminate(proc, name)
            setattr(self, name, None)

    async def _terminate(self, proc, label: str) -> None:
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=_PROC_TERM_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("%s did not exit after terminate() — killing", label)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("Error terminating %s", label)


# ----------------------------------------------------------------- CascadeBridge --
def build_config(profile, env) -> dict:
    """The cascade config for a Talk call, with the honesty gates applied. Raises
    CascadeBridgeError for an unbuildable config or an unwired stage. Split out so
    DirectLaneBridge can refuse at construction (inside CallSession.start, where a
    refusal frees the slot) while deferring every resource until after its pickup check."""
    try:
        config = cascade_config.build_cascade_config(
            profile.doc, profile.registry, env,
            base_prompt=cascade_live.base_prompt_for(profile.doc))
    except cascade_config.CascadeConfigError as exc:
        raise CascadeBridgeError(str(exc)) from exc
    # Honesty gates: refuse an unwired stage rather than silently remapping (the mode-c
    # ``websocket.close()`` refusals, raised here so start() frees the slot + no bridge).
    if not config["stt"].get("wired_live"):
        raise CascadeBridgeError(
            f"cascade STT provider '{config['stt']['provider']}' has no live client")
    if not config["tts"].get("wired_live"):
        raise CascadeBridgeError(
            f"cascade TTS provider '{config['tts']['provider']}' has no live client")
    return config


async def _unroutable_tool(instruction: str, *, profile_name: str) -> str:
    logger.error("hermes_agent: hermes_profile %r is not routable - refusing fallback "
                 "dispatch", profile_name)
    return "Sorry, I can't reach the backend for this call right now."


class CascadeBridge:
    """Cascade lane over Nextcloud Talk (mode-v). Outbound for every cascade Agent;
    inbound only on the direct Hermes lane (VC24), where ``mission`` is None.

    Constructed exactly like mode-c's ``_run_cascade_call`` (shared builder, honesty
    gates, Deepgram/TurnDetector at PCM24k) but wired to a PacatWire. ``run()`` / ``stop()``
    mirror ``RealtimeBridge`` so ``CallSession`` dispatches to either transparently.

    Teardown ownership (single owner of each obligation, no double-emit):
      - the wire kills parec/pacat and ends the event stream;
      - the ENGINE's ``teardown`` writes the ONE call record + at most one retain (D8);
      - this bridge delivers the outbound transcript in code after teardown.
    """

    def __init__(self, cfg: Config, profile, mission, *, env=None, token: str = "",
                 spawn=asyncio.create_subprocess_exec, clock=time.monotonic,
                 stt=None, wire=None, recorder=None, caller: str = ""):
        self._cfg = cfg
        self._profile = profile
        self._mission = mission            # OutboundMission, or None on an inbound call
        direction = "outbound" if mission is not None else "inbound"
        brief = mission.brief if mission is not None else ""
        self._env = dict(os.environ) if env is None else env
        self._outcome = "ok"
        self._running = False
        self._stopped = False
        self._teardown_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._clock = clock

        config = build_config(profile, self._env)
        try:
            conversation = cascade_live.hermes_conversation_for(
                config, call_id=token, token=cfg.hermes_gateway_token,
                mission_brief=brief, caller=caller)
        except ValueError as exc:
            raise CascadeBridgeError(str(exc)) from exc

        fmt = cascade_config.PCM_24K
        self._stt = stt or cascade_live.open_stt(config, self._env, fmt)
        _vad = (profile.doc.get("knobs") or {}).get("vad") or {}
        detector = turn_detect.TurnDetector(
            silence_ms=_vad.get("silence_ms") or turn_detect.DEFAULT_SILENCE_MS,
            bytes_per_ms=fmt.bytes_per_ms, decode=fmt.decode_pcm16)
        self._wire = wire or PacatWire(audio_format=fmt, spawn=spawn, clock=clock)
        # call_id is the ROOM TOKEN, exactly as the realtime Talk lane records it
        # (realtime_bridge.py) — s14b confirmed the token is the only genuine join key on
        # this transport. The old `mission.to` was doubly wrong: OutboundMission has no
        # such field (it crashed every cascade dial), and a target-derived id would have
        # given the two Talk lanes different, unjoinable conventions.
        self._recorder = recorder or eventlog.CallRecorder(
            call_id=token, mode="talk", pipeline="cascade",
            direction=direction, outlet=profiles.OUTLET_TALK,
            target=mission.target_display if mission is not None else "")
        # s16 c1: the INNER HTTP timeout must not outlive the OUTER tool budget. It was
        # 120s inside a 47s cap, so the engine cancelled and spoke an apology while the
        # request was still live — and if that turn had side effects (an email sent, a
        # memory written) the tool DID them while the caller was told it failed. Clamping
        # makes the client give up no later than the budget that governs what the caller
        # hears. (The gateway may still finish its own work; bounding THAT is a Hermes-side
        # soft deadline, recorded as a follow-up, not s16 scope.)
        # VC24: the tool goes to THIS call's hermes_profile, through the one resolver.
        # It used to go to cfg.hermes_gateway_url whatever the Agent was bound to, so a
        # second profile's tool calls landed on the default Agent's backend.
        profile_name = hermes.hermes_profile_name(profile)
        tool_gateway = hermes.gateway_url_for_profile(profile_name, cfg.hermes_gateway_url)
        if tool_gateway is None:
            hermes_call = functools.partial(_unroutable_tool, profile_name=profile_name)
        else:
            hermes_call = functools.partial(
                hermes.call_hermes_agent, gateway_url=tool_gateway,
                token=cfg.hermes_gateway_token,
                timeout=min(cfg.hermes_timeout, cascade_live.TOOL_BUDGET_S))
        # Ticket 07: the Talk Outlet's cascade lane records through the same module as
        # the phone Outlet — only the audio format differs (PCM16/24k off the Pulse
        # pipes). The engine's teardown closes it.
        self._recording = recording.start(
            call_id=token, outlet=profiles.OUTLET_TALK, direction=direction, fmt=fmt)
        self._session = cascade_live.CascadeLiveSession(
            twilio_ws=None, stream_sid=None, config=config, profile=profile,
            recorder=self._recorder, env=self._env, mission_brief=brief,
            recording=self._recording, direction=direction,
            hermes_conversation=conversation,
            stt=self._stt,
            hermes_call=hermes_call, tools_enabled=profile.on_call_tools,
            detector=detector, filler_text=cfg.filler_text,
            # s16 c1: the `max(..., 2.0)` floor that used to sit here made
            # VOICE_FILLER_DEBOUNCE_MS inert DOWNWARD — the deployed value was 1500 and the
            # lane silently ran 2000, so the knob could only ever raise the debounce, never
            # lower it. Same inert-knob class as pfSense's management_ports. The debounce is
            # now honest, and it no longer changes the tool ceiling either way (see
            # TOOL_BUDGET_S) — it only decides how soon the caller hears the filler.
            filler_debounce_s=cfg.filler_debounce_ms / 1000.0,
            retain_default=cfg.retain_enabled, hindsight_url=cfg.hindsight_url,
            hindsight_bank=cfg.hindsight_bank, audio_format=fmt, wire=self._wire,
            # Ticket 06: the post-call summariser for THIS call's Agent, resolved through
            # the same hermes_profile -> gateway seam the in-call tool uses above.
            summariser=call_summary.make_summariser(
                gateway_url=tool_gateway, token=cfg.hermes_gateway_token))

    @property
    def transcript(self) -> list:
        return self._session.transcript

    @property
    def stt_lost(self) -> bool:
        """Ticket 21: the call went deaf, so it is not a last-known-good."""
        return self._session.stt_lost

    async def run(self) -> None:
        """Spawn the audio devices, drive the engine until stop/EOF/error, tear down once.
        Blocks (CallSession launches it as the run task). Parity with RealtimeBridge.run()."""
        if self._stopped:
            raise RuntimeError("CascadeBridge is single-use")
        self._running = True
        try:
            await self._wire.start()
            if self._mission is None:
                self._idle_task = asyncio.create_task(
                    self._idle_watchdog(), name="cascade-v-idle")
            await self._session.run()
        except Exception:  # noqa: BLE001
            self._outcome = "error"
            logger.exception("cascade (mode-v) call failed")
        finally:
            await self.stop()
            # Outbound only: deliver the transcript to the owner AFTER teardown, in code
            # (never via the on-call model). Errors are swallowed inside deliver_transcript.
            if self._mission is not None:
                await outbound.deliver_transcript(
                    self._cfg, self._mission, self._session.transcript)
            logger.info("cascade (mode-v) call ended")

    async def _idle_watchdog(self) -> None:
        """INBOUND only (VC24): end the call after cfg.idle_timeout with no caller speech,
        the same rule and the same knob as RealtimeBridge's watchdog. parec streams
        silence forever, so frame arrival says nothing; an utterance Deepgram returned
        text for is the one idleness signal this lane has. Outbound keeps no watchdog on
        purpose: a quiet callee mid-listen is not an idle call. The plugin's hangup
        detection is still the real detector; this bounds the slot when it fails, well
        inside CallSession's 30-minute ceiling."""
        while True:
            await asyncio.sleep(5.0)
            idle = self._clock() - self._session.last_caller_speech
            if idle >= self._cfg.idle_timeout:
                logger.warning("cascade (mode-v) inbound call idle for %.0fs - ending it",
                               idle)
                # Ending the wire's event stream is what makes session.run() return;
                # run()'s finally then owns the one teardown.
                await self._wire.aclose()
                return

    async def stop(self) -> None:
        """Idempotent + awaitable-complete teardown (parity with RealtimeBridge.stop())."""
        if self._teardown_task is None:
            self._teardown_task = asyncio.create_task(self._teardown())
        await self._teardown_task

    async def _teardown(self) -> None:
        self._stopped = True
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        # End the wire's event stream FIRST so a still-blocked ``session.run()`` returns,
        # and kill parec/pacat. Then the engine teardown (ONE record + retain) runs with no
        # audio in flight. Both are idempotent; each obligation has exactly one owner.
        try:
            await self._wire.aclose()
        except Exception:  # noqa: BLE001
            logger.exception("wire aclose failed during teardown")
        try:
            await self._session.teardown(outcome=self._outcome)
        except Exception:  # noqa: BLE001
            logger.exception("engine teardown failed")
        self._running = False


# ------------------------------------------------------------ DirectLaneBridge --
class DirectLaneBridge:
    """An INBOUND Talk call on the direct Hermes lane, behind the pickup check (VC24).

    ``run()`` / ``stop()`` parity with the other two bridges, so ``CallSession`` launches
    it like either. It exists because the pickup check is an await, and
    ``CallSession.start`` allows none between claiming the slot and launching the run
    task (that gap is what keeps a concurrent stop() from orphaning a bridge). So the
    check runs INSIDE the run task: construction validates the config and touches no
    resource, ``run()`` probes the Agent's gateway inside ``hermes_voice.PROBE_BUDGET_S``,
    and only then builds the bridge that answers - the CascadeBridge, or, with Hermes
    unreachable, the Realtime lane with this bridge's own defaults, announced through
    the same fallback log a last-known-good answer uses (q6-failure).
    """

    def __init__(self, cfg: Config, profile, *, token: str, caller: str,
                 realtime_factory, env=None, probe=None, cascade_factory=None):
        self._cfg = cfg
        self._profile = profile
        self._token = token
        self._caller = caller
        self._env = dict(os.environ) if env is None else env
        self._probe = probe
        self._realtime_factory = realtime_factory
        self._cascade_factory = cascade_factory or (
            lambda: CascadeBridge(cfg, profile, None, env=self._env, token=token,
                                  caller=caller))
        self._inner = None
        self._stopped = False
        self.fell_back = False
        build_config(profile, self._env)          # refuse an unwired Agent in start()

    async def run(self) -> None:
        name = hermes.hermes_profile_name(self._profile)
        gateway = hermes.gateway_url_for_profile(name, self._cfg.hermes_gateway_url)
        problem = None
        if gateway is None:
            problem = (f"its hermes_profile '{name}' is not routable (not in "
                       "HERMES_PROFILE_GATEWAY_URLS, not ok in gateways.json)")
        # Looked up at call time, not bound as a default argument: a default captures
        # the function when the class is defined, so nothing could ever stand in for it.
        elif not await (self._probe or hermes_voice.probe)(gateway):
            problem = (f"the '{name}' gateway at {gateway} did not answer the pickup "
                       f"check within {hermes_voice.PROBE_BUDGET_S:.0f}s")
        if self._stopped:
            return                                # hung up during the check: answer nothing
        if problem is None:
            self._inner = self._cascade_factory()
        else:
            lkg.announce_lane_fallback(profiles.OUTLET_TALK, "inbound",
                                       self._profile.agent_id, problem, self._env)
            self.fell_back = True
            self._inner = self._realtime_factory()
        await self._inner.run()

    @property
    def stt_lost(self) -> bool:
        return bool(getattr(self._inner, "stt_lost", False))

    async def stop(self) -> None:
        self._stopped = True
        if self._inner is not None:
            await self._inner.stop()
