"""ElevenLabs on a live call: Scribe realtime hears, stream-input TTS speaks.

This is the ElevenLabs Agents shape on our own infrastructure: ElevenLabs does the
listening and the speaking, the Agent's Hermes profile is the brain, and the turn-taking
stays ours (``cascade_live`` + ``turn_detect``), so the archive, recordings, summaries,
Missions and last-known-good keep working.

``ScribeLive`` - speech in
--------------------------
``wss://api.elevenlabs.io/v1/speech-to-text/realtime``, model ``scribe_v2_realtime``, fed
the wire's own audio (``ulaw_8000`` from Twilio, ``pcm_24000`` from Talk; both are native
Scribe formats, so nothing is re-encoded). It has the ``DeepgramLive`` surface the engine
already drives: ``start`` / ``feed`` / ``reset`` / ``take_utterance`` / ``close``.

The commit strategy is ``manual``: our VAD proposes the end of a turn and
``take_utterance`` commits, the way ``DeepgramLive`` sends ``Finalize``. Measured on the
owner's recorded caller leg (2026-09-23), a commit came back in 350-420 ms.

Scribe also says when a caller has NOT finished. It writes a trailing ``...`` or ``-`` on
speech that stopped mid-thought ("Um...", "I just...", "are you waiting for-"), and
terminal punctuation on speech that ended. ``turn_verdict`` reads that marker, and it is
the end-of-turn signal for this provider: Smart-Turn v3.2 was measured at 435-660 ms per
verdict on a low-power Celeron NAS CPU (no AVX), too slow to sit in every turn.

Scribe closes a socket that gets no audio for a while ("insufficient_audio_activity"),
and the echo gate withholds caller audio while the agent speaks, so a keepalive task
sends silence whenever nothing was fed for ``keepalive_s``. It also closes the session on
a commit over less than 0.3 s of audio, so every commit is padded to ``MIN_COMMIT_S``.

A session that ends without our asking (a quota, a time limit, a server error) is
reconnected once; the open segment is lost, like a reset. A second loss, or a failed
reconnect, marks the client ``lost``: the call cannot hear any more, and it must not become
a last-known-good. Both are reported through ``on_event`` for the event log.

``TTSStream`` - speech out
--------------------------
``wss://api.elevenlabs.io/v1/text-to-speech/{voice}/stream-input``: ONE socket per
reply. Sentences are sent as Hermes writes them, each with ``flush: true``, and audio
comes back continuously, so a reply no longer pays a new HTTP request and first byte per
sentence (the ~0.6 s gaps that invited the caller in). ``eleven_flash_v2_5`` is the
low-latency model ElevenLabs recommends for this endpoint.

Both connects are injectable (``connect(url, additional_headers=...)``, the
``websockets.connect`` signature) so units drive fake sockets. Keys travel in the
``xi-api-key`` header and are never logged.
"""
import asyncio
import base64
import json
import logging
import time
import urllib.parse

logger = logging.getLogger("voice.elevenlabs")

STT_WS_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
STT_MODEL = "scribe_v2_realtime"
TTS_WS_URL_TMPL = "wss://api.elevenlabs.io/v1/text-to-speech/{voice}/stream-input"
TTS_MODEL = "eleven_flash_v2_5"
# Hermes can go quiet mid-reply while a tool runs; the socket's own default (20 s) would
# close under it. 180 is the documented maximum.
TTS_INACTIVITY_S = 180
# Text was sent and nothing came back for this long: the socket is dead, and the reply
# fails loudly (an apology) instead of waiting forever. Only armed while text is
# outstanding, so a reply that is waiting on Hermes mid-tool is not a stall.
TTS_STALL_S = 10.0
STALL_POLL_S = 0.5

COMMIT_WAIT_S = 1.5
# Scribe closes the session on a commit that covers less than 0.3 s of uncommitted audio
# ("commit_throttled", checked live 2026-09-23). Every commit is padded with silence up to
# this, which does not change the transcript.
MIN_COMMIT_S = 0.35
KEEPALIVE_S = 1.0
CHUNK_S = 0.1                 # Scribe wants 0.1-1 s chunks; 20 ms frames are batched

# Scribe audio_format for each wire format name (cascade_config.AudioFormat.name), with
# the bytes-per-second and the silence byte its keepalive sends.
AUDIO_FORMATS = {
    "mulaw_8k": ("ulaw_8000", 8000, 8000, b"\xff"),
    "pcm_24k": ("pcm_24000", 24000, 48000, b"\x00"),
}

# A committed segment that ends like this was cut off mid-thought.
_TRAILING_OFF = ("...", "…", "-", "–", "—", ",")


def _ws_connect(url, **kwargs):
    # Imported here: the dashboard imports voicecore without the websockets package.
    import websockets
    return websockets.connect(url, **kwargs)


def turn_verdict(text: str) -> "str | None":
    """Scribe's own end-of-turn signal, read off the committed text: "incomplete" when it
    trails off, "complete" when it ends a sentence, None when there is no text."""
    tail = (text or "").strip().rstrip("\"')]")
    if not tail:
        return None
    if tail.endswith(_TRAILING_OFF):
        return "incomplete"
    return "complete"


def stt_url(*, model: str = STT_MODEL, language: "str | None" = None,
            keyterms: "list | None" = None, audio_format: str = "ulaw_8000") -> str:
    params = [("model_id", model or STT_MODEL), ("audio_format", audio_format),
              ("commit_strategy", "manual")]
    if language:
        params.append(("language_code", language))
    for term in (keyterms or []):
        term = (term or "").strip()
        if term:
            params.append(("keyterms", term))
    return f"{STT_WS_URL}?{urllib.parse.urlencode(params)}"


class ScribeLive:
    """Streaming session: ``start()`` -> ``feed()`` frames -> ``take_utterance()`` at the
    end of a turn -> ``close()``. Never raises out of feed/keepalive: a Scribe hiccup
    degrades the turn, not the call."""

    def __init__(self, api_key: str, *, model: str = STT_MODEL,
                 language: "str | None" = None, keyterms: "list | None" = None,
                 wire_format: str = "mulaw_8k", connect=None,
                 keepalive_s: float = KEEPALIVE_S, clock=time.monotonic):
        fmt, rate, bytes_per_s, silence = AUDIO_FORMATS[wire_format]
        self._api_key = api_key
        self._url = stt_url(model=model, language=language, keyterms=keyterms,
                            audio_format=fmt)
        self._rate = rate
        self._chunk_bytes = int(bytes_per_s * CHUNK_S)
        self._silence = silence * self._chunk_bytes
        self._connect = connect or _ws_connect
        self._keepalive_s = keepalive_s
        self._clock = clock
        self._ws = None
        self._recv_task = None
        self._keepalive_task = None
        self._send_lock = asyncio.Lock()
        self._buffer = b""
        self._segments: list = []
        self._commit_event = asyncio.Event()
        self._drop_next_commit = False       # reset(): the next commit carries stale audio
        self._reset_pending = False
        self._sent_since_commit = False
        self._uncommitted = 0                # bytes sent since the last commit
        self._last_audio = 0.0
        self._closed = False
        self._reconnects = 0
        self._last_error: "str | None" = None
        self._send_failed = False
        self.lost = False
        # Set by the engine: called with {"event": "reconnected"|"lost", "reason": ...}.
        self.on_event = None

    async def start(self) -> None:
        self._ws = await self._open()
        self._last_audio = self._clock()
        self._recv_task = asyncio.create_task(self._recv_loop(), name="scribe-recv")
        self._keepalive_task = asyncio.create_task(self._keepalive_loop(),
                                                   name="scribe-keepalive")

    async def _open(self):
        return await self._connect(
            self._url, additional_headers={"xi-api-key": self._api_key})

    async def feed(self, audio: bytes) -> None:
        if self._ws is None or self._closed or not audio:   # not started, reconnecting, lost
            return
        self._last_audio = self._clock()
        async with self._send_lock:
            await self._flush_reset()
            self._buffer += audio
            while len(self._buffer) >= self._chunk_bytes:
                chunk, self._buffer = (self._buffer[:self._chunk_bytes],
                                       self._buffer[self._chunk_bytes:])
                await self._send(chunk, commit=False)

    def reset(self) -> None:
        """Discard the pending utterance (barge-in). The audio already sent is still in
        Scribe's open segment, so the next send commits it first and its transcript is
        dropped on arrival; audio fed after the reset starts a clean segment."""
        self._segments = []
        self._buffer = b""
        self._reset_pending = True

    async def take_utterance(self, *, finalize_wait_s: float = COMMIT_WAIT_S) -> str:
        """Commit + return the caller's utterance at the end of a turn."""
        if self._ws is not None and not self._closed:
            async with self._send_lock:
                await self._flush_reset()
                self._commit_event.clear()
                tail, self._buffer = self._buffer, b""
                pending = self._sent_since_commit or bool(tail)
                if pending:
                    await self._send(tail, commit=True)
            if pending:
                try:
                    await asyncio.wait_for(self._commit_event.wait(),
                                           timeout=finalize_wait_s)
                except asyncio.TimeoutError:
                    logger.warning("Scribe commit not answered in %.1fs", finalize_wait_s)
        text = " ".join(s for s in self._segments if s).strip()
        self._segments = []
        return text

    @staticmethod
    def turn_verdict(text: str) -> "str | None":
        return turn_verdict(text)

    async def close(self) -> None:
        self._closed = True
        for task in (self._keepalive_task, self._recv_task):
            if task is not None:
                task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    # -- internals ------------------------------------------------------------

    async def _flush_reset(self) -> None:
        if self._reset_pending:
            self._reset_pending = False
            if self._sent_since_commit:
                # Only a segment that holds audio is committed and answered; dropping
                # "the next commit" with nothing to commit would drop the caller's.
                self._drop_next_commit = True
                await self._send(b"", commit=True)

    async def _send(self, audio: bytes, *, commit: bool) -> None:
        if self._ws is None:
            return                            # reconnecting, or lost
        self._uncommitted += len(audio)
        if commit:
            short = int(self._chunk_bytes * MIN_COMMIT_S / CHUNK_S) - self._uncommitted
            if short > 0:
                audio += self._silence[:1] * short
            self._uncommitted = 0
        self._sent_since_commit = not commit and (self._sent_since_commit or bool(audio))
        try:
            await self._ws.send(json.dumps({
                "message_type": "input_audio_chunk",
                "audio_base_64": base64.b64encode(audio).decode(),
                "commit": commit, "sample_rate": self._rate}))
            self._send_failed = False
        except Exception:  # noqa: BLE001
            if not self._send_failed:        # once per failure, not once per 100 ms chunk
                self._send_failed = True
                logger.warning("Scribe send failed", exc_info=True)

    async def _recv_loop(self) -> None:
        while True:
            try:
                async for message in self._ws:
                    self._on_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = self._last_error or type(exc).__name__
            if self._closed:
                return
            if not await self._recover():
                return

    async def _recover(self) -> bool:
        """The session ended without our asking. Reconnect once; after that, lost."""
        reason = self._last_error or "the session closed"
        self._last_error = None
        self._ws = None
        self._commit_event.set()              # release a take waiting on this session
        if self._reconnects < 1:
            self._reconnects += 1
            logger.warning("Scribe session ended (%s) - reconnecting once", reason)
            try:
                ws = await self._open()
            except Exception as exc:  # noqa: BLE001
                reason = f"{reason}; reconnect failed: {type(exc).__name__}"
            else:
                async with self._send_lock:
                    self._buffer = b""
                    self._uncommitted = 0
                    self._sent_since_commit = False
                    self._drop_next_commit = False
                    self._reset_pending = False
                    self._ws = ws
                self._emit("reconnected", reason)
                return True
        logger.error("Scribe session lost (%s) - this call can no longer hear the caller",
                     reason)
        self.lost = True
        self._closed = True
        self._emit("lost", reason)
        return False

    def _emit(self, event: str, reason: str) -> None:
        if self.on_event is not None:
            try:
                self.on_event({"event": event, "reason": reason})
            except Exception:  # noqa: BLE001
                logger.warning("STT event callback failed", exc_info=True)

    def _on_message(self, message) -> None:
        try:
            data = json.loads(message)
        except (ValueError, TypeError):
            return
        kind = data.get("message_type")
        if kind == "committed_transcript":
            if self._drop_next_commit:
                self._drop_next_commit = False
                return
            text = (data.get("text") or "").strip()
            if text:
                self._segments.append(text)
            self._commit_event.set()
        elif kind == "warning":
            logger.info("Scribe warning: %s", str(data.get("warning") or data)[:300])
        elif kind not in ("partial_transcript", "session_started",
                          "committed_transcript_with_timestamps"):
            # Every error type arrives as a message before the socket closes. Logged
            # once here, where an operator will look; the key is never in it.
            self._last_error = kind
            logger.error("Scribe %s: %s", kind, str(data.get("error") or data)[:300])

    async def _keepalive_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._keepalive_s / 2)
                if self._clock() - self._last_audio >= self._keepalive_s:
                    self._last_audio = self._clock()
                    async with self._send_lock:
                        await self._send(self._silence, commit=False)
        except asyncio.CancelledError:
            raise


async def _settle(future) -> None:
    """Cancel a read that is still pending and retrieve whatever it ended with."""
    if future is None:
        return
    if not future.done():
        future.cancel()
    try:
        await future
    except (asyncio.CancelledError, StopAsyncIteration, Exception):  # noqa: BLE001
        pass


class TTSStream:
    """One reply's speech: ``open()``, then ``send()`` each sentence and ``finish()``,
    while ``audio()`` yields raw audio in the wire's format until the reply is done.
    ``aclose()`` drops the socket at once (a barge-in)."""

    def __init__(self, *, api_key: str, voice: str, model: "str | None" = None,
                 output_format: str = "ulaw_8000", speed: "float | None" = None,
                 connect=None, clock=time.monotonic):
        params = {"model_id": model or TTS_MODEL, "output_format": output_format,
                  "inactivity_timeout": str(TTS_INACTIVITY_S)}
        self.url = (TTS_WS_URL_TMPL.format(voice=voice) + "?"
                    + urllib.parse.urlencode(params))
        self._api_key = api_key
        self._speed = speed
        self._connect = connect or _ws_connect
        self._clock = clock
        self._ws = None
        self._outstanding_since: "float | None" = None

    async def open(self) -> None:
        self._ws = await self._connect(
            self.url, additional_headers={"xi-api-key": self._api_key})
        first = {"text": " "}
        if self._speed is not None:
            first["voice_settings"] = {"speed": self._speed}
        await self._ws.send(json.dumps(first))

    async def send(self, text: str) -> None:
        self._mark_outstanding()
        await self._ws.send(json.dumps({"text": text.strip() + " ", "flush": True}))

    async def finish(self) -> None:
        self._mark_outstanding()
        await self._ws.send(json.dumps({"text": ""}))

    def _mark_outstanding(self) -> None:
        if self._outstanding_since is None:
            self._outstanding_since = self._clock()

    async def audio(self):
        messages = self._ws.__aiter__()
        pending = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(messages.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=STALL_POLL_S)
                if not done:
                    since = self._outstanding_since
                    if since is not None and self._clock() - since > TTS_STALL_S:
                        raise RuntimeError(
                            f"ElevenLabs TTS sent nothing for {TTS_STALL_S:.0f}s")
                    continue
                finished, pending = pending, None
                try:
                    message = finished.result()
                except StopAsyncIteration:
                    return
                self._outstanding_since = None
                try:
                    data = json.loads(message)
                except (ValueError, TypeError):
                    continue
                if data.get("audio"):
                    yield base64.b64decode(data["audio"])
                elif data.get("isFinal"):
                    return
                elif data.get("error") or data.get("message"):
                    raise RuntimeError(f"ElevenLabs TTS: {str(data)[:200]}")
        finally:
            await _settle(pending)

    async def aclose(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
