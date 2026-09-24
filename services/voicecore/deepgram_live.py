"""Deepgram streaming STT for the LIVE cascade lane (s7 c2, D15).

One ``DeepgramLive`` per call: a websocket to Deepgram's /v1/listen fed the SAME
μ-law/8k bytes Twilio delivers (no re-encode — Deepgram takes ``encoding=mulaw&
sample_rate=8000`` natively), so words stream in while the caller is still talking
and the transcript is ~ready when turn detection fires.

Transcript assembly (Deepgram semantics):
- Interim ``Results`` (``is_final: false``) are DISPLAY-ONLY guesses — never stored.
- ``is_final: true`` results are appended to the current utterance's segment list.
- ``take_utterance()`` (called at turn-end) sends ``Finalize`` to flush Deepgram's
  buffered tail, waits briefly for the flushed final, then returns the joined
  segments and starts a fresh utterance.
- ``reset()`` (barge-in / turn boundary) discards all pending segments AND marks a
  discard point: finals already in flight for pre-reset audio are dropped when they
  arrive (s7 c2 "partials discarded after barge-in").

Keepalive: Deepgram closes a socket that goes ~10s without audio; while the caller
is silent (agent speaking / echo gate withholding) a background task sends
``KeepAlive`` JSON every ``keepalive_s``.

A stream that ends without our asking is reconnected once; a second loss (or a failed
reconnect) marks the client ``lost`` and reports it through ``on_event``, so the call is
known to be deaf and is never recorded as a last-known-good (ticket 21).

The websocket connect is injectable (``connect``) so units drive a fake socket; the
default dials the real endpoint with the registry-resolved model/language and the
``DEEPGRAM_API_KEY`` env key (never logged).
"""
import asyncio
import json
import logging
import time
import urllib.parse

import websockets

logger = logging.getLogger("mode-c.deepgram")

DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"
DEFAULT_MODEL = "nova-3"
KEEPALIVE_S = 5.0
FINALIZE_WAIT_S = 1.5


# Bytes per sample per Deepgram encoding — the audio-sent cursor (below) needs it to
# convert fed bytes into stream-seconds. μ-law is 1 byte/sample; linear16 is 2.
ENCODING_BYTES = {"mulaw": 1, "linear16": 2}


def listen_url(model: str = DEFAULT_MODEL, language: "str | None" = None,
               keyterms: "list | None" = None, *, encoding: str = "mulaw",
               sample_rate: int = 8000, smart_format: "bool | None" = None,
               numerals: "bool | None" = None) -> str:
    params = [
        ("encoding", encoding),
        ("sample_rate", str(sample_rate)),
        ("channels", "1"),
        ("model", model or DEFAULT_MODEL),
        ("interim_results", "true"),
        ("punctuate", "true"),
    ]
    if language:
        params.append(("language", language))
    # VC24: sent only when the Agent sets them, so an Agent that never touched the
    # Listening card dials the byte-identical URL it dialled before.
    if smart_format is not None:
        params.append(("smart_format", "true" if smart_format else "false"))
    if numerals is not None:
        params.append(("numerals", "true" if numerals else "false"))
    # Keyterm prompting (s8 c4, nova-3): one repeatable ``keyterm`` param per proper
    # noun to bias transcription toward it ("Hermes", agent names). Verbatim, ordered.
    for term in (keyterms or []):
        term = (term or "").strip()
        if term:
            params.append(("keyterm", term))
    return f"{DEEPGRAM_WS_URL}?{urllib.parse.urlencode(params)}"


class DeepgramLive:
    """Streaming session: ``start()`` → ``feed()`` frames → ``take_utterance()`` at
    turn-end → ``close()``. Never raises out of feed/keepalive — a Deepgram hiccup
    degrades the turn, not the call."""

    def __init__(self, api_key: str, *, model: str = DEFAULT_MODEL,
                 language: "str | None" = None, keyterms: "list | None" = None,
                 connect=None, keepalive_s: float = KEEPALIVE_S,
                 clock=time.monotonic, encoding: str = "mulaw",
                 sample_rate: int = 8000, smart_format: "bool | None" = None,
                 numerals: "bool | None" = None):
        self._api_key = api_key
        self._url = listen_url(model, language, keyterms,
                               encoding=encoding, sample_rate=sample_rate,
                               smart_format=smart_format, numerals=numerals)
        self._connect = connect or self._real_connect
        self._keepalive_s = keepalive_s
        self._clock = clock
        # Stream-time cursor divisor: bytes-per-second of the fed stream. μ-law/8k =
        # 8000; linear16/24k = 48000. The reset() discard watermark rides on this — a
        # wrong divisor drops/keeps post-barge finals by the rate ratio (s12 c2).
        self._bytes_per_s = sample_rate * ENCODING_BYTES.get(encoding, 1)
        self._ws = None
        self._recv_task = None
        self._keepalive_task = None
        self._segments: list = []            # is_final texts for the current utterance
        self._flush_event = asyncio.Event()  # set on each arriving final
        self._last_audio = 0.0
        self._audio_sent_s = 0.0             # stream-time cursor (bytes fed / 8000)
        self._discard_before_s = 0.0         # reset() cursor — drop finals of older audio
        self._closed = False
        self._reconnects = 0
        self._send_failed = False
        self.lost = False
        # Set by the engine: called with {"event": "reconnected"|"lost", "reason": ...}.
        self.on_event = None

    def _real_connect(self):
        return websockets.connect(
            self._url, additional_headers={"Authorization": f"Token {self._api_key}"})

    async def start(self) -> None:
        self._ws = await self._connect()
        self._last_audio = self._clock()
        self._recv_task = asyncio.create_task(self._recv_loop(), name="deepgram-recv")
        self._keepalive_task = asyncio.create_task(self._keepalive_loop(),
                                                   name="deepgram-keepalive")

    async def feed(self, mulaw_bytes: bytes) -> None:
        """Send raw μ-law audio (already base64-decoded from the Twilio frame)."""
        if self._ws is None or self._closed or not mulaw_bytes:   # reconnecting, or lost
            return
        self._last_audio = self._clock()
        self._audio_sent_s += len(mulaw_bytes) / self._bytes_per_s   # → stream-seconds
        try:
            await self._ws.send(mulaw_bytes)
            self._send_failed = False
        except Exception:  # noqa: BLE001
            if not self._send_failed:        # once per failure, not once per frame
                self._send_failed = True
                logger.warning("Deepgram feed failed", exc_info=True)

    def reset(self) -> None:
        """Discard the pending utterance (turn boundary / barge-in): drops stored
        segments, and in-flight finals for pre-reset audio are dropped on arrival
        (Results carry the stream-time window they transcribe — anything ending at or
        before the reset cursor is stale)."""
        self._segments = []
        self._discard_before_s = self._audio_sent_s

    async def take_utterance(self, *, finalize_wait_s: float = FINALIZE_WAIT_S) -> str:
        """Flush + return the caller's utterance at turn-end; starts the next one."""
        if self._ws is not None and not self._closed:
            self._flush_event.clear()
            try:
                await self._ws.send(json.dumps({"type": "Finalize"}))
                await asyncio.wait_for(self._flush_event.wait(), timeout=finalize_wait_s)
            except asyncio.TimeoutError:
                pass                          # whatever finals we have is the utterance
            except Exception:  # noqa: BLE001
                logger.warning("Deepgram finalize failed", exc_info=True)
        text = " ".join(s for s in self._segments if s).strip()
        self._segments = []
        return text

    async def close(self) -> None:
        self._closed = True
        for task in (self._keepalive_task, self._recv_task):
            if task is not None:
                task.cancel()
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    # -- internals ------------------------------------------------------------

    async def _recv_loop(self) -> None:
        while True:
            reason = "the stream closed"
            try:
                async for message in self._ws:
                    self._on_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                reason = type(exc).__name__
            if self._closed or not await self._recover(reason):
                return

    async def _recover(self, reason: str) -> bool:
        """The stream ended without our asking. Reconnect once (a new stream restarts
        its clock, so the discard cursor restarts too); after that, lost."""
        self._ws = None
        self._flush_event.set()               # release a take waiting on this stream
        if self._reconnects < 1:
            self._reconnects += 1
            logger.warning("Deepgram stream ended (%s) - reconnecting once", reason)
            try:
                ws = await self._connect()
            except Exception as exc:  # noqa: BLE001
                reason = f"{reason}; reconnect failed: {type(exc).__name__}"
            else:
                self._audio_sent_s = 0.0
                self._discard_before_s = 0.0
                self._ws = ws
                self._emit("reconnected", reason)
                return True
        logger.error("Deepgram stream lost (%s) - this call can no longer hear the caller",
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
        if data.get("type") != "Results":
            return
        alt = (((data.get("channel") or {}).get("alternatives")) or [{}])[0]
        text = (alt.get("transcript") or "").strip()
        if not data.get("is_final"):
            return                            # interim — display-only, never stored
        end_s = (data.get("start") or 0.0) + (data.get("duration") or 0.0)
        if end_s and end_s <= self._discard_before_s:
            return                            # final for pre-reset audio — discarded
        if text:
            self._segments.append(text)
        self._flush_event.set()               # empty finals still release Finalize waits

    async def _keepalive_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._keepalive_s / 2)
                if self._ws is not None and \
                        self._clock() - self._last_audio >= self._keepalive_s:
                    try:
                        await self._ws.send(json.dumps({"type": "KeepAlive"}))
                        self._last_audio = self._clock()
                    except Exception:  # noqa: BLE001
                        logger.warning("Deepgram keepalive failed", exc_info=True)
        except asyncio.CancelledError:
            raise
