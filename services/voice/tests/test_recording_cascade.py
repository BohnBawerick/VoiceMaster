"""Ticket 07 on the cascade lane — one engine, both Outlets.

``cascade_live`` is the pipeline BOTH Outlets run under Advanced, so capture is wired
once, at the wire seam, and inherited by phone and Talk alike. Same bar as the realtime
lanes: what goes on the wire must be identical whether the recording works, fails or
stalls.
"""
import asyncio
import base64
from pathlib import Path

import pytest

from voicecore import recording
from voicecore.cascade_live import CascadeLiveSession

from test_cascade_live import (CONFIG, ENV, FakeDeepgram, FakeRecorder, FakeTwilioWS,
                               make_transport)
from tts_fake import http_tts_connect

REAL_START = recording.start


class CollectingEncoder:
    instances = []

    def __init__(self, part, rate):
        self.part = Path(part)
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


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("VOICE_RECORDING_ENABLED", "true")
    CollectingEncoder.instances = []
    yield
    CollectingEncoder.instances = []


def _session(ws, rec, capture, *, transport):
    return CascadeLiveSession(
        twilio_ws=ws, stream_sid="MZcascade", config=CONFIG, profile=None,
        recorder=rec, env=ENV, stt=FakeDeepgram(),
        detector=None, transport=transport, tts_connect=http_tts_connect(transport), retain_default=False,
        hindsight_url="", recording=capture)


def _capture(tmp_path, encoder, **over):
    return REAL_START(call_id="MZcascade", outlet="phone", direction="outbound",
                      fmt=None, root=tmp_path, enabled=True,
                      encoder_factory=lambda p, r: encoder(p, r), **over)


def test_a_failing_capture_does_not_change_the_frames_on_the_wire(tmp_path):
    """Two identical agent turns, one with capture off and one with capture failing.

    The Twilio envelopes — media payloads and the mark — must match exactly.
    """
    def run(capture):
        ws = FakeTwilioWS()
        transport = make_transport(tts_chunks=[b"\xff" * 320] * 4)
        session = _session(ws, FakeRecorder(), capture, transport=transport)
        asyncio.run(session._agent_turn(opener=True))
        return [o for _, o in ws.sent]

    baseline = run(recording.NullRecording())
    broken = run(_capture(tmp_path, ExplodingEncoder))
    assert broken == baseline
    assert any(o.get("event") == "media" for o in baseline)


def test_the_caller_leg_reaches_the_recording_without_delaying_the_detector(tmp_path):
    """_on_media tees the caller frame and then does exactly what it always did."""
    fed = []

    class Spy(recording.NullRecording):
        def caller_audio(self, frame):
            fed.append(frame)

    ws = FakeTwilioWS()
    session = _session(ws, FakeRecorder(), Spy(), transport=make_transport())
    frame = b"\xff" * 160
    asyncio.run(session._on_media(frame))
    assert fed == [frame]
    assert session._stt.fed, "the STT leg still got the same frame"


def test_both_legs_of_a_cascade_turn_land_on_their_own_channel(tmp_path):
    ws = FakeTwilioWS()
    capture = _capture(tmp_path, CollectingEncoder)
    transport = make_transport(tts_chunks=[bytes([0x55]) * 320] * 4)
    session = _session(ws, FakeRecorder(), capture, transport=transport)

    async def drive():
        await session._on_media(bytes([0x2A]) * 160)   # caller
        await session._agent_turn(opener=True)         # agent speaks

    asyncio.run(drive())
    result = capture.finish(timeout=10)
    assert result.status == recording.OK

    import audioop
    data = CollectingEncoder.instances[0].data
    left = {int.from_bytes(data[i:i + 2], "little", signed=True)
            for i in range(0, len(data), 4)}
    right = {int.from_bytes(data[i + 2:i + 4], "little", signed=True)
             for i in range(0, len(data), 4)}
    caller_v = int.from_bytes(audioop.ulaw2lin(bytes([0x2A]), 2), "little", signed=True)
    agent_v = int.from_bytes(audioop.ulaw2lin(bytes([0x55]), 2), "little", signed=True)
    assert caller_v in left and caller_v not in right
    assert agent_v in right and agent_v not in left


def test_a_barge_in_truncates_the_recording_by_what_was_actually_audible():
    """The engine already computes "how much did they hear" for the LLM history. The
    recording must be truncated by the SAME number, not a second opinion."""
    truncs = []

    class Spy(recording.NullRecording):
        def agent_truncate(self, played_ms):
            truncs.append(played_ms)

    ws = FakeTwilioWS()
    session = _session(ws, FakeRecorder(), Spy(), transport=make_transport())
    session._playing = True
    session._speak_text = "a long reply that got cut off"
    session._burst_sent_ms = 2000.0                 # 2 s handed to the wire...
    session._stream_sent_ms = 2000.0
    session._playout_end = session._clock() + 1.6   # ...of which 1.6 s is still queued

    asyncio.run(session._handle_barge_in())
    assert truncs, "the recording was never told about the barge-in"
    # Sent minus still queued: ~400 ms was heard.
    assert 380 <= truncs[0] <= 460, truncs[0]


def test_teardown_publishes_the_recording_reference_into_the_call_metadata(tmp_path,
                                                                           monkeypatch):
    """The reference is ONE additive field on the metadata ticket 05 owns."""
    retained = {}

    def fake_retain(url, bank, *, content, document_id, metadata, tags, on_result=None,
                    prepare=None):
        retained.update(metadata=metadata, document_id=document_id, tags=tags)

    # Ticket 05 routes every lane's retention through call_record.retain_call, so the
    # seam to intercept is the one IT dispatches on.
    from voicecore import call_record as cr
    monkeypatch.setattr(cr.hindsight, "retain_detached", fake_retain)

    ws = FakeTwilioWS()
    capture = _capture(tmp_path, CollectingEncoder)
    rec = FakeRecorder()
    session = CascadeLiveSession(
        twilio_ws=ws, stream_sid="MZcascade", config=CONFIG, profile=None,
        recorder=rec, env=ENV, stt=FakeDeepgram(),
        detector=None, transport=(tr := make_transport()), tts_connect=http_tts_connect(tr), retain_default=True,
        hindsight_url="http://hindsight.test", recording=capture)
    session.transcript.append("Them: hello")

    async def drive():
        await session._on_media(b"\xff" * 160)
        await session.teardown()

    asyncio.run(drive())
    meta = retained["metadata"]
    assert meta["recording"].endswith("MZcascade.opus")
    # ...and ticket 05's shape is intact around it: this is an ADDITIVE field, not a
    # replacement for the structured metadata that lane already wrote.
    assert meta["platform"] == "voice_cascade"
    assert meta["outlet"] == "phone" and meta["direction"] == "outbound"
    assert meta["outcome"] == "ok" and "duration_s" in meta and "timestamp" in meta
    assert "voice" in retained["tags"] and "cascade" in retained["tags"]
    assert rec.finishes[-1]["recording_status"] == recording.OK
    assert rec.finishes[-1]["recording_ref"].endswith("MZcascade.opus")


def test_a_failed_capture_is_reported_and_leaves_no_dangling_reference(tmp_path,
                                                                       monkeypatch):
    retained = {}

    def fake_retain(url, bank, *, content, document_id, metadata, tags, on_result=None,
                    prepare=None):
        retained.update(metadata=metadata)

    # Ticket 05 routes every lane's retention through call_record.retain_call, so the
    # seam to intercept is the one IT dispatches on.
    from voicecore import call_record as cr
    monkeypatch.setattr(cr.hindsight, "retain_detached", fake_retain)

    capture = _capture(tmp_path, ExplodingEncoder)
    rec = FakeRecorder()
    session = CascadeLiveSession(
        twilio_ws=FakeTwilioWS(), stream_sid="MZcascade", config=CONFIG, profile=None,
        recorder=rec, env=ENV, stt=FakeDeepgram(),
        detector=None, transport=(tr := make_transport()), tts_connect=http_tts_connect(tr), retain_default=True,
        hindsight_url="http://hindsight.test", recording=capture)
    session.transcript.append("Them: hello")

    async def drive():
        await session._on_media(b"\xff" * 160)
        await session.teardown()

    asyncio.run(drive())
    assert "recording" not in retained["metadata"]
    assert rec.finishes[-1]["recording_status"] == recording.FAILED
