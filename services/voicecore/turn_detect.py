"""Turn detection for the LIVE cascade lane: an energy VAD over the wire's frames.

``TurnDetector`` is a synchronous, frame-driven state machine (idle, speech, pending).
It owns the energy thresholds, the speech/silence runs, the barge-in trigger and the
echo gate:

- Speech-start needs SUSTAINED energy, and while the agent is audible the bar is
  raised (higher threshold AND a longer run) so line echo or noise cannot self-barge.
- A barge-in is detected in EVERY state while the agent is audible, not only from
  idle. A caller who had already started talking when the agent began (into a filler,
  or into a reply that started over them) can still stop it (report 2026-09-23, 2.3
  mechanism 2).
- While the agent is audible and the caller is idle, frames are NOT forwarded to STT
  (``forward_to_stt=False``): the echo gate. A short pre-buffer of withheld frames is
  kept so a confirmed barge can replay the utterance head into STT.
- A VAD turn-end (``vad_turn_end``) is a PROPOSAL, not a verdict. The engine asks the
  STT provider whether the caller finished (``elevenlabs_live.turn_verdict``) and either
  accepts (``turn_ended()``) or re-arms the listening window (``extend()``: another
  silence run is needed before the next proposal). ``spoke_since_extend`` tells the
  engine whether anything was said inside that window.

Smart-Turn is gone from here. The v2 model lived on a PC that stopped answering, so
every turn paid a 700 ms timeout for nothing; v3.2 in-process was measured at 435-660 ms
per verdict on a low-power Celeron NAS CPU (ticket 21).
"""
import audioop
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("mode-c.turn")

# Twilio Media Streams: G.711 μ-law, 8 kHz, one byte per sample. 20ms frame = 160 B.
SAMPLE_RATE = 8000
BYTES_PER_MS = 8

# Energy (RMS over decoded 16-bit PCM). The raised playing-threshold + longer run is
# the echo gate's first line: far-end echo of our own TTS is quiet relative to real
# caller speech on Twilio's echo-cancelled stream, but not silent.
SPEECH_RMS = 260
PLAYING_RMS_MULT = 3.0
SPEECH_START_MS = 60          # sustained speech to open a turn while agent is silent
# Sustained speech to confirm a barge-in while the agent talks. Dropped 220→140ms
# (2026-07-20) so an interruption registers faster — paired with TTS pacing (Twilio's
# buffer stays shallow), a confirmed barge now stops playback promptly. Still well
# above SPEECH_START_MS + the 3× echo-gate threshold, so line echo can't self-barge.
BARGE_SPEECH_MS = 140
DEFAULT_SILENCE_MS = 600      # VAD silence run that proposes a turn-end
PREBUFFER_MS = 1000           # withheld-audio replay window on a confirmed barge


def frame_rms(mulaw_frame: bytes) -> int:
    """RMS energy of one μ-law frame (decoded to 16-bit PCM)."""
    if not mulaw_frame:
        return 0
    return audioop.rms(audioop.ulaw2lin(mulaw_frame, 2), 2)


def mulaw_to_pcm16(mulaw: bytes) -> bytes:
    return audioop.ulaw2lin(mulaw, 2)


@dataclass
class FrameVerdict:
    """What the orchestrator should do with ONE inbound frame."""
    forward_to_stt: bool
    events: list = field(default_factory=list)   # "speech_start" | "barge_in" | "vad_turn_end"
    replay: bytes = b""                          # withheld pre-buffer, on a confirmed barge


class TurnDetector:
    """Frame-driven VAD state machine. States: idle (awaiting speech), speech,
    pending (a vad_turn_end proposal is out — waiting for turn_ended()/extend())."""

    def __init__(self, *, silence_ms: int = DEFAULT_SILENCE_MS,
                 speech_rms: int = SPEECH_RMS,
                 playing_rms_mult: float = PLAYING_RMS_MULT,
                 speech_start_ms: int = SPEECH_START_MS,
                 barge_speech_ms: int = BARGE_SPEECH_MS,
                 prebuffer_ms: int = PREBUFFER_MS,
                 bytes_per_ms: float = BYTES_PER_MS,
                 decode=mulaw_to_pcm16):
        self.silence_ms = silence_ms
        self.speech_rms = speech_rms
        self.playing_rms_mult = playing_rms_mult
        self.speech_start_ms = speech_start_ms
        self.barge_speech_ms = barge_speech_ms
        self.prebuffer_ms = prebuffer_ms
        # Format seam (s12): 8k μ-law = 8 B/ms decoded via ulaw2lin (mode-c default);
        # 24k linear16 = 48 B/ms with an identity decode (the frame is already PCM16).
        # RMS thresholds are on 16-bit linear samples, so they carry across formats;
        # only the byte→ms conversion and the decode change.
        self.bytes_per_ms = bytes_per_ms
        self._decode = decode
        self.state = "idle"
        self._speech_run_ms = 0.0
        self._silence_run_ms = 0.0
        self._barge_run_ms = 0.0
        self._prebuffer: list = []          # (ms, bytes) withheld while agent speaks
        self._prebuffer_total_ms = 0.0
        self.spoke_since_extend = False

    # -- orchestrator verdict feedback ---------------------------------------

    def turn_ended(self) -> None:
        """The proposal was accepted - reset for the next caller utterance."""
        self.state = "idle"
        self._speech_run_ms = 0.0
        self._silence_run_ms = 0.0

    def extend(self) -> None:
        """The caller is mid-thought: RE-ARM the window. Back to `speech`, silence run
        zeroed - another full silence_ms must elapse before the next vad_turn_end
        proposal."""
        self.state = "speech"
        self._silence_run_ms = 0.0
        self.spoke_since_extend = False

    # -- frame path ----------------------------------------------------------

    def feed(self, mulaw_frame: bytes, *, agent_playing: bool) -> FrameVerdict:
        frame_ms = len(mulaw_frame) / self.bytes_per_ms
        rms = audioop.rms(self._decode(mulaw_frame), 2) if mulaw_frame else 0
        threshold = self.speech_rms * (self.playing_rms_mult if agent_playing else 1.0)
        is_speech = rms >= threshold
        events: list = []
        replay = b""
        # The barge run is counted in every state: while the agent is audible, a
        # sustained raised-threshold run stops it whatever the caller was doing before.
        if agent_playing and is_speech:
            self._barge_run_ms += frame_ms
        else:
            self._barge_run_ms = 0.0

        if self.state == "idle":
            if is_speech:
                self._speech_run_ms += frame_ms
            else:
                self._speech_run_ms = 0.0
            if agent_playing:
                # Echo gate: withhold from STT until a barge is confirmed; keep a
                # bounded pre-buffer so the confirmed barge replays its own head.
                self._prebuffer.append((frame_ms, mulaw_frame))
                self._prebuffer_total_ms += frame_ms
                while self._prebuffer_total_ms > self.prebuffer_ms and self._prebuffer:
                    ms, _ = self._prebuffer.pop(0)
                    self._prebuffer_total_ms -= ms
                if self._barge_run_ms >= self.barge_speech_ms:
                    events += ["speech_start", "barge_in"]
                    replay = b"".join(chunk for _, chunk in self._prebuffer)
                    self._drop_prebuffer()
                    self._barge_run_ms = 0.0
                    self.state = "speech"
                    self._silence_run_ms = 0.0
                return FrameVerdict(forward_to_stt=False, events=events, replay=replay)
            # Agent silent: STT hears everything (the STT needs the utterance head).
            self._drop_prebuffer()
            if self._speech_run_ms >= self.speech_start_ms:
                events.append("speech_start")
                self.state = "speech"
                self._silence_run_ms = 0.0
            return FrameVerdict(forward_to_stt=True, events=events)

        # speech / pending: the caller's audio already flows to STT, so a barge here
        # has nothing to replay.
        if self._barge_run_ms >= self.barge_speech_ms:
            events.append("barge_in")
            self._barge_run_ms = 0.0
        if is_speech:
            self.spoke_since_extend = True
        if self.state == "speech":
            if is_speech:
                self._silence_run_ms = 0.0
            else:
                self._silence_run_ms += frame_ms
                if self._silence_run_ms >= self.silence_ms:
                    events.append("vad_turn_end")
                    self.state = "pending"
            return FrameVerdict(forward_to_stt=True, events=events)

        # pending: proposal out - keep forwarding (trailing audio still belongs to the
        # caller's utterance); renewed speech cancels the proposal locally even before
        # the engine's verdict lands.
        if is_speech:
            self.state = "speech"
            self._silence_run_ms = 0.0
        return FrameVerdict(forward_to_stt=True, events=events)

    def _drop_prebuffer(self) -> None:
        self._prebuffer = []
        self._prebuffer_total_ms = 0.0
