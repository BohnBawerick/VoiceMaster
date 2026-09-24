"""VC24: the cascade engine with a Hermes profile as its llm stage.

The gateway is a MockTransport speaking upstream's streamed chat-completions frames; the
TTS side is the same fake ElevenLabs the rest of the engine suite uses. What is pinned
here is what the CALLER hears: sentences in order while Hermes is still writing, the
2 s / 10 s / 60 s timings moved around the whole turn (report q3-latency), a soft barge
until the reply starts and a hard one after, and an apology - never silence, never an
error body - when the turn dies.
"""
import asyncio
import json

import httpx

from voicecore import cascade_live
from voicecore import hermes_voice
from voicecore.cascade_live import CascadeLiveSession
from test_cascade_live import (ENV, FakeDeepgram, FakeRecorder, FakeTwilioWS, SlowStream)
from test_hermes_voice import DONE, USAGE, _frame, failed_stream, ok_stream
from tts_fake import http_tts_connect

GATEWAY = "http://hermes.test:18790"
CONFIG = {
    "pipeline": "cascade",
    "stt": {"provider": "deepgram", "secret_env": "DEEPGRAM_API_KEY"},
    "llm": {"provider": "hermes-agent", "kind": "hermes", "hermes_profile": "vega",
            "secret_env": "HERMES_GATEWAY_TOKEN", "endpoint": GATEWAY, "model": None,
            "tool_choice": "auto", "temperature": 0.7, "system_prompt": cascade_live.HERMES_VOICE_RULES},
    "tts": {"provider": "elevenlabs", "secret_env": "ELEVENLABS_API_KEY",
            "voice": "v-1", "speed": 1.0, "format": "ulaw_8000",
            "model": "eleven_turbo_v2_5"},
}


def make_world(turns, *, delay=0.0, tts_delay=0.0):
    """One transport for the gateway and ElevenLabs. ``turns`` is a list of frame lists,
    one per Hermes request. Returns (transport, log): log.hermes holds request bodies,
    log.spoken the text ElevenLabs was asked to say, in order."""
    class Log:
        hermes: list = []
        headers: list = []
        spoken: list = []
    log = Log()
    log.hermes, log.headers, log.spoken = [], [], []
    pending = list(turns)

    def handler(request):
        if request.url.host == "hermes.test":
            log.hermes.append(json.loads(request.content))
            log.headers.append(request.headers)
            frames = pending.pop(0) if pending else ok_stream("I have nothing more to add.")
            if isinstance(frames, int):
                return httpx.Response(frames, json={"error": "down"})
            return httpx.Response(200, stream=SlowStream(list(frames), delay),
                                  headers={"Content-Type": "text/event-stream"})
        if request.url.host == "api.elevenlabs.io":
            log.spoken.append(json.loads(request.content)["text"])
            return httpx.Response(200, stream=SlowStream([b"\xff" * 480], tts_delay))
        raise AssertionError(f"unexpected host {request.url.host}")
    return httpx.MockTransport(handler), log


def make_session(transport, *, ws=None, rec=None, direction="inbound",
                 filler_debounce_s=2.0, mission_brief=""):
    conversation = cascade_live.hermes_conversation_for(
        CONFIG, call_id="MZdirect", token="gw-token", mission_brief=mission_brief,
        caller="+61400000001", transport=transport)
    return CascadeLiveSession(
        twilio_ws=ws or FakeTwilioWS(), stream_sid="MZdirect", config=CONFIG,
        profile=None, recorder=rec or FakeRecorder(), env=ENV, stt=FakeDeepgram(),
        filler_debounce_s=filler_debounce_s, transport=transport, tts_connect=http_tts_connect(transport),
        hindsight_url="", hermes_conversation=conversation, direction=direction,
        mission_brief=mission_brief)


def user_says(session, text):
    session.messages.append({"role": "user", "content": text})
    session.transcript.append(f"Them: {text}")


# ------------------------------------------------------------- the happy turn --

def test_hermes_answers_the_call_and_every_sentence_is_spoken_in_order():
    transport, log = make_world([ok_stream(
        "Good morning, it is good to hear from you. ",
        "You have two meetings today, the first at ten. ", "Shall I read them out?")])
    rec = FakeRecorder()
    session = make_session(transport, rec=rec)
    asyncio.run(session._agent_turn(opener=True))
    assert log.spoken == ["Good morning, it is good to hear from you.",
                          "You have two meetings today, the first at ten.",
                          "Shall I read them out?"]
    assert session.transcript[-1] == ("AI: Good morning, it is good to hear from you. You "
                                      "have two meetings today, the first at ten. Shall I "
                                      "read them out?")
    assert rec.turns[-1]["usage"] == USAGE
    assert "llm_first_sentence" in rec.turns[-1]["extra"]["stage_ms"]


def test_an_inbound_call_is_opened_as_an_answered_call_and_names_the_caller():
    transport, log = make_world([ok_stream("Hello, how can I help you today?")])
    asyncio.run(make_session(transport)._agent_turn(opener=True))
    system, user = log.hermes[0]["messages"]
    assert user == {"role": "user", "content": cascade_live.HERMES_OPENER_INBOUND}
    assert system["content"].startswith(cascade_live.HERMES_VOICE_RULES)
    assert "You are speaking with +61400000001." in system["content"]
    # q5-tools: full tools for a listed caller, so no confirm-first instruction rides along.
    assert "confirm" not in system["content"].lower()
    # The vendor-LLM prompt tells the model to call hermes_agent. Hermes IS that agent.
    assert "hermes_agent" not in system["content"]
    # The tools switch is explicit on the wire (tests/test_tool_toggle.py pins it per
    # Agent); `tools: []` is never how it is done, because it enforces nothing.
    assert log.hermes[0]["tool_choice"] == "auto" and "tools" not in log.hermes[0]


def test_history_is_never_resent_and_one_call_is_one_session():
    transport, log = make_world([ok_stream("Hello, how can I help you today?"),
                                 ok_stream("It is sunny all day with a top of thirty.")])
    session = make_session(transport)

    async def run():
        await session._agent_turn(opener=True)
        user_says(session, "what is the weather")
        await session._agent_turn()
    asyncio.run(run())
    second = [m for m in log.hermes[1]["messages"] if m["role"] != "system"]
    assert second == [{"role": "user", "content": "what is the weather"}]
    assert {h["X-Hermes-Session-Id"] for h in log.headers} == {"voice-MZdirect"}


def test_an_outbound_mission_travels_with_every_turn():
    transport, log = make_world([ok_stream("Hi, I am calling about the booking for Friday.")])
    session = make_session(transport, direction="outbound",
                           mission_brief="Confirm the Friday booking.")
    asyncio.run(session._agent_turn(opener=True))
    system, user = log.hermes[0]["messages"]
    assert "== YOUR MISSION FOR THIS CALL ==\nConfirm the Friday booking." in system["content"]
    assert "other party has answered" in user["content"]


# ------------------------------------------------- the wait is audible (q3-latency) --

def test_a_slow_turn_gets_the_filler_then_the_reply(monkeypatch):
    """A turn with no answer after HERMES_FILLER_WAIT_S is a real wait: the filler."""
    monkeypatch.setattr(cascade_live, "HERMES_FILLER_WAIT_S", 0.05)
    transport, log = make_world(
        [ok_stream("Right, I have found it, your booking is for two people.")], delay=0.15)
    session = make_session(transport)
    user_says(session, "find my booking")
    asyncio.run(session._agent_turn())
    assert log.spoken == ["One sec, let me check that.",
                          "Right, I have found it, your booking is for two people."]


def test_a_long_turn_is_reassured_until_it_answers(monkeypatch):
    monkeypatch.setattr(cascade_live, "TOOL_REASSURE_AFTER_S", 0.1)
    monkeypatch.setattr(cascade_live, "HERMES_FILLER_WAIT_S", 0.02)
    transport, log = make_world(
        [ok_stream("Done, the report is in your inbox now.")], delay=0.2)
    session = make_session(transport)
    user_says(session, "send me the report")
    asyncio.run(session._agent_turn())
    assert log.spoken[0] == "One sec, let me check that."
    assert cascade_live.FILLER_REASSURE_TEXT in log.spoken[1:-1]
    assert log.spoken[-1] == "Done, the report is in your inbox now."


def test_a_fast_turn_gets_no_filler():
    transport, log = make_world([ok_stream("It is half past three in the afternoon.")])
    session = make_session(transport, filler_debounce_s=0.5)
    user_says(session, "what time is it")
    asyncio.run(session._agent_turn())
    assert log.spoken == ["It is half past three in the afternoon."]


def test_a_turn_that_blows_its_budget_ends_in_a_spoken_apology(monkeypatch):
    """The 60 s cap. The stream is closed at the cap, which is what makes upstream
    interrupt the agent: a turn the caller was told we gave up on must not keep acting."""
    monkeypatch.setattr(cascade_live, "TOOL_BUDGET_S", 0.2)
    closed: list = []

    class Hanging(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                await asyncio.sleep(30)
                yield b""
            finally:
                closed.append(True)

        async def aclose(self):
            pass

    def handler(request):
        if request.url.host == "hermes.test":
            return httpx.Response(200, stream=Hanging())
        spoken.append(json.loads(request.content)["text"])
        return httpx.Response(200, stream=SlowStream([b"\xff" * 480]))
    spoken: list = []
    rec = FakeRecorder()
    session = make_session(httpx.MockTransport(handler), rec=rec, filler_debounce_s=5.0)
    user_says(session, "reorganise my whole week")

    async def run():
        loop = asyncio.get_running_loop()
        started = loop.time()
        await session._agent_turn()
        return loop.time() - started
    took = asyncio.run(run())
    assert spoken == [cascade_live.HERMES_TIMEOUT_REPLY]
    # `closed` alone proves nothing: the fake's finally also fires when its 30 s sleep
    # simply runs out. The claim is that the stream is closed AT the cap, so the turn
    # has to be over long before that sleep could end on its own.
    assert closed == [True]
    assert took < 5.0, f"the Hermes stream was left running for {took:.1f}s past the cap"
    assert rec.turns[-1]["extra"]["hermes_turn"] == "timeout"
    assert session.transcript[-1] == f"AI: {cascade_live.HERMES_TIMEOUT_REPLY}"


# ------------------------------------------------------ a dead model is not spoken --

def test_a_failed_turn_is_an_apology_and_the_error_text_never_reaches_tts():
    transport, log = make_world([failed_stream("402 Your credit balance is too low.")])
    rec = FakeRecorder()
    session = make_session(transport, rec=rec)
    user_says(session, "what is the weather")
    asyncio.run(session._agent_turn())
    assert log.spoken == [cascade_live.HERMES_FAILED_REPLY]
    assert not any("credit" in line for line in session.transcript)
    assert rec.turns[-1]["extra"]["hermes_turn"] == "failed"
    assert len(log.hermes) == 1                      # a turn that ran is never resent


def test_a_turn_with_nothing_to_say_is_not_silence():
    transport, log = make_world([[_frame(finish="stop"), DONE]])
    session = make_session(transport)
    user_says(session, "hello?")
    asyncio.run(session._agent_turn())
    assert log.spoken == [cascade_live.HERMES_FAILED_REPLY]


def test_hermes_lost_before_answering_is_said_out_loud_and_retried(monkeypatch):
    """q6-failure, mid-call: no lane swap. Say Hermes was lost, retry inside the one
    budget, and only a failure that arrived before any response is ever resent."""
    monkeypatch.setattr(cascade_live, "HERMES_RETRY_BACKOFF_S", (0.05,))
    transport, log = make_world([503, ok_stream("Sorry about that, I am back with you now.")])
    session = make_session(transport)
    user_says(session, "are you there")
    asyncio.run(session._agent_turn())
    assert log.spoken == [cascade_live.HERMES_LOST_TEXT,
                          "Sorry about that, I am back with you now."]
    assert len(log.hermes) == 2


def test_a_gateway_that_stays_down_ends_in_the_apology_inside_the_budget(monkeypatch):
    monkeypatch.setattr(cascade_live, "HERMES_RETRY_BACKOFF_S", (0.05,))
    monkeypatch.setattr(cascade_live, "TOOL_BUDGET_S", 0.3)
    transport, log = make_world([503] * 50)
    session = make_session(transport)
    user_says(session, "are you there")
    asyncio.run(session._agent_turn())
    assert log.spoken[0] == cascade_live.HERMES_LOST_TEXT
    assert log.spoken[-1] in (cascade_live.HERMES_FAILED_REPLY,
                              cascade_live.HERMES_TIMEOUT_REPLY)
    assert 2 <= len(log.hermes) < 10


# ------------------------------------------------------------------- barge-in --

def test_a_barge_before_the_reply_starts_is_soft_and_hermes_keeps_working():
    """A cough over the filler must not kill an agent turn that may be half way through
    sending an email. Same rule the tool path has had since s8."""
    transport, log = make_world(
        [ok_stream("All done, I have sent the email to the council.")], delay=0.25)
    ws = FakeTwilioWS()
    session = make_session(transport, ws=ws, filler_debounce_s=0.02)
    user_says(session, "email the council")

    async def run():
        session._turn_task = asyncio.create_task(session._agent_turn())
        await asyncio.sleep(0.1)                     # filler playing, Hermes still working
        soft = await session._handle_barge_in()
        assert soft is True
        await session._turn_task
    asyncio.run(run())
    assert ws.events("clear")
    assert len(log.hermes) == 1                      # one turn, never re-sent
    assert log.spoken[-1] == "All done, I have sent the email to the council."


def test_a_barge_over_the_reply_is_hard_and_hermes_is_told_what_was_heard():
    transport, log = make_world([
        ok_stream("Here is the very long list of everything on your calendar this week. ",
                  "Monday has the council meeting. ", "Tuesday is free."),
        ok_stream("Of course, I will keep it short from now on.")], tts_delay=0.2)
    ws = FakeTwilioWS()
    session = make_session(transport, ws=ws)
    user_says(session, "what is on this week")

    async def run():
        session._turn_task = asyncio.create_task(session._agent_turn())
        while not ws.events("media"):                # the reply is being heard
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        soft = await session._handle_barge_in()
        assert soft is False
        user_says(session, "stop, too long")
        await session._agent_turn()
    asyncio.run(run())
    assert ws.events("clear")
    assert any(line.endswith("[interrupted]") for line in session.transcript)
    follow_up = log.hermes[1]["messages"][-1]["content"]
    assert follow_up.startswith("(The caller interrupted your last reply.")
    assert follow_up.endswith("stop, too long")
    assert "Tuesday is free" not in follow_up        # never heard, so never claimed


def test_teardown_mid_turn_closes_the_hermes_stream():
    transport, log = make_world(
        [ok_stream("This reply will never be finished because the caller hung up.")],
        delay=5.0)
    session = make_session(transport, filler_debounce_s=5.0)
    user_says(session, "hello")

    async def run():
        session._turn_task = asyncio.create_task(session._agent_turn())
        await asyncio.sleep(0.05)
        await session.teardown(outcome="ok")
        assert session._turn_task.done()
    asyncio.run(run())
    assert log.spoken == []


# ------------------------------------------------------------- construction --

def test_a_vendor_llm_config_gets_no_hermes_client():
    vendor = {"llm": {"provider": "openrouter", "kind": "chat", "endpoint": "https://x"}}
    assert cascade_live.hermes_conversation_for(vendor, call_id="c", token="t") is None


def test_an_unroutable_profile_refuses_construction_rather_than_borrowing_a_backend():
    config = {"llm": dict(CONFIG["llm"], endpoint=None)}
    try:
        cascade_live.hermes_conversation_for(config, call_id="c", token="t")
    except ValueError as exc:
        assert "vega" in str(exc) and "not routable" in str(exc)
    else:
        raise AssertionError("an unroutable profile built a client")


def test_the_engine_module_still_exposes_the_client_it_wraps():
    assert cascade_live.hermes_voice is hermes_voice
