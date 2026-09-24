"""Audio device names + PCM framing helpers for the Mode V voice sidecar.

Pure helpers — no import side effects, no subprocess launching. The topology:

- `talk_speaker` is a PulseAudio null sink. Headless Chromium plays the remote
  caller's voice OUT to it (it's the default sink). We RECORD its `.monitor`
  source to capture what the caller said and forward it to OpenAI.
- `talk_mic_sink` is a PulseAudio null sink. We PLAY OpenAI's synthesized
  reply INTO it. A remap-source `talk_mic` (master `talk_mic_sink.monitor`)
  is Chromium's default source (microphone), so the reply is sent into the call.
"""

import base64

SPEAKER_MONITOR = "talk_speaker.monitor"
MIC_SINK = "talk_mic_sink"

# Cap parec/pacat buffering so the null-sink pipes add ~20 ms instead of Pulse's much larger
# default fragment. These are virtual sinks (no hardware to under-run against), so a small
# target latency is safe and trims a slice off both audio legs. Bump if audio ever gets choppy.
_LATENCY_MSEC = "20"


def pcm_to_b64(pcm: bytes) -> str:
    """Encode raw PCM bytes as a base64 string (for OpenAI Realtime JSON frames)."""
    return base64.b64encode(pcm).decode("ascii")


def b64_to_pcm(b64: str) -> bytes:
    """Decode a base64 string back into raw PCM bytes."""
    return base64.b64decode(b64)


def parec_cmd(rate: int) -> list[str]:
    """Argv to RECORD from the caller's audio (talk_speaker.monitor) as s16le mono PCM."""
    return [
        "parec",
        f"--device={SPEAKER_MONITOR}",
        "--format=s16le",
        f"--rate={rate}",
        "--channels=1",
        f"--latency-msec={_LATENCY_MSEC}",
        "--raw",
    ]


def pacat_cmd(rate: int) -> list[str]:
    """Argv to PLAY synthesized reply audio INTO talk_mic_sink as s16le mono PCM."""
    return [
        "pacat",
        "--playback",
        f"--device={MIC_SINK}",
        "--format=s16le",
        f"--rate={rate}",
        "--channels=1",
        f"--latency-msec={_LATENCY_MSEC}",
        "--raw",
    ]
