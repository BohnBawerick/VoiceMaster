"""Tests for Idea 4 - voice transcripts retained into Hindsight memory (Mode C).

Covers the retain client contract (path-segment endpoint, body shape, best-effort) and that an
INBOUND call now captures its transcript (was outbound-only) and dispatches a retain on teardown.

s5 (ticket 05) adds the two bars this ticket cannot ship without:

* **the dedicated bank** - calls go to `voice`, this app's own archive, not the shared `hermes`
  gateway-session bank;
* **fire-and-forget** - a store that hangs or fails must not wedge, delay or drop a live call,
  and the failure must be recorded rather than swallowed.
"""
import asyncio
import json
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from voicecore import call_record
from voicecore import eventlog
from voicecore import hindsight
import server
from test_bargein import ScriptedOpenAIWS   # reuse the scripted fake


ENDPOINT = "http://hindsight:8888/v1/default/banks/hermes/memories"
VOICE_ENDPOINT = "http://hindsight:8888/v1/default/banks/voice/memories"


@pytest.mark.asyncio
@respx.mock
async def test_retain_posts_correct_shape():
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json={}))
    ok = await hindsight.retain(
        "http://hindsight:8888", "hermes",
        content="Them: hi\nAI: hello", document_id="voice-twilio-x",
        metadata={"platform": "voice_twilio", "direction": "inbound"},
        tags=["voice", "twilio", "inbound"])
    assert ok is True and route.called
    body = route.calls.last.request.read()
    import json
    payload = json.loads(body)
    assert payload["async"] is True
    item = payload["items"][0]
    assert item["document_id"] == "voice-twilio-x"
    assert item["content"] == "Them: hi\nAI: hello"
    assert item["metadata"] == {"platform": "voice_twilio", "direction": "inbound"}   # all str
    assert item["tags"] == ["voice", "twilio", "inbound"]


@pytest.mark.asyncio
async def test_retain_empty_content_is_noop():
    assert await hindsight.retain("http://x", "hermes", content="   ", document_id="d") is False


@pytest.mark.asyncio
@respx.mock
async def test_retain_failure_returns_false_not_raises():
    respx.post(ENDPOINT).mock(return_value=httpx.Response(500, text="boom"))
    assert await hindsight.retain("http://hindsight:8888", "hermes",
                                  content="x", document_id="d") is False


# -- integration: inbound call now captures + retains ------------------------------------

@pytest.fixture
def client():
    return TestClient(server.app)


def test_inbound_call_captures_and_retains(client, monkeypatch):
    # The Hindsight archive, named explicitly: an unset HINDSIGHT_URL means the SQLite one.
    monkeypatch.setattr(server, "HINDSIGHT_URL", "http://hindsight:8888")
    calls = []
    monkeypatch.setattr(server.hindsight, "retain_detached",
                        lambda url, bank, **kw: calls.append((url, bank, kw)) or True)
    monkeypatch.setattr(server.eventlog, "append_event", lambda *a, **k: None)

    fake = ScriptedOpenAIWS([
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "hello robot"},
        {"type": "response.output_audio_transcript.done", "transcript": "hi there"},
    ])
    monkeypatch.setattr(server.websockets, "connect", lambda *a, **kw: fake)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")
    tok = server._mint_inbound_token()

    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZmem",
                      "start": {"streamSid": "MZmem", "callSid": "CAx",
                                "customParameters": {"inbound_token": tok}}})
        ws.send_json({"event": "stop", "sequenceNumber": "2", "streamSid": "MZmem"})

    assert len(calls) == 1
    _url, bank, kw = calls[0]
    # s5 (ticket 05): the call archive is its own bank, not the shared `hermes` one.
    assert bank == "voice"
    assert "Them: hello robot" in kw["content"] and "AI: hi there" in kw["content"]
    assert kw["document_id"].startswith("voice-twilio-")
    assert kw["metadata"]["platform"] == "voice_twilio"
    assert kw["metadata"]["direction"] == "inbound"
    # s5: the Outlet is RECORDED - the constant this bridge resolves its Agent with -
    # never derived from the direction or from "it came in over Twilio so it must be".
    assert kw["metadata"]["outlet"] == "phone"
    assert kw["metadata"]["outcome"] == "ok"
    assert float(kw["metadata"]["duration_s"]) >= 0.0
    # An inbound call has no Mission, so it records none. Nothing invents one.
    assert "mission" not in kw["metadata"]
    assert "voice" in kw["tags"] and "inbound" in kw["tags"] and "phone" in kw["tags"]


# -- s5 (ticket 05): the archive is a by-product; the call is the product -----------------
#
# The bar this ticket cannot ship without: a slow or unreachable store must never wedge or
# drop a live call. Each test below makes the store misbehave in a DIFFERENT way and asserts
# the call was unaffected -- not that "retain returned False", which proves only that the
# client swallowed something.


class HangingHindsight:
    """A Hindsight that accepts the request and never answers.

    The realistic shape of the failure this ticket has to survive: not a refused
    connection (fast), but a store that holds the socket open. A retain that awaited
    this would hold call teardown for the whole 30s client timeout.
    """

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def retain_result(self, url, bank, **kwargs):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return False, "released"


def test_a_hanging_store_does_not_delay_teardown_or_drop_the_call(client, monkeypatch):
    """The whole bar, end to end: the store hangs, the call still completes normally.

    Asserted from what the CALL produced -- the websocket ran to its `stop` event and the
    per-call event record was written -- not from the retain client's return value.
    """
    hanging = HangingHindsight()
    monkeypatch.setattr(hindsight, "retain_result", hanging.retain_result)
    # The Hindsight archive, named explicitly: an unset HINDSIGHT_URL means the SQLite one.
    monkeypatch.setattr(server, "HINDSIGHT_URL", "http://hindsight:8888")

    events = []
    monkeypatch.setattr(server.eventlog, "append_event",
                        lambda obj, path=None: events.append(obj))

    fake = ScriptedOpenAIWS([
        {"type": "conversation.item.input_audio_transcription.completed",
         "transcript": "hello robot"},
        {"type": "response.output_audio_transcript.done", "transcript": "hi there"},
    ])
    monkeypatch.setattr(server.websockets, "connect", lambda *a, **kw: fake)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")
    tok = server._mint_inbound_token()

    started = time.monotonic()
    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZhang",
                      "start": {"streamSid": "MZhang", "callSid": "CAhang",
                                "customParameters": {"inbound_token": tok}}})
        ws.send_json({"event": "stop", "sequenceNumber": "2", "streamSid": "MZhang"})
    elapsed = time.monotonic() - started

    # The call finished, and it finished NOW -- nowhere near the 30s retain timeout.
    assert elapsed < 5.0, f"teardown waited {elapsed:.1f}s on a hanging store"
    call_records = [e for e in events if e.get("type") == "call"]
    assert len(call_records) == 1, "the call record was not written"
    assert call_records[0]["call_id"] == "MZhang"
    assert call_records[0]["outcome"] == "ok"
    # And the retain really was in flight while all that happened.
    assert hanging.calls == 1


@respx.mock
def test_a_failing_store_leaves_the_call_alone_and_says_so_in_the_log(tmp_path):
    """A store that ERRORS: the call record still lands, and the failure is recorded.

    Driven at the retain seam rather than through the websocket so the assertion is about
    the `retain` event record itself -- the line a human reads to find out that a call is
    missing from the archive and why. The store is the REAL client talking to a route that
    answers 500, not a stubbed-out retain.
    """
    log = tmp_path / "events.jsonl"
    respx.post(VOICE_ENDPOINT).mock(return_value=httpx.Response(500, text="boom"))

    async def scenario():
        recorder = eventlog.CallRecorder(
            call_id="CAfail", mode="twilio", pipeline="realtime", direction="inbound",
            outlet="phone", path=str(log))
        status = call_record.retain_call(
            url="http://hindsight:8888", bank="voice", recorder=recorder,
            transcript=["Them: hi", "AI: hello"], document_id="voice-twilio-CAfail",
            platform="voice_twilio", lane="twilio", outcome="ok")
        # Dispatch does not block on the store.
        assert status == "dispatched"
        recorder.finish(outcome="ok", transcript_ref="voice-twilio-CAfail",
                        retain_status=status)
        # Let the detached retain settle.
        for _ in range(200):
            await asyncio.sleep(0.01)
            if any(json.loads(line).get("type") == "retain"
                   for line in log.read_text().splitlines() if line.strip()):
                break

    asyncio.run(scenario())

    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    calls = [r for r in rows if r["type"] == "call"]
    retains = [r for r in rows if r["type"] == "retain"]
    assert len(calls) == 1 and calls[0]["outcome"] == "ok", "the call record was lost"
    assert len(retains) == 1, "a failed archive write left no trace a human could find"
    assert retains[0]["ok"] is False
    assert retains[0]["document_id"] == "voice-twilio-CAfail"
    assert retains[0]["bank"] == "voice"
    assert retains[0]["err"], "the failure was recorded with no reason in it"


class _Recorder:
    """A recorder whose named attribute blows up when read."""

    call_id = "CAboom"
    target = ""
    caller = ""
    outlet = "phone"
    start_ts = 1_787_000_000.0

    def __init__(self, broken=""):
        self._broken = broken
        self.retains = []

    @property
    def direction(self):
        if self._broken == "direction":
            raise RuntimeError("direction is gone")
        return "inbound"

    def elapsed_s(self):
        if self._broken == "clock":
            raise RuntimeError("clock is gone")
        return 12.5

    def record_retain(self, **kw):
        self.retains.append(kw)


@respx.mock
def test_a_broken_clock_costs_the_duration_and_nothing_else():
    """A field that cannot be read is left OUT; the archive write still happens.

    The alternative -- refusing the write, or filling the field with 0.0 -- would either
    lose the call from the archive or state a duration nothing measured.
    """
    route = respx.post(VOICE_ENDPOINT).mock(return_value=httpx.Response(200, json={}))

    async def scenario():
        recorder = _Recorder(broken="clock")
        assert call_record.retain_call(
            url="http://hindsight:8888", bank="voice", recorder=recorder,
            transcript=["Them: hi"], document_id="voice-twilio-CAboom",
            platform="voice_twilio", lane="twilio", outcome="ok") == "dispatched"
        for _ in range(200):
            await asyncio.sleep(0.01)
            if route.called:
                break

    asyncio.run(scenario())
    assert route.called
    payload = json.loads(route.calls.last.request.read())
    assert "duration_s" not in payload["items"][0]["metadata"]


def test_a_retain_that_cannot_even_be_dispatched_never_raises_into_teardown():
    """The last resort: something inside the retain path itself blows up.

    Every retainer calls `retain_call` from a teardown `finally:`. An exception escaping
    it would take down the teardown that ends the call cleanly, so nothing may escape --
    and the caller is told the archive does not hold this call.
    """
    recorder = _Recorder(broken="direction")
    status = call_record.retain_call(
        url="http://hindsight:8888", bank="voice", recorder=recorder,
        transcript=["Them: hi"], document_id="voice-twilio-CAboom",
        platform="voice_twilio", lane="twilio", outcome="ok")
    assert status == "failed"
    assert recorder.retains and recorder.retains[0]["ok"] is False
    assert "direction is gone" in recorder.retains[0]["reason"]


def test_a_retain_with_no_event_loop_is_recorded_not_swallowed():
    """`retain_detached` off the loop: nothing is dispatched, and that is SAID."""
    seen = []
    ok = hindsight.retain_detached(
        "http://hindsight:8888", "voice", content="x", document_id="d",
        on_result=lambda ok, reason: seen.append((ok, reason)))
    assert ok is False
    assert seen and seen[0][0] is False and seen[0][1]
