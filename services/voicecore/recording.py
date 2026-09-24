"""Per-call audio capture — one stereo Opus file per Call (ticket 07).

We record the audio that is ALREADY passing through our own process on both Outlets:
the phone bridge relays μ-law/8k frames between Twilio and the model, the Talk bridge
pumps PCM16/24k between the Pulse null-sinks and the model, and the cascade engine
does the same through its wire seam. Nothing is asked of Twilio, nothing is stored on
their servers, and there is no per-minute recording fee.

THE BAR THIS MODULE IS BUILT AGAINST
------------------------------------
The call is the product; the recording is a by-product. Capture must not add delay to
the live path, and a recording failure must never degrade or drop a live call. Two
structural decisions make that true rather than hoped for, and the suites in
``tests/test_recording.py`` (plus the per-bridge suites) pin both:

1. **The live path only ever does a bounded, non-blocking, exception-free enqueue.**
   ``caller_audio``/``agent_audio``/``agent_truncate`` copy a reference onto a
   ``queue.Queue`` with ``put_nowait`` inside a blanket ``try/except`` and return. No
   encode, no syscall, no ``await``, no unbounded growth. When the queue is FULL the
   frame is DROPPED and counted — never blocked on, because blocking is exactly how a
   stalled disk would reach the caller's ear.

2. **Everything that can block or fail runs on a dedicated writer thread.** μ-law
   decode, stereo interleave, the ffmpeg pipe, the file system and the final rename all
   live there. A writer that raises, stalls, or fills the disk marks the recording
   failed, drains the queue and lets the call continue exactly as if this module did
   not exist.

The failure is never swallowed silently: it lands in the container log AND in the
sidecar JSON the dashboard reads, so a call whose capture failed says so on the screen
instead of showing a broken player.

TIMELINE
--------
Two legs arrive independently, so the writer places each frame at
``max(that leg's cursor, wall-clock offset since t0)`` and pads the gap with silence.
Never moving a cursor backwards keeps a burst (the model emits a turn's audio faster
than real time) contiguous, while the wall clock anchors the START of each burst — which
is when the listener actually heard it. ``agent_truncate`` walks the agent cursor back
when a barge-in means the tail we relayed was discarded before playout.

LAYOUT
------
``$VOICE_RECORDINGS_DIR/YYYY/MM/<call_id>.opus`` plus a ``.json`` sidecar holding what
the dashboard needs to render (and what it needs to be honest about a failure). The
file is written as ``.part`` and renamed on success, so a crashed process leaves no
half file that reads like a recording.
"""
import asyncio
import base64
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .cascade_config import AudioFormat, MULAW_8K, PCM_24K  # noqa: F401  (re-exported)

logger = logging.getLogger("voice.recording")

SCHEMA_VERSION = 1

# Where recordings land. In a deployment this is a volume shared with the dashboard
# (docker-compose.yml); locally/in tests it is a tmpdir.
DEFAULT_ROOT = os.environ.get("VOICE_RECORDINGS_DIR", "/app/recordings")

# Master switch. Recording is on by default — a Call the owner cannot play back is the
# thing ticket 07 exists to fix — but one env var turns the whole feature off without a
# code change if it ever needs to be taken out of the live path in a hurry.
def _enabled_default() -> bool:
    return os.environ.get("VOICE_RECORDING_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on")


# Opus bitrate for the stereo mix. 32 kbps is ~4 KB/s: a one-hour call is ~14 MB, a
# ten-minute call ~2.4 MB — "megabytes rather than tens of megabytes" (ticket 07).
OPUS_BITRATE = os.environ.get("VOICE_RECORDING_BITRATE", "32k")

# The queue is the shock absorber between the live path and the disk. 2000 items is
# ~40 s of phone audio (20 ms μ-law frames) or ~2 min of Talk audio (66 ms PCM chunks)
# of writer stall before the first frame is dropped, at a few MB of RAM worst case.
QUEUE_MAXSIZE = int(os.environ.get("VOICE_RECORDING_QUEUE", "2000"))

# How long teardown waits for the writer to drain and the encoder to exit. Teardown
# happens after the call is over, but the Talk Outlet has ONE call slot and this sits in
# its reconcile path, so a wedged writer must not hold the next call hostage: bounded,
# and a timeout is recorded as a failed recording. Losing the tail of a by-product beats
# delaying the product.
FINISH_TIMEOUT_S = float(os.environ.get("VOICE_RECORDING_FINISH_TIMEOUT_S", "10"))

# A leg lagging the clock by more than this is padded with silence so the mix can be
# flushed. Without it a silent leg (the agent between turns) would pin the flush point
# and the whole call would sit in RAM. It is also the ceiling on how late a frame may
# arrive and still land at its own timestamp rather than after the padding — 3 s is
# orders of magnitude past any real transport jitter, and it bounds the pending mix to
# ~3 s per leg (≈50 KB on the phone Outlet, ≈150 KB on Talk).
MAX_LEG_LEAD_S = 3.0

# Queue item kinds.
_CALLER = 0
_AGENT = 1
_TRUNCATE = 2
_STOP = 3

# Sidecar/result statuses.
OK = "ok"
FAILED = "failed"
EMPTY = "empty"          # the call produced no audio at all (nothing to encode)
DISABLED = "disabled"    # recording was off, or could not be started


def _safe_call_id(call_id) -> str:
    """The filename form of a call id.

    Call ids come from outside this process (a Twilio ``streamSid``, a Talk room token),
    so they are never interpolated into a path unchecked. Anything outside
    ``[A-Za-z0-9._-]`` is replaced, and a leading dot cannot survive — no traversal, no
    hidden file, no absolute path.
    """
    text = str(call_id or "").strip()
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "_" for c in text)
    cleaned = cleaned.lstrip(".")[:120]
    return cleaned or "unknown"


class RecordingResult:
    """What ``finish()`` hands back: enough for the metadata reference, the eventlog
    row and an honest sidecar. Never a lie — ``status`` is ``failed`` when the audio
    did not make it to disk, and ``ref`` is None in every case but ``ok``."""

    __slots__ = ("call_id", "status", "ref", "path", "duration_s", "size_bytes",
                 "dropped_frames", "error")

    def __init__(self, *, call_id, status, ref=None, path=None, duration_s=None,
                 size_bytes=None, dropped_frames=0, error=None):
        self.call_id = call_id
        self.status = status
        self.ref = ref
        self.path = path
        self.duration_s = duration_s
        self.size_bytes = size_bytes
        self.dropped_frames = dropped_frames
        self.error = error

    @property
    def ok(self) -> bool:
        return self.status == OK

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "call_id": self.call_id,
            "status": self.status,
            "ref": self.ref,
            "duration_s": self.duration_s,
            "size_bytes": self.size_bytes,
            "dropped_frames": self.dropped_frames,
            "error": self.error,
        }


class FfmpegEncoder:
    """Streams interleaved stereo PCM16 into ffmpeg and out as Opus.

    Constructed and driven ONLY from the writer thread — every method here may block.
    ``write`` raising is a normal, handled outcome (a full disk raises ENOSPC through
    the pipe, a dead ffmpeg raises BrokenPipeError); the writer turns either into a
    failed recording, never into an exception on the call path.
    """

    BINARY = os.environ.get("VOICE_RECORDING_FFMPEG", "ffmpeg")

    def __init__(self, part_path: Path, sample_rate: int, bitrate: str = OPUS_BITRATE):
        self._proc = subprocess.Popen(
            [
                self.BINARY, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "s16le", "-ar", str(sample_rate), "-ac", "2", "-i", "pipe:0",
                "-c:a", "libopus", "-b:a", bitrate, "-application", "voip",
                # The file is written as ".opus.part" and renamed on success, so ffmpeg
                # cannot infer the container from the extension — name it.
                "-f", "ogg", "-y", str(part_path),
            ],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    @classmethod
    def available(cls) -> bool:
        return shutil.which(cls.BINARY) is not None

    def write(self, data: bytes) -> None:
        self._proc.stdin.write(data)

    def close(self, timeout: float) -> None:
        """Close stdin and wait for the encoder to finish writing the container.

        Raises if ffmpeg exited non-zero — an Opus file whose muxer never finished is
        not a recording, and saying so is the whole point of the sidecar.
        """
        try:
            self._proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
            raise RuntimeError("ffmpeg did not exit within the finish timeout")
        if self._proc.returncode != 0:
            err = b""
            try:
                err = self._proc.stderr.read() or b""
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(
                f"ffmpeg exited {self._proc.returncode}: "
                f"{err.decode('utf-8', 'replace').strip()[:300]}")

    def abort(self) -> None:
        """Kill the encoder without waiting — used when the recording already failed."""
        try:
            if self._proc.poll() is None:
                self._proc.kill()
        except Exception:  # noqa: BLE001
            pass


class CallRecording:
    """One call's capture. Construct with :func:`start`; feed from the live path.

    The public feed methods are the ONLY thing the bridges call inside the audio path,
    and they are deliberately dumb: bounds-checked enqueue, blanket except, return.
    """

    def __init__(self, *, call_id, outlet, direction, fmt, root, encoder_factory,
                 clock=time.monotonic, queue_maxsize=None):
        self.call_id = call_id
        self.outlet = outlet
        self.direction = direction
        self.enabled = True
        self._fmt = fmt
        self._root = Path(root)
        self._encoder_factory = encoder_factory
        self._clock = clock
        self._q: queue.Queue = queue.Queue(
            maxsize=queue_maxsize if queue_maxsize is not None else QUEUE_MAXSIZE)
        self._dropped = 0
        self._t0 = clock()
        self._started_at = datetime.now(timezone.utc)
        self._result = None
        self._finished = False
        safe = _safe_call_id(call_id)
        self._rel = f"{self._started_at:%Y/%m}/{safe}.opus"
        self._path = self._root / self._rel
        self._part = self._path.with_suffix(".opus.part")
        self._sidecar = self._path.with_suffix(".json")
        self._thread = threading.Thread(
            target=self._run, name=f"call-recording-{safe}", daemon=True)
        self._thread.start()

    # -- the live path ----------------------------------------------------------
    #
    # Three methods, all O(1), all non-blocking, none of which can raise into the
    # bridge. Everything below the queue is another thread's problem.

    def caller_audio(self, frame) -> None:
        """One frame of the OTHER party's audio, in the wire's own format.

        ``frame`` may be raw bytes OR the base64 string the transport already carries
        (Twilio media payloads, OpenAI audio deltas). Passing the base64 through keeps
        even the decode off the event loop — the writer thread does it.
        """
        self._offer(_CALLER, frame, None)

    def agent_audio(self, frame, item_id=None) -> None:
        """One frame of OUR agent's audio, in the wire's own format (bytes or base64).

        ``item_id`` marks utterance boundaries so ``agent_truncate`` knows where the
        current utterance started. Any value that compares unequal to the previous one
        opens a new utterance (an OpenAI item id, a cascade mark sequence number).
        """
        self._offer(_AGENT, frame, item_id)

    def agent_truncate(self, played_ms) -> None:
        """A barge-in discarded the un-played tail of the current agent utterance.

        We relay a turn's audio to the transport faster than it plays; on a barge-in the
        transport is told to drop what it has buffered. Without this the recording would
        keep the agent talking over the caller for audio nobody heard.
        """
        self._offer(_TRUNCATE, played_ms, None)

    def _offer(self, kind, payload, item_id) -> None:
        if not self.enabled:
            return
        try:
            self._q.put_nowait((kind, self._clock(), payload, item_id))
        except queue.Full:
            # Deliberate: a stalled writer costs us audio, never the call. Counted so
            # the sidecar can say the recording has holes rather than imply it is whole.
            self._dropped += 1
        except Exception:  # noqa: BLE001
            # Nothing here is allowed to reach the caller's ear. Disable and move on.
            self.enabled = False
            logger.warning("recording enqueue failed for %s — capture disabled for this "
                           "call; the call itself is unaffected", self.call_id,
                           exc_info=True)

    # -- teardown ---------------------------------------------------------------

    def finish(self, timeout: float = None) -> RecordingResult:
        """Stop capture, drain, close the encoder, publish the file. Never raises.

        Called from the bridge's teardown, i.e. after the call is over. Bounded by
        ``timeout`` so a wedged encoder cannot hold up slot reconciliation.
        """
        if self._finished:
            return self._result
        self._finished = True
        self.enabled = False
        deadline = FINISH_TIMEOUT_S if timeout is None else timeout
        try:
            self._q.put_nowait((_STOP, self._clock(), None, None))
        except Exception:  # noqa: BLE001
            pass
        try:
            self._thread.join(timeout=deadline)
        except Exception:  # noqa: BLE001
            pass
        if self._thread.is_alive():
            self._result = RecordingResult(
                call_id=self.call_id, status=FAILED, dropped_frames=self._dropped,
                error="the recording writer did not finish within the timeout")
            logger.error("recording writer for %s did not finish within %.0fs — the "
                         "recording is incomplete (the call was unaffected)",
                         self.call_id, deadline)
            self._write_sidecar(self._result)
        return self._result or RecordingResult(
            call_id=self.call_id, status=FAILED, error="the writer produced no result")

    # -- the writer thread ------------------------------------------------------

    def _run(self) -> None:
        """Owns the encoder, the mix and the disk. Nothing here touches the call."""
        state = _MixState(self._fmt)
        encoder = None
        error = None
        try:
            while True:
                try:
                    kind, ts, payload, item_id = self._q.get(timeout=1.0)
                except queue.Empty:
                    if encoder is not None:
                        # Nothing arriving: flush what the timeline already allows so a
                        # long call does not accumulate in RAM.
                        self._drain(state, encoder, ts=self._clock() - self._t0)
                    continue
                if kind == _STOP:
                    break
                if kind == _TRUNCATE:
                    state.truncate_agent(payload)
                    continue
                if not payload:
                    continue
                if encoder is None:
                    encoder = self._open_encoder()
                state.feed(kind, ts - self._t0, payload, item_id)
                self._drain(state, encoder, ts=ts - self._t0)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("call recording failed for %s — the call itself was "
                             "unaffected", self.call_id)

        self._result = self._close(state, encoder, error)
        self._write_sidecar(self._result)

    def _open_encoder(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        return self._encoder_factory(self._part, self._fmt.sample_rate)

    def _drain(self, state, encoder, ts: float) -> None:
        block = state.take(ts)
        if block:
            encoder.write(block)

    def _close(self, state, encoder, error) -> RecordingResult:
        if encoder is None:
            status = FAILED if error else EMPTY
            return RecordingResult(call_id=self.call_id, status=status,
                                   dropped_frames=self._dropped, error=error)
        if error is None:
            try:
                tail = state.flush_all()
                if tail:
                    encoder.write(tail)
                encoder.close(timeout=FINISH_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                logger.exception("call recording could not be closed for %s — the call "
                                 "itself was unaffected", self.call_id)
        if error is not None:
            encoder.abort()
            self._unlink(self._part)
            return RecordingResult(call_id=self.call_id, status=FAILED,
                                   dropped_frames=self._dropped, error=error)
        try:
            os.replace(self._part, self._path)
            size = self._path.stat().st_size
        except Exception as exc:  # noqa: BLE001
            logger.exception("call recording could not be published for %s",
                             self.call_id)
            self._unlink(self._part)
            return RecordingResult(call_id=self.call_id, status=FAILED,
                                   dropped_frames=self._dropped,
                                   error=f"{type(exc).__name__}: {exc}")
        return RecordingResult(
            call_id=self.call_id, status=OK, ref=self._rel, path=str(self._path),
            duration_s=round(state.written_samples / self._fmt.sample_rate, 2),
            size_bytes=size, dropped_frames=self._dropped)

    def _write_sidecar(self, result) -> None:
        """The dashboard's only source of truth about this recording.

        Written for a FAILURE too: "capture failed, here is why" is what makes the
        failure visible to a human, and it is what stops the screen from showing a
        player for a file that is not there.
        """
        doc = result.to_dict()
        doc.update({
            "outlet": self.outlet,
            "direction": self.direction,
            "started_at": self._started_at.isoformat(),
            "sample_rate": self._fmt.sample_rate,
            "channels": 2,
        })
        try:
            self._sidecar.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._sidecar.with_suffix(".json.part")
            tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._sidecar)
            try:
                os.chmod(self._sidecar, 0o666)
            except OSError:
                pass
        except Exception:  # noqa: BLE001
            # Same multi-uid rendezvous hazard as the eventlog: the bridges and the
            # dashboard are different uids on the same volume. Log, never raise.
            logger.warning("recording sidecar write failed for %s", self.call_id,
                           exc_info=True)

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink()
        except Exception:  # noqa: BLE001
            pass


class NullRecording:
    """What the bridges get when recording is off or could not start.

    Public on purpose: the bridges construct one as the default value of their
    ``recording`` attribute, so the audio loops never need a ``if … is not None`` guard.

    Same surface, all no-ops. The bridges therefore have exactly ONE code path and no
    ``if self._recording is not None`` scattered through the audio loops — which is
    also why an unavailable encoder cannot change how a call behaves.
    """

    enabled = False

    def __init__(self, call_id="", reason=None):
        self.call_id = call_id
        self._reason = reason

    def caller_audio(self, frame) -> None:
        return None

    def agent_audio(self, frame, item_id=None) -> None:
        return None

    def agent_truncate(self, played_ms) -> None:
        return None

    def finish(self, timeout=None) -> RecordingResult:
        return RecordingResult(call_id=self.call_id, status=DISABLED,
                               error=self._reason)


class _MixState:
    """The stereo timeline: caller on the left channel, agent on the right.

    Lives entirely on the writer thread. Samples are counted per leg from t0; a frame is
    placed at ``max(cursor, wall-clock offset)`` and the gap padded with silence, so a
    burst stays contiguous while the START of each burst sits where it was heard.
    """

    def __init__(self, fmt: AudioFormat):
        self._fmt = fmt
        self._rate = fmt.sample_rate
        self._pending = [bytearray(), bytearray()]   # PCM16 not yet interleaved
        self._pos = [0, 0]                           # samples placed, per leg
        self.written_samples = 0                     # samples already interleaved out
        self._agent_item = object()                  # sentinel: no utterance yet
        self._agent_item_start = 0

    def feed(self, leg: int, offset_s: float, frame, item_id) -> None:
        if isinstance(frame, str):
            # The transport's own base64. Decoding here, on the writer thread, is why
            # the live path pays nothing but an enqueue.
            frame = base64.b64decode(frame)
        pcm = self._fmt.decode_pcm16(frame)
        if not pcm:
            return
        target = max(self._pos[leg], int(offset_s * self._rate))
        gap = target - self._pos[leg]
        if gap > 0:
            self._pending[leg].extend(b"\x00\x00" * gap)
            self._pos[leg] = target
        if leg == _AGENT and item_id != self._agent_item:
            self._agent_item = item_id
            self._agent_item_start = self._pos[leg]
        self._pending[leg].extend(pcm)
        self._pos[leg] += len(pcm) // 2

    def truncate_agent(self, played_ms) -> None:
        """Drop the agent tail past ``played_ms`` of the current utterance.

        Bounded by what is still pending: anything already interleaved and handed to the
        encoder is gone, and pretending otherwise would corrupt the mix. In practice the
        agent leg runs ahead of the (real-time) caller leg, which is what holds the flush
        point back, so the tail is nearly always still here.
        """
        try:
            keep = self._agent_item_start + int(float(played_ms) * self._rate / 1000.0)
        except (TypeError, ValueError):
            return
        floor = self._pos[_AGENT] - len(self._pending[_AGENT]) // 2
        keep = max(keep, floor)
        drop = self._pos[_AGENT] - keep
        if drop <= 0:
            return
        del self._pending[_AGENT][-drop * 2:]
        self._pos[_AGENT] = keep

    def take(self, now_offset_s: float) -> bytes:
        """Interleave every sample both legs have reached, and return it.

        A leg that has simply gone quiet (the agent is not speaking, or a transport
        stopped sending) must not pin the timeline, so a leg lagging the clock by more
        than ``MAX_LEG_LEAD_S`` is caught up with silence first.
        """
        cutoff = int((now_offset_s - MAX_LEG_LEAD_S) * self._rate)
        for leg in (_CALLER, _AGENT):
            if self._pos[leg] < cutoff:
                gap = cutoff - self._pos[leg]
                self._pending[leg].extend(b"\x00\x00" * gap)
                self._pos[leg] = cutoff
        n = min(self._pos) - self.written_samples
        if n <= 0:
            return b""
        return self._interleave(n)

    def flush_all(self) -> bytes:
        """Teardown: pad the shorter leg to the longer and interleave everything."""
        end = max(self._pos)
        for leg in (_CALLER, _AGENT):
            gap = end - self._pos[leg]
            if gap > 0:
                self._pending[leg].extend(b"\x00\x00" * gap)
                self._pos[leg] = end
        n = end - self.written_samples
        if n <= 0:
            return b""
        return self._interleave(n)

    def _interleave(self, n: int) -> bytes:
        left = bytes(self._pending[_CALLER][:n * 2])
        right = bytes(self._pending[_AGENT][:n * 2])
        del self._pending[_CALLER][:n * 2]
        del self._pending[_AGENT][:n * 2]
        out = bytearray(n * 4)
        out[0::4] = left[0::2]
        out[1::4] = left[1::2]
        out[2::4] = right[0::2]
        out[3::4] = right[1::2]
        self.written_samples += n
        return bytes(out)


async def finish_async(rec, timeout=None):
    """``rec.finish()`` without blocking the event loop. Never raises.

    ``finish()`` joins the writer thread, which can take up to FINISH_TIMEOUT_S when the
    volume is wedged. On the phone Outlet ONE event loop serves every concurrent call,
    so doing that join on the loop would stall other people's live audio — the exact
    failure this whole module is built to make impossible. Every bridge teardown goes
    through here.
    """
    try:
        return await asyncio.to_thread(rec.finish, timeout)
    except Exception as exc:  # noqa: BLE001
        logger.warning("closing the recording for %s failed",
                       getattr(rec, "call_id", "?"), exc_info=True)
        return RecordingResult(call_id=getattr(rec, "call_id", ""), status=FAILED,
                               error=f"{type(exc).__name__}: {exc}")


def start(*, call_id, outlet, direction, fmt=None, root=None, enabled=None,
          encoder_factory=None, clock=time.monotonic, queue_maxsize=None):
    """Begin capturing one call. NEVER raises, and never returns None.

    A disabled feature, a missing ffmpeg, an unwritable volume — every one of them
    returns a ``NullRecording`` whose feed methods are no-ops, so the bridge's audio
    loop is byte-identical whether or not recording is possible.
    """
    if enabled is None:
        enabled = _enabled_default()
    if not enabled:
        return NullRecording(call_id, "recording disabled (VOICE_RECORDING_ENABLED)")
    factory = encoder_factory or (lambda part, rate: FfmpegEncoder(part, rate))
    try:
        if encoder_factory is None and not FfmpegEncoder.available():
            logger.error("recording is enabled but %r is not on PATH — this call will "
                         "not be recorded (the call itself is unaffected). Install the "
                         "encoder in the image (the bridge Dockerfiles install ffmpeg)",
                         FfmpegEncoder.BINARY)
            return NullRecording(call_id, "the audio encoder is not installed in this "
                                           "image")
        base = Path(root if root is not None else DEFAULT_ROOT)
        base.mkdir(parents=True, exist_ok=True)
        if not os.access(base, os.W_OK):
            raise PermissionError(f"{base} is not writable")
        return CallRecording(
            call_id=call_id, outlet=outlet, direction=direction,
            fmt=fmt or MULAW_8K, root=base, encoder_factory=factory, clock=clock,
            queue_maxsize=queue_maxsize)
    except Exception as exc:  # noqa: BLE001
        logger.error("could not start call recording for %s (%s: %s) — this call will "
                     "not be recorded; the call itself is unaffected",
                     call_id, type(exc).__name__, exc, exc_info=True)
        return NullRecording(call_id, f"{type(exc).__name__}: {exc}")
