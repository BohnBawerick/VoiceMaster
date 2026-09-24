"""Ticket 07 on the phone Outlet: capture is a tee, never a valve.

The claim this suite exists to pin is the one that outranks the feature: **a recording
that fails, stalls or fills the disk changes NOTHING about the live call.** So the
central test drives a real call through ``media_stream`` twice — once with capture off,
once with a capture whose encoder raises on every write — and asserts that both what
Twilio receives and what OpenAI receives are byte-identical between the two runs.

The rest checks that when capture DOES work, it captured the right two legs.
"""
import asyncio
import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from voicecore import recording

from test_bargein import ScriptedOpenAIWS  # the established mode-c call harness

# ``server.recording`` IS ``voicecore.recording``, so the monkeypatch below replaces
# ``start`` for everyone. Keep the real one here or the factories recurse into the patch.
REAL_START = recording.start


CALLER_FRAME = base64.b64encode(bytes([0x2A] * 160)).decode()
AGENT_DELTA = base64.b64encode(bytes([0x55] * 160)).decode()


class CollectingEncoder:
    """Keeps the interleaved PCM instead of encoding it."""

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
    """A disk that is full, or an ffmpeg that died the moment it was spawned."""

    def __init__(self, part, rate):
        pass

    def write(self, data):
        raise OSError(28, "No space left on device")

    def close(self, timeout):
        raise OSError(28, "No space left on device")

    def abort(self):
        pass


def _drive_a_call(client, monkeypatch, *, capture, media_frames=2, wait_for=None):
    """One inbound call: caller frames in, two agent deltas out, then hang up.

    ``capture`` is a factory returning the recording object ``media_stream`` will use
    (or None for "recording disabled"). Returns what each side of the bridge saw.
    """
    fake = ScriptedOpenAIWS([
        {"type": "response.output_audio.delta", "item_id": "item_1", "delta": AGENT_DELTA},
        {"type": "response.output_audio.delta", "item_id": "item_1", "delta": AGENT_DELTA},
    ])
    monkeypatch.setattr(server.websockets, "connect", lambda *a, **kw: fake)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")
    made = {}
    if capture is None:
        monkeypatch.setattr(server.recording, "start",
                            lambda **kw: recording.NullRecording(kw.get("call_id", "")))
    else:
        def _start(**kw):
            made["rec"] = capture(**kw)
            return made["rec"]
        monkeypatch.setattr(server.recording, "start", _start)

    tok = server._mint_inbound_token()
    received = []
    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZrec",
                      "start": {"streamSid": "MZrec", "callSid": "CAx",
                                "customParameters": {"inbound_token": tok}}})
        received.append(ws.receive_json())
        received.append(ws.receive_json())
        for n in range(media_frames):
            ws.send_json({"event": "media", "sequenceNumber": str(n + 2),
                          "streamSid": "MZrec", "media": {"payload": CALLER_FRAME}})
        ws.send_json({"event": "stop", "sequenceNumber": "99", "streamSid": "MZrec"})
        if wait_for is not None:
            # The bridge's teardown (which closes the recording and writes the call
            # row) runs on the app's own loop once `stop` lands. Poll for it from
            # INSIDE the socket's lifetime — leaving the block first tears the
            # TestClient portal down under it.
            import time as _t
            deadline = _t.monotonic() + 10
            while _t.monotonic() < deadline and not wait_for():
                _t.sleep(0.05)
    return received, fake.sent, made.get("rec")


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("VOICE_RECORDING_ENABLED", "true")
    CollectingEncoder.instances = []
    yield
    CollectingEncoder.instances = []


# ------------------------------------------------- the bar that outranks the feature --

def test_a_failing_recording_changes_nothing_about_the_call(client, monkeypatch, tmp_path):
    """Twice through the SAME call: recording off, then recording failing on every write.

    Twilio's stream and OpenAI's stream must be identical. If capture could ever sit in
    the media path — a blocking write, an exception escaping a feed, a swallowed frame —
    this is where it shows up.
    """
    baseline_twilio, baseline_openai, _ = _drive_a_call(client, monkeypatch, capture=None)

    def failing(**kw):
        return REAL_START(
            call_id=kw["call_id"], outlet=kw["outlet"], direction=kw["direction"],
            fmt=kw["fmt"], root=tmp_path, enabled=True,
            encoder_factory=lambda p, r: ExplodingEncoder(p, r))

    broken_twilio, broken_openai, rec = _drive_a_call(client, monkeypatch, capture=failing)

    assert broken_twilio == baseline_twilio
    assert broken_openai == baseline_openai
    # ...and the failure was not silently lost: it is on the volume for the screen.
    assert rec.finish(timeout=5).status == recording.FAILED


def test_a_stalled_recording_changes_nothing_about_the_call(client, monkeypatch, tmp_path):
    """Same again with a writer wedged before it ever drains — the dead-NFS case.

    The stall is placed in the ENCODER FACTORY, not in a later write: that wedges the
    writer thread on its very first item, so the queue (maxsize 1) is genuinely full for
    the rest of the call. A stall further down would be drained past before the queue
    filled, and the test would pass without ever exercising a full queue.
    """
    import time as _time

    def wedged_factory(part, rate):
        _time.sleep(120)

    # Teardown's own bound, shortened so the test is quick. It is a SEPARATE property
    # from the one under test here (see test_teardown_does_not_block_the_event_loop):
    # this test is about the media loop, which must not wait at all.
    monkeypatch.setattr(recording, "FINISH_TIMEOUT_S", 1.0)
    baseline_twilio, baseline_openai, _ = _drive_a_call(client, monkeypatch, capture=None)

    def wedged(**kw):
        return REAL_START(
            call_id=kw["call_id"], outlet=kw["outlet"], direction=kw["direction"],
            fmt=kw["fmt"], root=tmp_path, enabled=True, queue_maxsize=1,
            encoder_factory=wedged_factory)

    started = _time.monotonic()
    stalled_twilio, stalled_openai, rec = _drive_a_call(
        client, monkeypatch, capture=wedged, media_frames=40)
    elapsed = _time.monotonic() - started

    assert stalled_twilio == baseline_twilio
    assert stalled_openai[:len(baseline_openai)] == baseline_openai
    # 40 caller frames against a writer that will not drain for two minutes. If the
    # enqueue could ever wait on the writer, the whole event loop waits with it.
    assert elapsed < 6.0, f"the call took {elapsed:.1f}s with a wedged recorder"
    assert rec._dropped > 0, "a full queue must drop, and must say that it dropped"
    rec.enabled = False


# --------------------------------------------------------- and it captures the call --

def test_both_legs_of_a_real_bridge_run_land_in_the_recording(client, monkeypatch,
                                                              tmp_path):
    def capturing(**kw):
        return REAL_START(
            call_id=kw["call_id"], outlet=kw["outlet"], direction=kw["direction"],
            fmt=kw["fmt"], root=tmp_path, enabled=True,
            encoder_factory=lambda p, r: CollectingEncoder(p, r))

    _, _, rec = _drive_a_call(client, monkeypatch, capture=capturing)
    result = rec.finish(timeout=10)

    assert result.status == recording.OK
    assert result.ref.endswith("MZrec.opus")
    assert CollectingEncoder.instances, "the writer never opened an encoder"
    data = CollectingEncoder.instances[0].data
    left = {int.from_bytes(data[i:i + 2], "little", signed=True)
            for i in range(0, len(data), 4)}
    right = {int.from_bytes(data[i + 2:i + 4], "little", signed=True)
             for i in range(0, len(data), 4)}
    # 0x2A and 0x55 are distinct μ-law codes; each must appear on ITS OWN channel only.
    caller_value = int.from_bytes(
        server.cascade_config.MULAW_8K.decode_pcm16(bytes([0x2A]))[:2], "little",
        signed=True)
    agent_value = int.from_bytes(
        server.cascade_config.MULAW_8K.decode_pcm16(bytes([0x55]))[:2], "little",
        signed=True)
    assert caller_value in left and caller_value not in right
    assert agent_value in right and agent_value not in left


def test_the_recording_is_armed_for_the_phone_outlet(client, monkeypatch, tmp_path):
    """The outlet and format are not guessed at the call site — pin them."""
    seen = {}

    def capturing(**kw):
        seen.update(kw)
        return recording.NullRecording(kw["call_id"])

    _drive_a_call(client, monkeypatch, capture=capturing)
    assert seen["outlet"] == server.profiles.OUTLET_PHONE
    assert seen["fmt"] is server.cascade_config.MULAW_8K
    assert seen["direction"] == "inbound"
    assert seen["call_id"] == "MZrec"


def test_a_barge_in_tells_the_recording_to_drop_the_tail_twilio_dropped(client,
                                                                       monkeypatch):
    """The bridge already flushes Twilio; the recording must hear about it too."""
    calls = []

    class Spy(recording.NullRecording):
        def agent_truncate(self, played_ms):
            calls.append(played_ms)

    fake = ScriptedOpenAIWS([
        {"type": "response.output_audio.delta", "item_id": "item_1",
         "delta": base64.b64encode(b"\x00" * 80).decode()},
        {"type": "input_audio_buffer.speech_started"},
    ])
    monkeypatch.setattr(server.websockets, "connect", lambda *a, **kw: fake)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(server.recording, "start", lambda **kw: Spy(kw["call_id"]))
    tok = server._mint_inbound_token()
    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZb",
                      "start": {"streamSid": "MZb", "callSid": "CAx",
                                "customParameters": {"inbound_token": tok}}})
        ws.receive_json()   # the relayed delta
        ws.receive_json()   # the clear
        ws.send_json({"event": "stop", "sequenceNumber": "2", "streamSid": "MZb"})
    # 80 μ-law bytes at 8 kHz is 10 ms — the same number the OpenAI truncate carries.
    assert calls == [10.0]


def test_the_event_log_records_what_happened_to_the_audio(client, monkeypatch, tmp_path):
    """A human reading the call log must see that a capture failed, not just its absence."""
    events = []
    monkeypatch.setattr(server.eventlog, "append_event",
                        lambda obj, path=None: events.append(obj))

    def failing(**kw):
        return REAL_START(
            call_id=kw["call_id"], outlet=kw["outlet"], direction=kw["direction"],
            fmt=kw["fmt"], root=tmp_path, enabled=True,
            encoder_factory=lambda p, r: ExplodingEncoder(p, r))

    _drive_a_call(client, monkeypatch, capture=failing,
                  wait_for=lambda: any(e.get("type") == "call" for e in events))
    call_rows = [e for e in events if e.get("type") == "call"]
    assert call_rows, "no per-call event was emitted"
    assert call_rows[-1]["recording_status"] == recording.FAILED
    assert call_rows[-1]["recording_ref"] is None
