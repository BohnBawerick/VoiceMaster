"""s12a — the cascade turn engine is rate-parameterized: μ-law/8k (mode-c) behaviour
is byte-unchanged, and a linear16/24k (Talk) construction computes frame_ms, RMS,
the Deepgram encoding+cursor, and the TTS output_format+frame size correctly.

Every assertion drives the REAL TurnDetector / DeepgramLive / CascadeLiveSession._speak
over the shared cascade_config.AudioFormat descriptors — not a stub that would pass while
the actual rate math is 6× off.
"""
import asyncio
import audioop
import base64
import time

import httpx
import pytest

from voicecore import cascade_config
from voicecore import deepgram_live
from voicecore.cascade_config import MULAW_8K, PCM_24K
from voicecore.cascade_live import CascadeLiveSession
from voicecore.deepgram_live import DeepgramLive, listen_url
from voicecore.turn_detect import TurnDetector

# reuse the mode-c session harness
from test_cascade_live import (CONFIG, ENV, FakeDeepgram, FakeRecorder,
                               FakeTwilioWS, SlowStream)
from tts_fake import http_tts_connect


# ── AudioFormat descriptor -------------------------------------------------

def test_audio_format_derived_constants():
    assert MULAW_8K.bytes_per_ms == 8.0 and MULAW_8K.frame_bytes == 160
    assert MULAW_8K.frame_period_s == 0.02
    assert PCM_24K.bytes_per_ms == 48.0 and PCM_24K.frame_bytes == 960
    assert PCM_24K.frame_period_s == 0.02
    # decode: μ-law is expanded to linear16; linear16 passes through untouched.
    pcm = (5000).to_bytes(2, "little", signed=True) * 160
    assert PCM_24K.decode_pcm16(pcm) == pcm
    assert MULAW_8K.decode_pcm16(audioop.lin2ulaw(pcm, 2)) == audioop.ulaw2lin(
        audioop.lin2ulaw(pcm, 2), 2)


# ── c1: TurnDetector rate-parameterized -------------------------------------

def _pcm24_frame(amplitude: int) -> bytes:
    """One 20ms linear16/24k frame (960 bytes = 480 samples) of a constant level."""
    return amplitude.to_bytes(2, "little", signed=True) * 480


def test_turndetector_24k_byte_to_ms_conversion():
    """A 960B PCM24k frame is 20ms — so speech_start (60ms) fires after exactly 3
    frames. If the detector used the μ-law 8 B/ms constant it would read each 960B
    frame as 120ms and fire after ONE frame (the 6×-early bug)."""
    det = TurnDetector(bytes_per_ms=PCM_24K.bytes_per_ms, decode=PCM_24K.decode_pcm16)
    loud = _pcm24_frame(9000)
    v1 = det.feed(loud, agent_playing=False)
    v2 = det.feed(loud, agent_playing=False)
    assert "speech_start" not in v1.events and "speech_start" not in v2.events
    v3 = det.feed(loud, agent_playing=False)          # 3 × 20ms = 60ms
    assert "speech_start" in v3.events


def test_turndetector_24k_rms_on_linear16_not_ulaw():
    """RMS is computed on the linear16 samples directly (identity decode). A loud PCM
    tone must register as speech; silence must not. A μ-law decode of raw PCM would
    garbage the energy."""
    det = TurnDetector(bytes_per_ms=PCM_24K.bytes_per_ms, decode=PCM_24K.decode_pcm16)
    # silence never opens a turn, however many frames
    for _ in range(10):
        assert "speech_start" not in det.feed(_pcm24_frame(0),
                                              agent_playing=False).events
    # the identity decode yields the true amplitude, well above SPEECH_RMS (260)
    assert audioop.rms(PCM_24K.decode_pcm16(_pcm24_frame(5000)), 2) == 5000


def test_turndetector_mulaw_default_unchanged():
    """Default construction is mode-c μ-law/8k: 160B = 20ms, ulaw2lin RMS."""
    det = TurnDetector()
    assert det.bytes_per_ms == 8
    loud = audioop.lin2ulaw((9000).to_bytes(2, "little", signed=True) * 160, 2)
    v1 = det.feed(loud, agent_playing=False)
    v2 = det.feed(loud, agent_playing=False)
    v3 = det.feed(loud, agent_playing=False)          # 3 × 20ms = 60ms
    assert "speech_start" not in v1.events and "speech_start" not in v2.events
    assert "speech_start" in v3.events


# ── c2: DeepgramLive encoding + rate + cursor -------------------------------

def test_listen_url_encoding_and_rate():
    mode_c = listen_url()
    assert "encoding=mulaw" in mode_c and "sample_rate=8000" in mode_c
    talk = listen_url(encoding="linear16", sample_rate=24000)
    assert "encoding=linear16" in talk and "sample_rate=24000" in talk


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, b):
        self.sent.append(b)


def test_deepgram_cursor_divisor_24k():
    """The audio-sent cursor is stream-seconds: 96000 bytes of linear16/24k = 2.0s
    (bytes ÷ 48000). Mode-c (μ-law/8k) divides by 8000. A wrong divisor mis-times the
    post-barge discard watermark by the rate ratio."""
    talk = DeepgramLive("k", encoding="linear16", sample_rate=24000)
    talk._ws = _FakeWS()
    asyncio.run(talk.feed(b"\x00" * 96000))
    assert talk._audio_sent_s == pytest.approx(2.0)

    mode_c = DeepgramLive("k")
    mode_c._ws = _FakeWS()
    asyncio.run(mode_c.feed(b"\x00" * 8000))
    assert mode_c._audio_sent_s == pytest.approx(1.0)


def test_deepgram_post_barge_discard_at_24k():
    """After a barge at stream-time T, a final whose window ends ≤ T is discarded and
    one after is kept — with the 24k cursor driving T."""
    dg = DeepgramLive("k", encoding="linear16", sample_rate=24000)
    dg._ws = _FakeWS()
    asyncio.run(dg.feed(b"\x00" * 96000))             # cursor → 2.0s
    dg.reset()                                        # discard_before = 2.0s
    assert dg._discard_before_s == pytest.approx(2.0)

    def _results(text, start, duration):
        import json
        return json.dumps({"type": "Results", "is_final": True,
                           "start": start, "duration": duration,
                           "channel": {"alternatives": [{"transcript": text}]}})

    dg._on_message(_results("stale", 1.0, 0.5))       # ends 1.5 ≤ 2.0 → discarded
    dg._on_message(_results("fresh", 2.0, 0.5))       # ends 2.5 > 2.0 → kept
    assert dg._segments == ["fresh"]


# ── c3: TTS output_format + frame math from config --------------------------

def _tts_capturing_transport(record_urls, *, pcm_bytes):
    """MockTransport that records the TTS request URL (query carries output_format)
    and streams ``pcm_bytes`` back as the synthesized audio body."""
    def handler(request):
        if request.url.host == "api.elevenlabs.io":
            record_urls.append(str(request.url))
            return httpx.Response(200, stream=SlowStream([pcm_bytes]))
        raise AssertionError(f"unexpected host {request.url.host}")
    return httpx.MockTransport(handler)


def _speak_session(ws, rec, *, transport, audio_format):
    return CascadeLiveSession(
        twilio_ws=ws, stream_sid="MZ24k", config=CONFIG, profile=None,
        recorder=rec, env=ENV, stt=FakeDeepgram(),
        transport=transport, tts_connect=http_tts_connect(transport), audio_format=audio_format)


def test_speak_pcm24k_output_format_and_frame_size():
    """A PCM_24K session asks ElevenLabs for output_format=pcm_24000 (NOT ulaw_8000)
    and re-frames the body into 960-byte (20ms @ 24k) media frames."""
    urls: list = []
    ws = FakeTwilioWS()
    transport = _tts_capturing_transport(urls, pcm_bytes=b"\x01\x02" * (960 * 3 // 2))
    session = _speak_session(ws, FakeRecorder(), transport=transport,
                             audio_format=PCM_24K)
    asyncio.run(session._speak("hello"))

    assert urls and "output_format=pcm_24000" in urls[0]
    assert "ulaw_8000" not in urls[0]
    payloads = [base64.b64decode(o["media"]["payload"])
                for _, o in ws.events("media")]
    assert payloads, "no media frames sent"
    assert all(len(p) == 960 for p in payloads[:-1])   # full frames are 20ms @ 24k


def test_speak_mulaw_default_output_format_unchanged():
    """Mode-c default stays ulaw_8000 / 160-byte frames."""
    urls: list = []
    ws = FakeTwilioWS()
    transport = _tts_capturing_transport(urls, pcm_bytes=b"\xff" * (160 * 3))
    session = _speak_session(ws, FakeRecorder(), transport=transport,
                             audio_format=MULAW_8K)
    asyncio.run(session._speak("hello"))

    assert urls and "output_format=ulaw_8000" in urls[0]
    payloads = [base64.b64decode(o["media"]["payload"])
                for _, o in ws.events("media")]
    assert all(len(p) == 160 for p in payloads[:-1])
