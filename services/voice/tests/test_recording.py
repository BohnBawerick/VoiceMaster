"""Ticket 07 — the capture module, tested against the bar that outranks the feature.

The claims pinned here, in the order they matter:

1. A recording failure never reaches the call path (raise / disk-full / dead encoder).
2. A recording STALL never reaches the call path — the feeds stay non-blocking and drop.
3. The mix is what it claims to be: stereo, caller left, agent right, on a wall clock.
4. A barge-in does not leave the agent talking over the caller in the recording.
5. What lands on disk is a real, playable Opus file (run for real when ffmpeg exists).

Every one of these was sabotage-checked when ticket 07 landed: its pull request says which
line was removed and which test went red.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from voicecore import recording  # noqa: E402
from voicecore import recording_store  # noqa: E402
from voicecore.cascade_config import MULAW_8K, PCM_24K  # noqa: E402


# --------------------------------------------------------------------- helpers --

class FakeClock:
    """A monotonic clock the test drives, so timeline assertions are not races."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class CollectingEncoder:
    """Captures the interleaved PCM the writer produces instead of encoding it."""

    instances = []

    def __init__(self, part_path, sample_rate, bitrate=None):
        self.part = Path(part_path)
        self.sample_rate = sample_rate
        self.data = bytearray()
        self.closed = False
        CollectingEncoder.instances.append(self)

    def write(self, data):
        self.data.extend(data)

    def close(self, timeout):
        self.closed = True
        self.part.parent.mkdir(parents=True, exist_ok=True)
        self.part.write_bytes(bytes(self.data))

    def abort(self):
        self.closed = True


def collecting_factory(part, rate):
    return CollectingEncoder(part, rate)


def pcm16_of(recording_obj_data, channel):
    """Split interleaved stereo PCM16 into one channel's samples."""
    out = []
    for i in range(0, len(recording_obj_data), 4):
        lo, hi = recording_obj_data[i + channel * 2], recording_obj_data[i + channel * 2 + 1]
        out.append(int.from_bytes(bytes([lo, hi]), "little", signed=True))
    return out


def ulaw_loud() -> bytes:
    """A 20 ms μ-law frame that decodes to something clearly non-zero."""
    return bytes([0x00] * 160)


def pcm_loud(n_samples: int) -> bytes:
    return (8000).to_bytes(2, "little", signed=True) * n_samples


@pytest.fixture(autouse=True)
def _clean_encoders(monkeypatch):
    # conftest turns capture OFF for every other suite (nothing should write audio to a
    # shared volume by accident). THIS suite is the one that exercises it, so it turns
    # the production default back on.
    monkeypatch.setenv("VOICE_RECORDING_ENABLED", "true")
    CollectingEncoder.instances = []
    yield
    CollectingEncoder.instances = []


# ------------------------------------------------- 1. failures stay off the call --

def test_a_writer_that_always_raises_never_reaches_the_call_path(tmp_path, caplog):
    """The disk is gone, ffmpeg is dead, whatever: the feeds still return normally."""

    class ExplodingEncoder:
        def __init__(self, part, rate):
            pass

        def write(self, data):
            raise OSError(28, "No space left on device")

        def close(self, timeout):
            raise OSError(28, "No space left on device")

        def abort(self):
            pass

    rec = recording.start(call_id="boom", outlet="phone", direction="inbound",
                          fmt=MULAW_8K, root=tmp_path,
                          encoder_factory=lambda p, r: ExplodingEncoder(p, r))
    for _ in range(50):
        rec.caller_audio(ulaw_loud())          # must not raise
        rec.agent_audio(ulaw_loud(), item_id="i1")
    result = rec.finish(timeout=5)

    assert result.status == recording.FAILED
    assert result.ref is None
    assert "No space left" in (result.error or "")
    # And the failure is legible where a human looks: the sidecar the screen reads.
    doc = recording_store.sidecar("boom", base=tmp_path)
    assert doc["status"] == "failed"
    assert "No space left" in doc["error"]
    # Nothing that reads like a recording was left behind.
    assert list(tmp_path.glob("**/*.opus")) == []


def test_an_encoder_that_cannot_even_start_leaves_the_call_alone(tmp_path):
    def refuse(part, rate):
        raise FileNotFoundError("ffmpeg: not found")

    rec = recording.start(call_id="nostart", outlet="talk", direction="outbound",
                          fmt=PCM_24K, root=tmp_path, encoder_factory=refuse)
    for _ in range(10):
        rec.caller_audio(pcm_loud(160))
    result = rec.finish(timeout=5)
    assert result.status == recording.FAILED
    assert "ffmpeg" in result.error


def test_start_never_raises_and_always_returns_a_feedable_object(tmp_path):
    """An unwritable volume is a deploy fault, not a dropped call."""
    blocked = tmp_path / "ro"
    blocked.mkdir()
    os.chmod(blocked, 0o500)
    try:
        rec = recording.start(call_id="ro", outlet="phone", direction="inbound",
                              fmt=MULAW_8K, root=blocked / "nested",
                              encoder_factory=collecting_factory)
        rec.caller_audio(ulaw_loud())
        rec.agent_audio(ulaw_loud())
        rec.agent_truncate(10.0)
        assert rec.finish(timeout=5).status in (recording.DISABLED, recording.FAILED)
    finally:
        os.chmod(blocked, 0o700)


def test_the_feed_methods_swallow_an_internal_failure(tmp_path):
    """Belt and braces: even a broken queue cannot throw into the audio loop."""
    rec = recording.start(call_id="brokenq", outlet="phone", direction="inbound",
                          fmt=MULAW_8K, root=tmp_path,
                          encoder_factory=collecting_factory)

    class Hostile:
        def put_nowait(self, item):
            raise RuntimeError("the queue itself is broken")

    rec._q = Hostile()
    rec.caller_audio(ulaw_loud())      # must not raise
    assert rec.enabled is False        # and it takes itself out of the path
    rec.agent_audio(ulaw_loud())
    rec.finish(timeout=5)


def test_recording_disabled_by_env_is_a_silent_no_op(tmp_path):
    rec = recording.start(call_id="off", outlet="phone", direction="inbound",
                          fmt=MULAW_8K, root=tmp_path, enabled=False)
    rec.caller_audio(ulaw_loud())
    rec.agent_audio(ulaw_loud())
    assert rec.finish().status == recording.DISABLED
    assert list(tmp_path.glob("**/*")) == []


# ------------------------------------------------------- 2. stalls stay off the call --

def test_a_stalled_writer_drops_frames_instead_of_blocking_the_call(tmp_path):
    """The load-bearing test for "capture adds no latency".

    The encoder takes 5 s per write — a wedged NFS mount, a stalled disk. The call
    path pushes 5000 frames through the feed and MUST come back in milliseconds,
    having dropped what did not fit rather than waiting for the writer.
    """
    started = threading.Event()

    class StalledEncoder:
        def __init__(self, part, rate):
            pass

        def write(self, data):
            started.set()
            time.sleep(5.0)

        def close(self, timeout):
            pass

        def abort(self):
            pass

    rec = recording.start(call_id="stall", outlet="phone", direction="inbound",
                          fmt=MULAW_8K, root=tmp_path, queue_maxsize=50,
                          encoder_factory=lambda p, r: StalledEncoder(p, r))
    frame = ulaw_loud()
    t0 = time.monotonic()
    for _ in range(5000):
        rec.caller_audio(frame)
    elapsed = time.monotonic() - t0

    # 5000 feeds against a writer stalled for 5 s a write. If ANY of them waited on
    # the writer this is seconds, not milliseconds.
    assert elapsed < 0.5, f"the feed blocked on the writer for {elapsed:.2f}s"
    assert rec._dropped > 0, "a full queue must drop, and must say that it dropped"
    rec.enabled = False


def test_finish_is_bounded_when_the_writer_will_never_come_back(tmp_path):
    """Teardown must not hold the bridge's slot reconciliation hostage."""

    class WedgedEncoder:
        def __init__(self, part, rate):
            pass

        def write(self, data):
            time.sleep(30)

        def close(self, timeout):
            pass

        def abort(self):
            pass

    rec = recording.start(call_id="wedged", outlet="phone", direction="inbound",
                          fmt=MULAW_8K, root=tmp_path,
                          encoder_factory=lambda p, r: WedgedEncoder(p, r))
    rec.caller_audio(ulaw_loud())
    time.sleep(0.2)
    t0 = time.monotonic()
    result = rec.finish(timeout=1.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0, f"finish() blocked for {elapsed:.1f}s"
    assert result.status == recording.FAILED
    assert "timeout" in (result.error or "")


def test_teardown_does_not_block_the_event_loop(tmp_path):
    """Closing a WEDGED recording must not stall the loop that serves other calls.

    One event loop serves every concurrent call on the phone Outlet, and on the Talk
    Outlet teardown sits in the single-call slot's reconcile path. ``finish()`` joins a
    thread, so calling it ON the loop would freeze live audio for other people — which
    is the exact failure this module exists to make impossible. Hence ``finish_async``,
    and hence this test: another coroutine must keep running throughout.
    """
    class WedgedEncoder:
        def __init__(self, part, rate):
            time.sleep(30)

        def write(self, data):
            pass

        def close(self, timeout):
            pass

        def abort(self):
            pass

    async def drive():
        rec = recording.start(call_id="loopblock", outlet="phone", direction="inbound",
                              fmt=MULAW_8K, root=tmp_path,
                              encoder_factory=lambda p, r: WedgedEncoder(p, r))
        rec.caller_audio(ulaw_loud())
        ticks = []

        async def heartbeat():
            while True:
                await asyncio.sleep(0.02)
                ticks.append(1)

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)                 # let the heartbeat reach its first await
        result = await recording.finish_async(rec, timeout=0.5)
        during = len(ticks)                    # ticks that happened WHILE we waited
        beat.cancel()
        return result, during

    result, ticks = asyncio.run(drive())
    assert result.status == recording.FAILED
    # 0.5 s of waiting at 20 ms a tick. Joining the writer on the loop scores ~0.
    assert ticks >= 10, f"the event loop only ticked {ticks} times during teardown"


# ----------------------------------------------------------------- 3. the mix --

def test_caller_is_left_agent_is_right(tmp_path):
    clock = FakeClock()
    rec = recording.start(call_id="sides", outlet="phone", direction="inbound",
                          fmt=PCM_24K, root=tmp_path, clock=clock,
                          encoder_factory=collecting_factory)
    # 100 samples of caller-only audio, then 100 of agent-only, a second apart.
    rec.caller_audio((1234).to_bytes(2, "little", signed=True) * 100)
    clock.advance(1.0)
    rec.agent_audio((-4321).to_bytes(2, "little", signed=True) * 100, item_id="a")
    clock.advance(1.0)
    result = rec.finish(timeout=10)
    assert result.status == recording.OK

    data = CollectingEncoder.instances[0].data
    left = pcm16_of(data, 0)
    right = pcm16_of(data, 1)
    assert 1234 in left and 1234 not in right
    assert -4321 in right and -4321 not in left
    # The agent's burst starts about a second in, not at zero: the wall clock places it.
    first_agent = next(i for i, v in enumerate(right) if v != 0)
    assert 23000 <= first_agent <= 25000, first_agent


def test_mulaw_is_decoded_not_written_raw(tmp_path):
    """0xFF is μ-law silence and 0x00 is μ-law full scale. If the writer shipped the
    bytes through untouched the recording would be a wall of noise."""
    clock = FakeClock()
    rec = recording.start(call_id="ulaw", outlet="phone", direction="inbound",
                          fmt=MULAW_8K, root=tmp_path, clock=clock,
                          encoder_factory=collecting_factory)
    rec.caller_audio(b"\xff" * 160)     # silence in μ-law
    clock.advance(0.02)
    rec.caller_audio(b"\x00" * 160)     # loud in μ-law
    clock.advance(0.02)
    result = rec.finish(timeout=10)
    assert result.status == recording.OK
    left = pcm16_of(CollectingEncoder.instances[0].data, 0)
    assert max(abs(v) for v in left[:160]) < 100      # first frame really is quiet
    assert max(abs(v) for v in left[160:320]) > 8000  # second really is loud


def test_a_silent_leg_does_not_pin_the_mix_in_memory(tmp_path):
    """The agent says nothing for a minute. The caller's audio must still flow to the
    encoder, padded against silence, not accumulate in RAM until teardown."""
    clock = FakeClock()
    rec = recording.start(call_id="onesided", outlet="phone", direction="inbound",
                          fmt=MULAW_8K, root=tmp_path, clock=clock,
                          encoder_factory=collecting_factory)
    for _ in range(3000):               # 60 s of caller-only audio
        rec.caller_audio(b"\x00" * 160)
        clock.advance(0.02)
    deadline = time.monotonic() + 10
    while not CollectingEncoder.instances and time.monotonic() < deadline:
        time.sleep(0.05)
    assert CollectingEncoder.instances, "the writer never opened an encoder"
    enc = CollectingEncoder.instances[0]
    while time.monotonic() < deadline and len(enc.data) < 4 * 8000 * 50:
        time.sleep(0.05)
    assert len(enc.data) >= 4 * 8000 * 50, (
        "the caller leg was still sitting in memory waiting for the silent agent leg")
    rec.finish(timeout=10)


# ------------------------------------------------------------- 4. barge-in --

def test_barge_in_truncates_the_agent_tail_nobody_heard(tmp_path):
    """The model emits a 4 s turn in one burst; the caller talks over it after 500 ms
    and the transport is told to drop the rest. The recording must drop it too."""
    clock = FakeClock()
    rec = recording.start(call_id="barge", outlet="phone", direction="inbound",
                          fmt=PCM_24K, root=tmp_path, clock=clock,
                          encoder_factory=collecting_factory)
    rec.caller_audio(pcm_loud(2400))            # 100 ms of caller
    clock.advance(0.1)
    rec.agent_audio((5000).to_bytes(2, "little", signed=True) * (24000 * 4), item_id="t1")
    clock.advance(0.5)
    rec.agent_truncate(500.0)                   # only 500 ms was ever played
    clock.advance(0.1)
    result = rec.finish(timeout=10)
    assert result.status == recording.OK

    right = pcm16_of(CollectingEncoder.instances[0].data, 1)
    agent_samples = sum(1 for v in right if v != 0)
    # 500 ms at 24 kHz is 12000 samples. Without the truncate this is 96000.
    assert 11000 <= agent_samples <= 13000, agent_samples


def test_barge_in_never_eats_audio_from_an_earlier_utterance(tmp_path):
    clock = FakeClock()
    rec = recording.start(call_id="barge2", outlet="phone", direction="inbound",
                          fmt=PCM_24K, root=tmp_path, clock=clock,
                          encoder_factory=collecting_factory)
    rec.agent_audio((100).to_bytes(2, "little", signed=True) * 24000, item_id="first")
    clock.advance(1.0)
    rec.agent_audio((200).to_bytes(2, "little", signed=True) * 24000, item_id="second")
    clock.advance(0.1)
    rec.agent_truncate(0.0)     # the second utterance was cut off immediately
    clock.advance(0.1)
    rec.finish(timeout=10)
    right = pcm16_of(CollectingEncoder.instances[0].data, 1)
    assert right.count(100) == 24000, "the first utterance was damaged"
    assert 200 not in right


# ------------------------------------------------- 5. what actually lands on disk --

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_a_real_call_produces_a_real_playable_stereo_opus_file(tmp_path):
    """No fake encoder: the actual ffmpeg pipeline, probed with ffprobe.

    This is the only test that proves the artefact is a file a browser can play.
    """
    clock = FakeClock()
    rec = recording.start(call_id="REAL123", outlet="phone", direction="inbound",
                          fmt=MULAW_8K, root=tmp_path, clock=clock)
    for i in range(250):                      # 5 s of two-way audio
        rec.caller_audio(bytes([(i * 7) % 256] * 160))
        rec.agent_audio(bytes([(i * 11) % 256] * 160), item_id="t1")
        clock.advance(0.02)
    result = rec.finish(timeout=30)

    assert result.status == recording.OK, result.error
    path = tmp_path / result.ref
    assert path.is_file()
    assert result.size_bytes and result.size_bytes < 200_000, (
        "5 s of audio should be kilobytes — Opus is the whole point")

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_name,channels:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True)
    info = json.loads(probe.stdout)
    assert info["streams"][0]["codec_name"] == "opus"
    assert info["streams"][0]["channels"] == 2
    assert 4.0 < float(info["format"]["duration"]) < 6.5

    # And the store finds it by call id, with a sidecar the screen can read.
    assert recording_store.find("REAL123", base=tmp_path) == path
    described = recording_store.describe("REAL123", base=tmp_path)
    assert described["available"] is True
    assert described["url"].endswith("/recording")


def test_a_call_with_no_audio_at_all_is_recorded_as_empty_not_as_a_file(tmp_path):
    rec = recording.start(call_id="silent", outlet="talk", direction="inbound",
                          fmt=PCM_24K, root=tmp_path,
                          encoder_factory=collecting_factory)
    result = rec.finish(timeout=10)
    assert result.status == recording.EMPTY
    assert list(tmp_path.glob("**/*.opus")) == []
    assert recording_store.describe("silent", base=tmp_path)["available"] is False


# ---------------------------------------------------------------- the store --

def test_a_call_id_can_never_walk_out_of_the_recordings_volume(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.opus").write_bytes(b"nope")
    root = tmp_path / "recordings"
    (root / "2026" / "08").mkdir(parents=True)
    for hostile in ("../../outside/secret", "/etc/passwd", "..%2f..%2fsecret",
                    "....//secret"):
        assert recording_store.find(hostile, base=root) is None


def test_the_writer_sanitises_the_call_id_it_was_given(tmp_path):
    rec = recording.start(call_id="../../evil", outlet="phone", direction="inbound",
                          fmt=MULAW_8K, root=tmp_path,
                          encoder_factory=collecting_factory)
    rec.caller_audio(ulaw_loud())
    result = rec.finish(timeout=10)
    assert result.status == recording.OK
    written = (tmp_path / result.ref).resolve()
    assert str(written).startswith(str(tmp_path.resolve()))
    assert written.parent == (tmp_path / Path(result.ref).parent).resolve()
    assert Path(result.ref).parts[-1] == "_.._evil.opus"   # neutered, not a segment


def test_describe_reports_a_failed_capture_rather_than_pretending_it_is_old(tmp_path):
    d = tmp_path / "2026" / "08"
    d.mkdir(parents=True)
    (d / "failedcall.json").write_text(json.dumps(
        {"status": "failed", "error": "OSError: No space left on device"}))
    described = recording_store.describe("failedcall", base=tmp_path)
    assert described["available"] is False
    assert described["status"] == "failed"
    assert "No space left" in described["error"]


def test_describe_says_nothing_at_all_for_a_call_that_predates_recording(tmp_path):
    described = recording_store.describe("ancient", base=tmp_path)
    assert described == {"available": False, "status": None, "error": None,
                         "url": None, "duration_s": None, "size_bytes": None}
