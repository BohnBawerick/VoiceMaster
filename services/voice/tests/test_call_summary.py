"""Ticket 06 - the per-call summary, written by the Agent that was on the call.

Every test here is written against one of the three bars the ticket has to clear, and
each is asserted from what the ARCHIVE ended up holding (or from what the call produced),
not from a helper's return value:

1. **The call is the product, the summary is a by-product.** A summariser that raises,
   hangs, times out or answers nonsense costs the document its summary and nothing else -
   the call ends on time and the transcript, recording reference and every ticket-05
   field are retained complete.
2. **No fabricated summaries.** A call with nothing on it never reaches the gateway at
   all, and gets no summary - not a generic one, not an empty one, not an inference from
   silence.
3. **Absent is legible as absent.** The document says WHICH absence it is, so the screen
   can tell "there was nothing to summarise" from "the Agent could not answer" from
   "nobody was asked".
"""
import asyncio
import json
import re
import threading
import time
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from voicecore import call_record
from voicecore import eventlog
from voicecore import hermes_gateway
from voicecore import hindsight
from voicecore import summary as call_summary
import server
from test_bargein import ScriptedOpenAIWS   # reuse the scripted fake


VOICE_ENDPOINT = "http://hindsight:8888/v1/default/banks/voice/memories"
GATEWAY = "http://gateway.test/v1/chat/completions"

# A transcript with a real two-sided conversation on it - the only kind that gets a
# summary at all.
REAL_CALL = [
    "Them: hi, I'm calling about the delivery that was meant to arrive on Tuesday",
    "AI: sure, let me look that up for you - it went out Monday and is due tomorrow",
    "Them: great, thanks, that's all I needed",
]


class _Recorder:
    """Enough recorder for `retain_call`, with a record_retain that keeps its rows."""

    call_id = "CAsum"
    direction = "inbound"
    outlet = "phone"
    target = ""
    caller = "+61400000000"
    start_ts = 1_787_000_000.0

    def __init__(self):
        self.retains = []

    def elapsed_s(self):
        return 41.5

    def record_retain(self, **kw):
        self.retains.append(kw)


@pytest.fixture
def client():
    return TestClient(server.app)


def _gateway_reply(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def _retain(transcript, summariser, **kw):
    """Drive one retain to completion and return the metadata that reached the store."""
    route = respx.post(VOICE_ENDPOINT).mock(return_value=httpx.Response(200, json={}))
    recorder = _Recorder()

    async def scenario():
        status = call_record.retain_call(
            url="http://hindsight:8888", bank="voice", recorder=recorder,
            transcript=transcript, document_id="voice-twilio-CAsum",
            platform="voice_twilio", lane="twilio", agent="hermes-main",
            outcome="ok", recording="2026/08/18/phone-CAsum.opus",
            summariser=summariser, **kw)
        assert status == "dispatched"
        for _ in range(400):
            await asyncio.sleep(0.01)
            if route.called:
                break

    asyncio.run(scenario())
    assert route.called, "the call never reached the archive"
    return json.loads(route.calls.last.request.read())["items"][0], recorder


# ---------------------------------------------------------------------------
# Bar 2: nothing is invented for a call with nothing on it
# ---------------------------------------------------------------------------

# Greetings copied from what the lanes actually open a call with, NOT trimmed to sit
# under a threshold. The first version of this guard was tested with
# "AI: Hello, Robot speaking." - 22 characters - which no answered line ever produces,
# and the 40-character combined floor it was hiding behind was already cleared by any
# real greeting. Every "hung up on the greeting" case below must use one of these.
REAL_GREETINGS = [
    "AI: Hi, you've reached Jamie's assistant, how can I help you today?",
    "AI: Hello! This is Robot answering for Jamie - what can I do for you?",
    "AI: Good afternoon, you're through to Jamie's line, how can I help?",
]


@pytest.mark.parametrize("greeting", REAL_GREETINGS)
@pytest.mark.parametrize("reply", ["Them: oh", "Them: yeah", "Them: hello",
                                   "Them: uh huh", "Them: sorry wrong number"])
def test_a_real_greeting_answered_with_one_word_is_not_a_conversation(greeting, reply):
    """The production shape of "immediately hung up", which the first guard let through.

    A greeting a realtime model actually produces is past the COMBINED floor on its own,
    so the caller could clear the guard by making any sound at all. Handed to a model,
    that becomes "The caller did not speak and hung up immediately" - a sentence invented
    from silence, stored as a real summary. It is also what an STT hallucination of a
    noise word looks like.
    """
    assert len(greeting) > call_summary.MIN_SPOKEN_CHARS, (
        "this greeting is shorter than the combined floor, so it cannot prove anything - "
        "use one a real answered line produces")
    assert call_summary.is_summarisable([greeting, reply]) is False


@pytest.mark.parametrize("transcript,why", [
    ([], "an unanswered call captured nothing at all"),
    (["Them: hello"], "one word from one party is not a conversation"),
    ([REAL_GREETINGS[0], "Them: oh"], "a real greeting and a hangup"),
    (["AI: Hello? Hello, is anybody there at all? I cannot hear you."],
     "only the Agent ever spoke - the other side's silence says nothing"),
    (["Them: hi there, is this the right number for the delivery people at all?"],
     "only the caller ever spoke - the Agent never answered"),
    ([REAL_GREETINGS[1], "Them: ", "Them: "],
     "blank caller turns are not the caller speaking"),
])
def test_a_call_with_nothing_on_it_is_never_summarisable(transcript, why):
    assert call_summary.is_summarisable(transcript) is False, why


def test_the_caller_carries_the_floor_not_the_agent():
    """The asymmetry is deliberate, and it has to cut both ways to be worth having.

    The other party is the one who can be silent while the Agent fills the line by
    itself, so the floor is on THEM. An Agent that barely speaks is not the same
    problem: a caller who asks a real question and loses the line before the answer
    IS summarisable ("they asked X, the call ended before an answer"), and refusing
    that would lose a true summary rather than prevent a false one.
    """
    caller_asked_agent_dropped = [
        "Them: hi, I need to move my appointment from Thursday to Friday if that's ok",
        "AI: ok",
    ]
    assert call_summary.is_summarisable(caller_asked_agent_dropped) is True

    agent_talked_caller_did_not = [REAL_GREETINGS[2], "Them: yep"]
    assert call_summary.is_summarisable(agent_talked_caller_did_not) is False


def test_the_caller_floor_is_a_floor_and_not_a_wall():
    """A caller who says a real sentence clears it, in one turn or across several."""
    one_turn = [REAL_GREETINGS[0],
                "Them: can you please call me back later this afternoon"]
    assert call_summary.is_summarisable(one_turn) is True

    across_turns = [REAL_GREETINGS[0], "Them: yeah hi", "AI: go ahead",
                    "Them: it's about the delivery", "AI: sure", "Them: is it today"]
    assert call_summary.is_summarisable(across_turns) is True


def test_a_real_two_sided_conversation_is_summarisable():
    """The positive control: without it every guard test above passes vacuously."""
    assert call_summary.is_summarisable(REAL_CALL) is True


@respx.mock
def test_a_thin_call_never_reaches_the_gateway_at_all():
    """The guard runs BEFORE the summariser, so nothing can invent a summary.

    Asserted on the gateway route: a summariser handed an empty-ish transcript will
    happily write "the caller did not speak", which is an inference from silence and
    exactly what this ticket must not ship.
    """
    gateway = respx.post(GATEWAY).mock(return_value=_gateway_reply(
        "The caller did not speak and hung up immediately."))
    # A REAL greeting plus one word - the production shape of "hung up on the greeting",
    # and the exact case a trimmed 24-character fixture used to hide.
    item, _ = _retain([REAL_GREETINGS[0], "Them: oh"],
                      call_summary.make_summariser(gateway_url="http://gateway.test"))

    assert not gateway.called, "the Agent was asked to summarise a call with nothing on it"
    assert "summary" not in item["metadata"], "a summary was invented from silence"
    assert item["metadata"]["summary_state"] == call_summary.STATE_NOTHING


def test_a_call_that_never_connected_is_not_retained_or_summarised():
    """No transcript => no document at all, so there is nothing to summarise either."""
    asked = []

    async def summariser(transcript):
        asked.append(transcript)
        return "should never happen", call_summary.STATE_WRITTEN

    recorder = _Recorder()
    status = call_record.retain_call(
        url="http://hindsight:8888", bank="voice", recorder=recorder,
        transcript=[], document_id="voice-twilio-CAdead",
        platform="voice_twilio", lane="twilio", summariser=summariser)

    assert status == "skipped"
    assert asked == [], "the summariser was asked about a call that never happened"


@respx.mock
def test_an_empty_reply_is_an_absence_not_an_empty_summary():
    """A gateway that answers with nothing writes NO summary key.

    An empty string stored under `summary` renders as a summary that says nothing,
    which is a different (and false) claim from "no summary was written".
    """
    respx.post(GATEWAY).mock(return_value=_gateway_reply("   "))
    item, _ = _retain(REAL_CALL,
                      call_summary.make_summariser(gateway_url="http://gateway.test"))

    assert "summary" not in item["metadata"]
    assert item["metadata"]["summary_state"] == call_summary.STATE_UNAVAILABLE


@respx.mock
def test_a_gateway_apology_is_never_stored_as_the_summary():
    """The in-call helper answers a FAILURE with prose ("Sorry, that took too long.").

    Stored as a summary that sentence is a fabricated summary of a call nobody
    summarised, so this path does its own HTTP and turns every failure into None.
    """
    respx.post(GATEWAY).mock(return_value=httpx.Response(500, text="backend is down"))
    item, _ = _retain(REAL_CALL,
                      call_summary.make_summariser(gateway_url="http://gateway.test"))

    assert "summary" not in item["metadata"]
    assert item["metadata"]["summary_state"] == call_summary.STATE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Bar 1: a broken summariser costs the summary and nothing else
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer,why", [
    (RuntimeError("the gateway is on fire"), "an exception out of the gateway client"),
    ("", "an empty reply"),
    ("   \n  ", "a whitespace reply"),
    (None, "a body with no assistant text in it"),
])
def test_the_summariser_itself_answers_none_rather_than_prose(answer, why):
    """`summarise_call` is total on its own, not only because its callers catch.

    Each of these used to be a way for the archive to end up holding something that is
    not a summary of the call. The layer above catches too - which is why this asserts
    the layer HERE, directly on its return value.
    """
    async def ask(prompt):
        if isinstance(answer, Exception):
            raise answer
        return answer

    summary, state = asyncio.run(
        call_summary.summarise_call(REAL_CALL, gateway_url="http://gateway.test",
                                    ask=ask))
    assert summary is None, why
    assert state == call_summary.STATE_UNAVAILABLE, why


def test_the_real_gateway_client_turns_every_failure_into_none():
    """The HTTP layer under it, asserted on the public summarise path.

    This used to call a private ``_ask_gateway`` in summary.py. That client is
    gone: the turn is ``hermes_gateway.ask_chat``, which already promises
    never-raise / None-on-failure. Driving ``summarise_call`` without the
    ``ask=`` seam is what proves the summary path still has that contract.
    """
    with respx.mock:
        respx.post(GATEWAY).mock(return_value=httpx.Response(503, text="down"))
        text, state = asyncio.run(call_summary.summarise_call(
            REAL_CALL, gateway_url="http://gateway.test", token="", timeout_s=5))
        assert text is None and state == call_summary.STATE_UNAVAILABLE

    with respx.mock:
        respx.post(GATEWAY).mock(return_value=httpx.Response(200, text="not json"))
        text, state = asyncio.run(call_summary.summarise_call(
            REAL_CALL, gateway_url="http://gateway.test", token="", timeout_s=5))
        assert text is None and state == call_summary.STATE_UNAVAILABLE

    with respx.mock:
        respx.post(GATEWAY).mock(return_value=httpx.Response(200, json={"choices": []}))
        text, state = asyncio.run(call_summary.summarise_call(
            REAL_CALL, gateway_url="http://gateway.test", token="", timeout_s=5))
        assert text is None and state == call_summary.STATE_UNAVAILABLE

    # ...and the positive control, or the three above pass against a client that
    # always returns None.
    with respx.mock:
        respx.post(GATEWAY).mock(return_value=_gateway_reply("a real answer"))
        text, state = asyncio.run(call_summary.summarise_call(
            REAL_CALL, gateway_url="http://gateway.test", token="", timeout_s=5))
        assert (text, state) == ("a real answer", call_summary.STATE_WRITTEN)


@respx.mock
def test_a_summariser_that_fails_hard_leaves_the_call_record_intact():
    """The whole of bar 1, asserted on the document that reached the archive."""

    async def exploding(transcript):
        raise RuntimeError("the summariser is on fire")

    item, recorder = _retain(REAL_CALL, exploding)

    meta = item["metadata"]
    assert item["content"] == "\n".join(REAL_CALL), "the transcript was damaged"
    assert meta["platform"] == "voice_twilio"
    assert meta["outlet"] == "phone"
    assert meta["direction"] == "inbound"
    assert meta["agent"] == "hermes-main"
    assert meta["outcome"] == "ok"
    assert meta["duration_s"] == "41.5"
    assert meta["recording"] == "2026/08/18/phone-CAsum.opus"
    assert meta["caller"] == "+61400000000"
    assert item["tags"] == ["voice", "twilio", "inbound", "phone", "hermes-main"]
    # ...and the only thing missing is the summary, which says so.
    assert "summary" not in meta
    assert meta["summary_state"] == call_summary.STATE_UNAVAILABLE
    assert recorder.retains and recorder.retains[0]["ok"] is True


@respx.mock
@pytest.mark.parametrize("summariser,why", [
    (lambda transcript: 1 / 0, "a summariser that blows up before it is even awaited"),
    (lambda transcript: "not a coroutine at all", "a lane that wired something not async"),
    (lambda transcript: _returns("only one value"), "a summariser with the wrong shape"),
])
def test_any_broken_summariser_at_all_still_leaves_the_call_archived(summariser, why):
    """`_fill_summary` is total, not merely "catches what summarise_call raises"."""
    item, _ = _retain(REAL_CALL, summariser)
    assert item["content"] == "\n".join(REAL_CALL), why
    assert "summary" not in item["metadata"], why
    assert item["metadata"]["summary_state"] == call_summary.STATE_UNAVAILABLE, why


async def _returns(value):
    return value


@respx.mock
def test_a_hanging_summariser_is_bounded_and_the_call_is_still_archived():
    """A summariser that never returns must not cost the archive the call.

    The bound is what makes bar 1 true for a HANG rather than an error: without it the
    detached task waits forever and the document is never written at all.
    """
    started = time.monotonic()

    async def hanging(transcript):
        await asyncio.sleep(3600)

    item, _ = _retain(REAL_CALL, hanging, summary_timeout_s=0.2)

    assert time.monotonic() - started < 5.0, "the retain waited on the hanging summariser"
    assert item["content"] == "\n".join(REAL_CALL)
    assert "summary" not in item["metadata"]
    assert item["metadata"]["summary_state"] == call_summary.STATE_UNAVAILABLE


def test_a_hanging_summariser_does_not_delay_the_call_ending(client, monkeypatch):
    """End to end: the summariser hangs, the CALL still finishes now.

    Asserted from what the call produced - the websocket ran to its `stop` event and the
    per-call event record was written - which is the only evidence that matters for
    "summarisation never blocks teardown".
    """
    entered = threading.Event()

    async def hanging(prompt):
        entered.set()
        await asyncio.sleep(3600)

    monkeypatch.setenv("VOICE_SUMMARY_TIMEOUT_S", "0.2")
    monkeypatch.setattr(hermes_gateway, "ask_chat",
                        lambda prompt, **kw: hanging(prompt))
    monkeypatch.setattr(hindsight, "retain_result",
                        lambda *a, **kw: asyncio.sleep(0, result=(True, None)))

    events = []
    monkeypatch.setattr(server.eventlog, "append_event",
                        lambda obj, path=None: events.append(obj))

    fake = ScriptedOpenAIWS([
        {"type": "conversation.item.input_audio_transcription.completed",
         "transcript": "hello robot, I am calling about the delivery on Tuesday"},
        {"type": "response.output_audio_transcript.done",
         "transcript": "sure, it went out Monday and is due tomorrow"},
    ])
    monkeypatch.setattr(server.websockets, "connect", lambda *a, **kw: fake)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")
    tok = server._mint_inbound_token()

    started = time.monotonic()
    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZslow",
                      "start": {"streamSid": "MZslow", "callSid": "CAslow",
                                "customParameters": {"inbound_token": tok}}})
        ws.send_json({"event": "stop", "sequenceNumber": "2", "streamSid": "MZslow"})
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"teardown waited {elapsed:.1f}s on a hanging summariser"
    call_records = [e for e in events if e.get("type") == "call"]
    assert len(call_records) == 1, "the call record was not written"
    assert call_records[0]["outcome"] == "ok"
    # ...and the hang was real: the summariser was entered, on the detached task, AFTER
    # the call had already ended. Without this the test would pass just as well against a
    # lane that never summarises at all.
    assert entered.wait(5.0), "the summariser never ran - this test proved nothing"


@respx.mock
def test_the_retain_seam_writes_the_document_even_if_prepare_blows_up():
    """The last line of the by-product rule, asserted on `hindsight` itself.

    `prepare` is the hook the summary runs on. It is total by construction one layer up,
    but a broken observer must never be worse than no observer, so the transport refuses
    to lose a call over it either.
    """
    route = respx.post(VOICE_ENDPOINT).mock(return_value=httpx.Response(200, json={}))
    settled = []

    async def exploding_prepare():
        raise RuntimeError("prepare is on fire")

    async def scenario():
        assert hindsight.retain_detached(
            "http://hindsight:8888", "voice", content="Them: hi\nAI: hello",
            document_id="voice-twilio-CAprep", metadata={"platform": "voice_twilio"},
            tags=["voice"], prepare=exploding_prepare,
            on_result=lambda ok, reason: settled.append((ok, reason))) is True
        for _ in range(400):
            await asyncio.sleep(0.01)
            if route.called:
                break

    asyncio.run(scenario())
    assert route.called, "a broken prepare cost the archive the whole call"
    assert json.loads(route.calls.last.request.read())["items"][0]["content"] == \
        "Them: hi\nAI: hello"
    assert settled and settled[0][0] is True


# ---------------------------------------------------------------------------
# The normal path
# ---------------------------------------------------------------------------

@respx.mock
def test_the_summary_body_is_listen_only():
    """Summarising a finished call must not let the Agent fire tools.

    Sabotage: reintroduce a bare ``{"messages": [...]}`` body (the old
    ``summary._ask_gateway`` shape) and this goes red. The live difference
    between the twins was that the summary client omitted ``tool_choice``,
    so a profile that would fire skills - including anything that places a
    call - could act while writing the archive row.
    """
    route = respx.post(GATEWAY).mock(return_value=_gateway_reply("a summary"))
    text, state = asyncio.run(call_summary.summarise_call(
        REAL_CALL, gateway_url="http://gateway.test", token="", timeout_s=5))
    assert (text, state) == ("a summary", call_summary.STATE_WRITTEN)
    assert route.called
    body = json.loads(route.calls.last.request.read())
    assert body["tool_choice"] == "none"
    hermes_gateway.assert_listen_only(body)
    assert "audio" not in body
    assert "modalities" not in body


def test_summary_passes_its_own_timeout_to_the_gateway(monkeypatch):
    """Do not silently adopt ``hermes_gateway.DEFAULT_TIMEOUT_S``."""
    seen = {}

    async def spy(prompt, *, timeout_s, **kw):
        seen["timeout_s"] = timeout_s
        return "ok"

    monkeypatch.setattr(hermes_gateway, "ask_chat", spy)
    text, state = asyncio.run(call_summary.summarise_call(
        REAL_CALL, gateway_url="http://gateway.test", timeout_s=12.5))
    assert (text, state) == ("ok", call_summary.STATE_WRITTEN)
    assert seen["timeout_s"] == 12.5


def test_summary_has_no_private_gateway_client():
    """The private copies must be gone, not wrapped."""
    source = Path(call_summary.__file__).read_text()
    assert "def _ask_gateway" not in source
    assert "def _reply_text" not in source
    assert "import httpx" not in source
    assert "hermes_gateway.ask_chat" in source


@respx.mock
def test_the_agent_on_the_call_writes_the_summary_and_it_is_stored_with_the_call():
    gateway = respx.post(GATEWAY).mock(return_value=_gateway_reply(
        "The caller chased a delivery due Tuesday. It shipped Monday and arrives "
        "tomorrow; nothing outstanding."))
    item, _ = _retain(REAL_CALL,
                      call_summary.make_summariser(gateway_url="http://gateway.test",
                                                   token="secret-token"))

    assert gateway.called, "the Agent was never asked"
    asked = json.loads(gateway.calls.last.request.read())
    prompt = asked["messages"][0]["content"]
    for line in REAL_CALL:
        assert line in prompt, "the Agent was asked to summarise something else"
    assert asked["tool_choice"] == "none"
    hermes_gateway.assert_listen_only(asked)
    assert gateway.calls.last.request.headers["Authorization"] == "Bearer secret-token"

    meta = item["metadata"]
    assert meta["summary"].startswith("The caller chased a delivery")
    assert meta["summary_state"] == call_summary.STATE_WRITTEN
    # ...and it did not cost the call anything else.
    assert item["content"] == "\n".join(REAL_CALL)
    assert meta["recording"] == "2026/08/18/phone-CAsum.opus"


@respx.mock
def test_a_runaway_summary_is_cut_and_the_cut_is_visible():
    """Same rule as a mission brief: a silently shortened summary reads as the whole one."""
    respx.post(GATEWAY).mock(return_value=_gateway_reply("x" * 5000))
    item, _ = _retain(REAL_CALL,
                      call_summary.make_summariser(gateway_url="http://gateway.test"))

    stored = item["metadata"]["summary"]
    assert stored.endswith(call_record.TRUNCATION_MARK)
    assert len(stored) == call_record.SUMMARY_MAX + len(call_record.TRUNCATION_MARK)


# ---------------------------------------------------------------------------
# Bar 3: which absence this is, and whose Agent was asked
# ---------------------------------------------------------------------------

def test_the_three_absences_are_three_different_records():
    """Nobody asked / nothing to say / could not answer are not the same fact."""
    nobody_asked = call_record.build_metadata(platform="voice_twilio")
    assert "summary" not in nobody_asked and "summary_state" not in nobody_asked

    nothing = call_record.build_metadata(platform="voice_twilio",
                                         summary_state=call_summary.STATE_NOTHING)
    assert nothing["summary_state"] == call_summary.STATE_NOTHING and "summary" not in nothing

    broken = call_record.build_metadata(platform="voice_twilio",
                                        summary_state=call_summary.STATE_UNAVAILABLE)
    assert broken["summary_state"] == call_summary.STATE_UNAVAILABLE

    written = call_record.build_metadata(platform="voice_twilio", summary="it went fine")
    assert written["summary"] == "it went fine"
    assert written["summary_state"] == call_summary.STATE_WRITTEN


def test_the_document_never_contradicts_itself():
    """A `written` state with nothing under it would make the screen argue with itself."""
    empty = call_record.build_metadata(platform="voice_twilio", summary="   ",
                                       summary_state=call_summary.STATE_WRITTEN)
    assert "summary" not in empty
    assert empty["summary_state"] == call_summary.STATE_UNAVAILABLE

    contradicted = call_record.build_metadata(
        platform="voice_twilio", summary="the caller booked a table",
        summary_state=call_summary.STATE_NOTHING)
    assert contradicted["summary"] == "the caller booked a table"
    assert contradicted["summary_state"] == call_summary.STATE_WRITTEN


@respx.mock
def test_the_summary_comes_from_this_calls_own_agent_gateway(monkeypatch):
    """The Agent's `hermes_profile` picks the gateway, exactly as the in-call tool does.

    A global summariser model would answer for every Agent; this asserts the request
    landed on the backend THIS call's Agent runs on.
    """
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", "scout=http://scout.test,default=http://default.test")
    theirs = respx.post("http://scout.test/v1/chat/completions").mock(
        return_value=_gateway_reply("Scout's own account of the call."))
    other = respx.post("http://default.test/v1/chat/completions").mock(
        return_value=_gateway_reply("The wrong Agent answered."))

    snapshot = type("S", (), {"doc": {"hermes_profile": "scout"}})()
    item, _ = _retain(REAL_CALL, server._summariser_for(snapshot))

    assert theirs.called and not other.called
    assert item["metadata"]["summary"] == "Scout's own account of the call."


def test_an_agent_with_no_gateway_reports_a_gap_not_an_empty_call(monkeypatch):
    """An unknown hermes_profile has NO backend - it must never borrow another one."""
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", "somebody-else=http://elsewhere.test")
    snapshot = type("S", (), {"doc": {"hermes_profile": "nobody-configured"}})()

    summary, state = asyncio.run(server._summariser_for(snapshot)(REAL_CALL))
    assert summary is None
    assert state == call_summary.STATE_UNAVAILABLE


@respx.mock
def test_summaries_switched_off_leave_the_document_saying_nothing_about_one(monkeypatch):
    """Off is a fourth thing again: nobody was asked, so the document claims nothing."""
    monkeypatch.setenv("VOICE_SUMMARY_ENABLED", "false")
    gateway = respx.post(GATEWAY).mock(return_value=_gateway_reply("should not happen"))

    summariser = call_summary.make_summariser(gateway_url="http://gateway.test")
    assert summariser is None

    item, _ = _retain(REAL_CALL, summariser)
    assert not gateway.called
    assert "summary" not in item["metadata"]
    assert "summary_state" not in item["metadata"]


# ---------------------------------------------------------------------------
# Wiring: every lane that retains a call also asks for a summary
# ---------------------------------------------------------------------------

def test_the_phone_realtime_lane_hands_the_retain_a_summariser(monkeypatch, tmp_path):
    """A lane that quietly passed no summariser would silently never summarise.

    Asserted at the `retain_call` seam, on the keyword the lane actually passes.
    """
    seen = []
    monkeypatch.setattr(call_record, "retain_call",
                        lambda **kw: seen.append(kw) or "dispatched")

    snapshot = type("S", (), {"doc": {"hermes_profile": "default"}, "agent_id": "a",
                              "retain_enabled": staticmethod(lambda d: True)})()
    recorder = eventlog.CallRecorder(call_id="CAlane", mode="twilio",
                                     pipeline="realtime", direction="inbound",
                                     outlet="phone", path=str(tmp_path / "e.jsonl"))
    server._maybe_retain(snapshot, recorder, ["Them: hi", "AI: hello"])

    assert seen, "the realtime lane did not retain at all"
    assert seen[0].get("summariser") is not None, "this lane never asks for a summary"


@respx.mock
def test_the_cascade_engine_summarises_at_teardown(tmp_path):
    """The engine both Outlets share under Advanced summarises like the realtime lanes."""
    from voicecore.cascade_live import CascadeLiveSession
    from test_cascade_live import CONFIG, ENV, FakeDeepgram, FakeRecorder, FakeTwilioWS

    route = respx.post(VOICE_ENDPOINT).mock(return_value=httpx.Response(200, json={}))
    respx.post(GATEWAY).mock(return_value=_gateway_reply("The cascade lane's own summary."))

    session = CascadeLiveSession(
        twilio_ws=FakeTwilioWS(), stream_sid="MZcascade", config=CONFIG, profile=None,
        recorder=FakeRecorder(), env=ENV, stt=FakeDeepgram(),
        detector=None, retain_default=True, hindsight_url="http://hindsight:8888",
        summariser=call_summary.make_summariser(gateway_url="http://gateway.test"))
    session.transcript.extend(REAL_CALL)

    async def drive():
        await session.teardown()
        for _ in range(400):
            await asyncio.sleep(0.01)
            if route.called:
                break

    asyncio.run(drive())
    assert route.called
    meta = json.loads(route.calls.last.request.read())["items"][0]["metadata"]
    assert meta["summary"] == "The cascade lane's own summary."
    assert meta["summary_state"] == call_summary.STATE_WRITTEN


def test_the_cascade_construction_site_hands_the_engine_a_summariser(monkeypatch):
    """`_build_cascade_session` is where the phone cascade lane's Agent is resolved."""
    from test_cascade_live import CONFIG, ENV, FakeDeepgram, FakeTwilioWS

    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", "default=http://default.test")
    snapshot = type("S", (), {"doc": {"hermes_profile": "default"}, "agent_id": "a",
                              "registry": {}, "on_call_tools": False})()
    mission = type("M", (), {"to": "+61400000000", "brief": "say hello"})()
    session = server._build_cascade_session(
        FakeTwilioWS(), "MZbuild", CONFIG, snapshot, _Recorder(), ENV, mission,
        FakeDeepgram(), None, None)

    assert session._summariser is not None, "the cascade lane never asks for a summary"


# ---------------------------------------------------------------------------
# Anti-drift: the guard reads the labels the producers actually write
# ---------------------------------------------------------------------------

def test_the_guard_reads_the_speaker_labels_the_producers_write():
    """`is_summarisable` decides "both parties spoke" from the transcript's PREFIXES.

    That makes the prefixes a contract between the writers and one reader, and nothing
    else in the system enforces it. A lane that renamed "Them" would not fail: it would
    quietly report every one of its calls as having no conversation to summarise, forever,
    and the only visible symptom would be summaries that stopped appearing.
    """
    services = Path(__file__).resolve().parents[2]
    writers = {
        "voice/server.py": services / "voice/server.py",
        "voicecore/cascade_live.py": services / "voicecore/cascade_live.py",
        "talk-voice-bridge/realtime_bridge.py":
            services / "talk-voice-bridge/realtime_bridge.py",
    }
    labels = {call_summary.SPEAKER_AGENT, call_summary.SPEAKER_OTHER}
    found = {}
    for name, path in writers.items():
        source = path.read_text()
        written = set(re.findall(r'transcript\.append\(f"(\w+)(?=:)', source))
        # A lane whose label comes from a variable (`who`) names its values nearby.
        written |= set(re.findall(r'if "input_audio_transcription" in \w+ else "(\w+)"',
                                  source))
        written |= set(re.findall(r'who = "(\w+)" if ', source))
        assert written, f"{name}: found no transcript labels to check - the scan is stale"
        found[name] = written
        assert written <= labels, (
            f"{name} writes transcript labels {sorted(written - labels)} that "
            f"summary.is_summarisable does not recognise, so every one of its calls "
            f"would silently be reported as having no conversation to summarise")

    # ...and between them the writers really do write BOTH labels, or the assertion above
    # is satisfied by a scan that found only one of them everywhere.
    assert set().union(*found.values()) == labels
