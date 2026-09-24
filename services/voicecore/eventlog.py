"""Structured voice-call event log (JSONL) — Idea 2.

One JSON object per line, appended to a shared bind-mounted file so a future dashboard can read a
single place. Two record types: a per-call summary emitted on teardown, and a per-turn record with
latency (TTFB), tool-call durations and token usage. The schema is flat/typed like a DB row so it
can be promoted to a Postgres `call_log` table later without changing producers.

Best-effort by construction: append_event NEVER raises into the call path — a broken log must not
drop a live call. `CallRecorder` owns the schema and the per-turn/per-call accounting so both
bridges stay consistent.

Both bridges and voice-control's calllog reader import THIS module — SCHEMA_VERSION and
DEFAULT_PATH included — so the producers and the reader can never disagree.
"""
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("voice.eventlog")

SCHEMA_VERSION = 1

# Default path; overridable via env so tests / local dev don't touch the shared volume.
DEFAULT_PATH = os.environ.get("VOICE_EVENTLOG_PATH", "/app/events/voice_events.jsonl")


def append_event(obj: dict, path: str = DEFAULT_PATH) -> None:
    """Append one event dict as a JSON line. Best-effort — logs and swallows any error.

    ⚠️ The swallow is deliberate (a broken log must never drop a live call) but it is also
    how the Talk lane lost observability SILENTLY for its entire life: this file is shared
    between mode-c (running as root) and the Talk bridge (uid 1000, pwuser). Whichever
    process creates it first owns it, and a root-created 0644 file is unappendable by
    pwuser — so every Talk append raised EACCES and was swallowed. Of 22 historical call
    records, ZERO had mode="talk".

    Hence the chmod on create: the log is a MULTI-UID rendezvous file, so it must be
    group/other writable no matter which process wins the race to create it. Applied only
    when we actually created it — never stomping an operator's deliberate mode on an
    existing file. s14a-2a; the live `chmod 666` that preceded it fixed only the file that
    existed at the time, and would not have survived a delete-and-recreate.
    """
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        if not existed:
            try:
                os.chmod(p, 0o666)
            except OSError:  # not ours to chmod — the append already succeeded
                logger.warning("eventlog created but chmod failed (path=%s)", path)
    except Exception:  # noqa: BLE001
        logger.warning("eventlog append failed (path=%s)", path, exc_info=True)


def _normalize_usage(usage: dict) -> dict:
    """Flatten an OpenAI Realtime response `usage` block to the four fields we track."""
    usage = usage or {}
    itd = usage.get("input_token_details") or {}
    otd = usage.get("output_token_details") or {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "input_audio_tokens": itd.get("audio_tokens"),
        "output_audio_tokens": otd.get("audio_tokens"),
    }


class CallRecorder:
    """Accumulates per-turn latency + token metrics for one call and writes JSONL events.

    The bridges feed it a handful of events; it owns the schema so the two stay consistent.
    `clock` is injectable for tests. Turn boundaries follow the Realtime response cycle: TTFB is
    speech_stopped → first output-audio delta; token usage comes from response.done.
    """

    def __init__(self, *, call_id: str, mode: str, direction: str, caller: str = "",
                 target: str = "", pipeline: str = None, outlet: str = None,
                 path: str = DEFAULT_PATH, clock=time.time):
        self.call_id = call_id
        self.mode = mode                # transport: "twilio" | "talk"
        self.pipeline = pipeline        # "realtime" | "cascade" (None on legacy rows; s12c)
        self.direction = direction      # "inbound" | "outbound"
        # s5 (ticket 05): the Outlet this call travelled on - profiles.OUTLETS, i.e.
        # "phone" | "talk". Each bridge passes ITS OWN outlet, the same constant it
        # resolves its Agent with (s16); it is never derived from mode or direction.
        # None means the writer did not record one, which reads as unknown - never a
        # guess at which outlet it "must have been".
        self.outlet = outlet
        self.caller = caller
        self.target = target
        self._path = path
        self._now = clock
        self.start_ts = self._now()
        self._turn_index = 0
        self._num_turns = 0
        self._num_tool_calls = 0
        self._tokens_total: dict = {}
        # per-turn scratch
        self._speech_stopped_mono = None
        self._ttfb_ms = None
        self._answer_latency_ms = None
        self._turn_tools: list = []

    # -- event hooks (called from the bridge receive loop) -------------------

    def on_speech_stopped(self, mono: float) -> None:
        self._speech_stopped_mono = mono

    def on_audio_delta(self, mono: float) -> None:
        """First output-audio delta after a speech_stopped defines this turn's TTFB.

        TTFB is time-to-first-AUDIBLE: on a tool turn the first frame is the FILLER
        ("one sec, let me check that"), so ttfb measures responsiveness, not backend
        latency. The backend-inclusive metric is answer_latency_ms (on_answer_audio)."""
        if self._speech_stopped_mono is not None and self._ttfb_ms is None:
            self._ttfb_ms = (mono - self._speech_stopped_mono) * 1000.0

    def on_answer_audio(self, mono: float) -> None:
        """First NON-filler (answer) output-audio frame after a speech_stopped defines
        this turn's answer_latency_ms — turn-end verdict → first spoken ANSWER audio,
        spanning the backend/tool round (s12c). Equals ttfb_ms on a no-tool turn (the
        answer IS the first frame); exceeds it whenever a filler played first."""
        if self._speech_stopped_mono is not None and self._answer_latency_ms is None:
            self._answer_latency_ms = (mono - self._speech_stopped_mono) * 1000.0

    def on_tool_call(self, name: str, duration_ms: float, ok: bool = True) -> None:
        self._num_tool_calls += 1
        self._turn_tools.append({"name": name, "duration_ms": round(duration_ms, 1), "ok": ok})

    def on_response_done(self, usage: dict = None, ts: float = None,
                         extra: dict = None) -> None:
        """Emit a per-turn record and fold its tokens into the call total.

        ``extra`` (s7): additive lane-specific fields — the cascade lane records
        per-stage latencies and the Smart-Turn fallback flag here. Still schema 1:
        readers tolerate extra keys; the core fields never move.
        """
        record = {
            "type": "turn",
            "schema": SCHEMA_VERSION,
            "call_id": self.call_id,
            "turn_index": self._turn_index,
            "ts": ts if ts is not None else self._now(),
            "ttfb_ms": round(self._ttfb_ms, 1) if self._ttfb_ms is not None else None,
            "answer_latency_ms": (round(self._answer_latency_ms, 1)
                                  if self._answer_latency_ms is not None else None),
            "tool_calls": self._turn_tools,
            "usage": _normalize_usage(usage),
        }
        if extra:
            for k, v in extra.items():
                record.setdefault(k, v)
        append_event(record, self._path)
        for k, v in _normalize_usage(usage).items():
            if isinstance(v, (int, float)):
                self._tokens_total[k] = self._tokens_total.get(k, 0) + v
        self._turn_index += 1
        self._num_turns += 1
        self._ttfb_ms = None
        self._answer_latency_ms = None
        # Keep _speech_stopped_mono across response.done: a tool turn spans two response cycles
        # (the function-call response, then the spoken result), so measuring from the user's
        # speech_stopped to the first spoken-result audio captures end-to-end latency incl. the
        # backend turn. A genuinely new user turn overwrites the marker via on_speech_stopped.
        self._turn_tools = []

    def elapsed_s(self) -> float:
        """Seconds since this call started, on the injected clock.

        The retain path needs the duration BEFORE ``finish`` runs (the record is built
        while the call is being torn down), so both read it from here and can never
        disagree about how long the same call lasted.
        """
        return self._now() - self.start_ts

    def record_retain(self, *, ok: bool, document_id: str = None, bank: str = None,
                      reason: str = None) -> None:
        """Emit the outcome of this call's archive write (s5, ticket 05).

        Retention is fire-and-forget, so it settles AFTER ``finish`` has already written
        the call record saying "dispatched". Without this line a failed write is visible
        only as a container-log warning, and the call record's ``retain_status`` reads as
        a claim the archive holds this call when it does not. Appended, never rewritten -
        the log is append-only.
        """
        append_event({
            "type": "retain",
            "schema": SCHEMA_VERSION,
            "call_id": self.call_id,
            "document_id": document_id,
            "bank": bank,
            "ok": bool(ok),
            "err": reason,
            "ts": self._now(),
        }, self._path)

    def record_stt(self, *, event: str, reason: str = None, provider: str = None) -> None:
        """The call's STT session was reconnected, or lost for good (ticket 21). A lost
        session means the rest of the call was not heard, which nothing else records."""
        append_event({
            "type": "stt",
            "schema": SCHEMA_VERSION,
            "call_id": self.call_id,
            "provider": provider,
            "event": event,
            "reason": reason,
            "ts": self._now(),
        }, self._path)

    def finish(self, *, outcome: str = "ok", transcript_ref: str = None,
               retain_status: str = None, err: str = None,
               recording_ref: str = None, recording_status: str = None) -> None:
        """Emit the per-call summary record (call this on teardown).

        ``recording_*`` (ticket 07) are the audio capture's outcome, alongside the
        transcript's: ``recording_ref`` is the path under the recordings volume when a
        file landed, and ``recording_status`` is "ok" / "failed" / "empty" / "disabled".
        A failed capture is recorded here on purpose — an operator reading the event log
        after a call must be able to see that the audio did not survive.
        """
        end_ts = self._now()
        append_event({
            "type": "call",
            "schema": SCHEMA_VERSION,
            "call_id": self.call_id,
            "mode": self.mode,
            "pipeline": self.pipeline,
            "outlet": self.outlet,
            "direction": self.direction,
            "caller": self.caller,
            "target": self.target,
            "start_ts": self.start_ts,
            "end_ts": end_ts,
            "duration_s": round(end_ts - self.start_ts, 1),
            "num_turns": self._num_turns,
            "num_tool_calls": self._num_tool_calls,
            "outcome": outcome,
            "transcript_ref": transcript_ref,
            "retain_status": retain_status,
            "recording_ref": recording_ref,
            "recording_status": recording_status,
            "tokens_total": self._tokens_total,
            "err": err,
        }, self._path)
