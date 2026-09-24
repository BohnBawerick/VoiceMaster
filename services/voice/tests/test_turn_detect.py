"""s7 c3/c5 units — VAD state machine, barge-in trigger, echo gate."""
import asyncio
import audioop
import json

import httpx
import pytest

from voicecore import turn_detect
from voicecore.turn_detect import FrameVerdict, TurnDetector

FRAME_MS = 20
FRAME_BYTES = FRAME_MS * turn_detect.BYTES_PER_MS


def _mulaw_frame(amplitude: int) -> bytes:
    """One 20ms μ-law frame of a constant-amplitude square-ish wave."""
    pcm = (amplitude.to_bytes(2, "little", signed=True) * (FRAME_BYTES))
    return audioop.lin2ulaw(pcm[:FRAME_BYTES * 2], 2)


LOUD = _mulaw_frame(9000)        # caller speech (rms >> raised threshold)
MEDIUM = _mulaw_frame(400)       # above the base threshold, below the raised one
QUIET = _mulaw_frame(30)         # silence/noise floor


def _feed(det, frame, n, *, playing=False):
    out = []
    for _ in range(n):
        out.append(det.feed(frame, agent_playing=playing))
    return out


def _events(verdicts):
    return [e for v in verdicts for e in v.events]


# -- speech start / turn-end proposal ----------------------------------------

def test_sustained_speech_opens_a_turn_and_single_frame_does_not():
    det = TurnDetector()
    assert _events(_feed(det, LOUD, 1)) == []                  # 20ms — not sustained
    det2 = TurnDetector()
    assert "speech_start" in _events(_feed(det2, LOUD, 3))     # 60ms sustained


def test_vad_silence_run_proposes_turn_end():
    det = TurnDetector(silence_ms=100)
    _feed(det, LOUD, 3)
    verdicts = _feed(det, QUIET, 5)                            # 100ms silence
    assert "vad_turn_end" in _events(verdicts)
    assert det.state == "pending"
    assert all(v.forward_to_stt for v in verdicts)             # trailing audio kept


def test_extend_rearms_a_full_silence_window_not_a_sleep():
    det = TurnDetector(silence_ms=100)
    _feed(det, LOUD, 3)
    _feed(det, QUIET, 5)                                       # proposal #1
    det.extend()                                               # STT: caller not finished
    assert det.state == "speech"
    assert "vad_turn_end" not in _events(_feed(det, QUIET, 4))  # 80ms — window re-armed
    assert "vad_turn_end" in _events(_feed(det, QUIET, 1))      # full 100ms again


def test_turn_ended_resets_for_the_next_utterance():
    det = TurnDetector(silence_ms=100)
    _feed(det, LOUD, 3)
    _feed(det, QUIET, 5)
    det.turn_ended()
    assert det.state == "idle"
    assert "speech_start" in _events(_feed(det, LOUD, 3))


def test_renewed_speech_while_pending_returns_to_speech_state():
    det = TurnDetector(silence_ms=100)
    _feed(det, LOUD, 3)
    _feed(det, QUIET, 5)
    _feed(det, LOUD, 1)
    assert det.state == "speech"


# -- echo gate + barge-in (c5) ------------------------------------------------

def test_agent_playing_withholds_frames_from_stt():
    det = TurnDetector()
    verdicts = _feed(det, QUIET, 5, playing=True)
    assert all(v.forward_to_stt is False for v in verdicts)
    assert all(v.forward_to_stt for v in _feed(det, QUIET, 2, playing=False))


def test_echo_level_speech_does_not_self_barge_while_agent_talks():
    """MEDIUM clears the base threshold (it would open a turn when the agent is
    silent) but NOT the raised playing threshold — line echo can't barge."""
    det = TurnDetector()
    assert "speech_start" in _events(_feed(det, MEDIUM, 3))
    det2 = TurnDetector()
    assert _events(_feed(det2, MEDIUM, 30, playing=True)) == []


def test_real_speech_barges_after_sustained_run_and_replays_prebuffer():
    det = TurnDetector(barge_speech_ms=100, prebuffer_ms=200)
    _feed(det, QUIET, 3, playing=True)                # withheld noise-floor audio
    verdicts = _feed(det, LOUD, 5, playing=True)      # 100ms sustained real speech
    events = _events(verdicts)
    assert "barge_in" in events and "speech_start" in events
    replay = next(v.replay for v in verdicts if "barge_in" in v.events)
    assert len(replay) >= 4 * FRAME_BYTES             # utterance head + some prebuffer
    assert det.state == "speech"


def test_prebuffer_is_bounded():
    det = TurnDetector(prebuffer_ms=100)
    _feed(det, QUIET, 50, playing=True)               # 1s withheld ≫ 100ms cap
    assert det._prebuffer_total_ms <= 100


def test_cascade_vad_knob_read_does_not_touch_realtime_provider():
    """s7 regression (live-caught): the cascade lane reads knobs.vad.silence_ms
    directly from the doc — ActiveProfile.resolve() walks providers.realtime, which a
    cascade profile has none of (KeyError crashed the first live call)."""
    from voicecore import profiles
    doc = {"id": "casc", "pipeline": "cascade",
           "providers": {"stt": "deepgram", "llm": "openrouter", "tts": "elevenlabs"},
           "knobs": {"vad": {"silence_ms": 700}}}
    prof = profiles.ActiveProfile(agent_id="casc", source="<t>", doc=doc, registry={})
    vad = (prof.doc.get("knobs") or {}).get("vad") or {}
    assert vad.get("silence_ms") == 700          # direct read works
    import pytest as _pt
    with _pt.raises(KeyError):
        _ = prof.realtime_provider               # confirms why resolve() would crash
