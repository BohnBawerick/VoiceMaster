"""Ticket 07 on the Talk Outlet: capture is a tee on the Pulse pipes, never a valve.

Same bar as the phone Outlet's suite. The Talk lane's audio path is two subprocess
pipes — ``parec`` in, ``pacat`` out — so the claims to pin are that capture never sits
between parec and OpenAI, never sits between OpenAI and pacat, and cannot break either
when the disk is gone.
"""
import asyncio
import base64
import json
from pathlib import Path

import pytest

import realtime_bridge
from approval import ApprovalStore
from config import load
from voicecore import recording

REAL_START = recording.start


class CollectingEncoder:
    instances = []

    def __init__(self, part, rate):
        self.part = Path(part)
        self.rate = rate
        self.data = bytearray()
        CollectingEncoder.instances.append(self)

    def write(self, data):
        self.data.extend(data)

    def close(self, timeout):
        self.part.parent.mkdir(parents=True, exist_ok=True)
        self.part.write_bytes(bytes(self.data))

    def abort(self):
        pass


class ExplodingEncoder:
    def __init__(self, part, rate):
        pass

    def write(self, data):
        raise OSError(28, "No space left on device")

    def close(self, timeout):
        raise OSError(28, "No space left on device")

    def abort(self):
        pass


class FakePacat:
    """Stands in for the pacat subprocess: records every PCM byte written to its stdin."""

    class _Stdin:
        def __init__(self):
            self.written = bytearray()

        def write(self, data):
            self.written.extend(data)

        async def drain(self):
            return None

    def __init__(self):
        self.stdin = FakePacat._Stdin()
        self.returncode = None


class FakeParec:
    """Feeds a fixed list of PCM chunks, then EOF."""

    class _Stdout:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        async def read(self, n):
            return self._chunks.pop(0) if self._chunks else b""

    def __init__(self, chunks):
        self.stdout = FakeParec._Stdout(chunks)
        self.returncode = None


class FakeWS:
    def __init__(self):
        self.sent = []
        self.state = type("S", (), {"name": "OPEN"})()

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _bridge(monkeypatch, recorder):
    bridge = realtime_bridge.RealtimeBridge(
        load(), "system prompt", ApprovalStore(),
        token_ctx={"token": "tok07", "caller": "Owner"})
    bridge._recording = recorder
    return bridge


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("VOICE_RECORDING_ENABLED", "true")
    CollectingEncoder.instances = []
    yield
    CollectingEncoder.instances = []


def _capture(tmp_path, encoder, *, factory=None, **over):
    return REAL_START(call_id="tok07", outlet="talk", direction="inbound",
                      fmt=realtime_bridge.cascade_config.PCM_24K, root=tmp_path,
                      enabled=True,
                      encoder_factory=factory or (lambda p, r: encoder(p, r)), **over)


# ------------------------------------------------- the bar that outranks the feature --

@pytest.mark.asyncio
async def test_a_failing_capture_does_not_change_what_reaches_pacat(tmp_path):
    """The agent's voice must reach the Pulse mic sink byte-for-byte, disk or no disk."""
    pcm = (1234).to_bytes(2, "little", signed=True) * 480
    delta = base64.b64encode(pcm).decode()

    async def run(recorder):
        bridge = _bridge(None, recorder)
        bridge._pacat = FakePacat()
        for _ in range(5):
            await bridge._play(delta)
        return bytes(bridge._pacat.stdin.written), bridge._stop_event.is_set()

    baseline = await run(recording.NullRecording())
    broken = await run(_capture(tmp_path, ExplodingEncoder))

    assert broken == baseline
    assert broken[0] == pcm * 5
    assert broken[1] is False, "a failed recording must not tear the call down"


@pytest.mark.asyncio
async def test_a_failing_capture_does_not_change_what_reaches_openai(tmp_path):
    """The caller's voice must reach the model byte-for-byte, disk or no disk."""
    chunks = [(500).to_bytes(2, "little", signed=True) * 480 for _ in range(4)]

    async def run(recorder):
        bridge = _bridge(None, recorder)
        bridge._parec = FakeParec(list(chunks))
        ws = FakeWS()
        await bridge._pump_mic(ws)
        return ws.sent

    baseline = await run(recording.NullRecording())
    broken = await run(_capture(tmp_path, ExplodingEncoder))

    assert broken == baseline
    assert [m["audio"] for m in broken] == [base64.b64encode(c).decode() for c in chunks]


@pytest.mark.asyncio
async def test_a_stalled_capture_does_not_slow_the_pumps(tmp_path):
    """A writer wedged before it ever drains must cost the audio pumps nothing.

    The stall sits in the ENCODER FACTORY so the writer thread never returns to the
    queue: with maxsize 1 the queue is genuinely full for the rest of the call. A stall
    placed further down would be drained past before the queue filled, and this test
    would pass without exercising a full queue at all.
    """
    import time as _time

    def wedged_factory(part, rate):
        _time.sleep(120)

    rec = _capture(tmp_path, None, queue_maxsize=1, factory=wedged_factory)
    bridge = _bridge(None, rec)
    bridge._pacat = FakePacat()
    delta = base64.b64encode(b"\x01\x02" * 480).decode()
    started = _time.monotonic()
    for _ in range(400):
        await bridge._play(delta)
    elapsed = _time.monotonic() - started
    dropped = rec._dropped
    rec.enabled = False
    assert elapsed < 1.0, f"playback waited {elapsed:.1f}s on the recording writer"
    assert dropped > 0, "a full queue must drop, and must say that it dropped"
    assert len(bridge._pacat.stdin.written) == 400 * 960


# --------------------------------------------------------- and it captures the call --

@pytest.mark.asyncio
async def test_both_legs_land_on_their_own_channel(tmp_path):
    rec = _capture(tmp_path, CollectingEncoder)
    bridge = _bridge(None, rec)
    bridge._pacat = FakePacat()
    bridge._parec = FakeParec([(700).to_bytes(2, "little", signed=True) * 480])
    ws = FakeWS()
    await bridge._pump_mic(ws)
    await bridge._play(base64.b64encode(
        (-700).to_bytes(2, "little", signed=True) * 480).decode())
    result = rec.finish(timeout=10)

    assert result.status == recording.OK
    data = CollectingEncoder.instances[0].data
    left = {int.from_bytes(data[i:i + 2], "little", signed=True)
            for i in range(0, len(data), 4)}
    right = {int.from_bytes(data[i + 2:i + 4], "little", signed=True)
             for i in range(0, len(data), 4)}
    assert 700 in left and 700 not in right
    assert -700 in right and -700 not in left


@pytest.mark.asyncio
async def test_the_recording_is_armed_for_the_talk_outlet_when_the_session_opens(
        monkeypatch):
    """The outlet and the audio shape are not guessed at the call site — pin them.

    Arming happens in run(), not __init__, so a bridge that is built and never run
    starts no writer thread.
    """
    seen = {}

    def spy(**kw):
        seen.update(kw)
        return recording.NullRecording(kw.get("call_id", ""))

    monkeypatch.setattr(recording, "start", spy)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def refuse(*a, **kw):
        raise RuntimeError("no network in a unit test")

    monkeypatch.setattr(realtime_bridge.websockets, "connect", refuse)
    bridge = realtime_bridge.RealtimeBridge(
        load(), "prompt", ApprovalStore(), token_ctx={"token": "tokX", "caller": "O"})
    assert seen == {}, "construction must not arm capture"
    await bridge.run()

    assert seen["outlet"] == "talk"
    assert seen["direction"] == "inbound"
    assert seen["call_id"] == "tokX"
    assert seen["fmt"].sample_rate == bridge._cfg.audio_rate


def test_the_pipes_audio_shape_follows_the_configured_rate():
    """24 kHz is the shared constant; another rate must still be recorded at that rate,
    not silently mixed at 24k (which would play back at the wrong speed)."""
    assert realtime_bridge._pcm_format(24000) is realtime_bridge.cascade_config.PCM_24K
    other = realtime_bridge._pcm_format(16000)
    assert other.sample_rate == 16000
    assert other.bytes_per_sample == 2
    assert other.decode_pcm16(b"\x01\x02") == b"\x01\x02"   # linear16 passes through


@pytest.mark.asyncio
async def test_a_barge_in_drops_the_tail_pacat_never_played(tmp_path, monkeypatch):
    """_flush_playback kills pacat to drop its buffer; the recording drops the same tail."""
    calls = []

    class Spy(recording.NullRecording):
        def agent_truncate(self, played_ms):
            calls.append(played_ms)

    bridge = _bridge(None, Spy())
    bridge._current_item_id = "item_9"
    bridge._played_ms = 320.0

    async def no_respawn():
        return None

    monkeypatch.setattr(bridge, "_flush_playback", no_respawn)
    ws = FakeWS()
    await bridge._handle_barge_in(ws)
    assert calls == [320.0]
