"""VC24: the on-call Hermes client (voicecore/hermes_voice.py).

The gateway here is a MockTransport that speaks the SSE frames the pinned upstream
writes for a streamed ``/v1/chat/completions`` (``api_server_openai_routes.py`` at
345cd2b): one ``data:`` frame per delta, a terminal chunk carrying ``finish_reason``
(and, on a failed turn, ``hermes.failed`` plus an ``error`` block), then ``[DONE]``.
"""
import asyncio
import json

import httpx
import pytest

from voicecore import hermes_gateway
from voicecore import hermes_voice
from voicecore.hermes_voice import HermesConversation, HermesTurnFailed, SentenceBuffer

GATEWAY = "http://hermes.test:18790"


def _frame(delta=None, finish=None, **extra) -> bytes:
    chunk = {"id": "chatcmpl-x", "object": "chat.completion.chunk",
             "choices": [{"index": 0, "delta": ({"content": delta} if delta else {}),
                          "finish_reason": finish}], **extra}
    return f"data: {json.dumps(chunk)}\n\n".encode()


DONE = b"data: [DONE]\n\n"
USAGE = {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}


def ok_stream(*deltas) -> list:
    return [_frame(d) for d in deltas] + [_frame(finish="stop", usage=USAGE), DONE]


def failed_stream(*deltas) -> list:
    return [_frame(d) for d in deltas] + [
        _frame(finish="error",
               error={"message": "402 Your credit balance is too low", "type": "agent_error"},
               hermes={"completed": False, "partial": False, "failed": True,
                       "error": "402 Your credit balance is too low",
                       "error_code": "agent_error"}),
        DONE]


def gateway(frames, *, status=200, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": "nope"})
        return httpx.Response(200, content=b"".join(frames),
                              headers={"Content-Type": "text/event-stream"})
    return httpx.MockTransport(handler)


def conversation(transport, **kw) -> HermesConversation:
    kw.setdefault("tool_choice", "auto")
    return HermesConversation(gateway_url=GATEWAY, token="gw-token", call_id="MZ123",
                              transport=transport, **kw)


def turn(conv, text="hello") -> list:
    async def run():
        return [s async for s in conv.stream_turn(text)]
    return asyncio.run(run())


# ------------------------------------------------------------ the sentences --

def test_a_reply_comes_out_sentence_by_sentence_with_the_tail_last():
    conv = conversation(gateway(ok_stream(
        "Your next meeting is at three ", "this afternoon. It is with ",
        "the council, in the main hall. Shall I ", "move it?")))
    assert turn(conv) == ["Your next meeting is at three this afternoon.",
                          "It is with the council, in the main hall.",
                          "Shall I move it?"]
    assert conv.usage == USAGE


def test_a_short_opening_rides_along_with_the_next_sentence():
    buf = SentenceBuffer()
    assert buf.feed("Sure. ") == []                      # too short to cost a TTS request
    assert buf.feed("I have put it in your calendar for Friday. ") == [
        "Sure. I have put it in your calendar for Friday."]


def test_markup_and_reasoning_never_reach_speech():
    conv = conversation(gateway(ok_stream(
        "<think>the user wants the weather, ", "call the skill</think>",
        "**Sunny** all day in `Denver`, with a top of thirty.")))
    assert turn(conv) == ["Sunny all day in Denver, with a top of thirty."]


# ------------------------------------------------- a dead model is not read aloud --

def test_a_failed_turn_never_yields_its_error_text():
    """The report's C5. A dead model chain answers HTTP 200 with the provider's error as
    the content and hermes.failed behind it. The held tail is what keeps that text from
    reaching ElevenLabs: remove the hold and this test reads a billing error aloud."""
    conv = conversation(gateway(failed_stream("402 Your credit balance is too low.")))
    spoken: list = []

    async def run():
        async for sentence in conv.stream_turn("what is the weather"):
            spoken.append(sentence)
    with pytest.raises(HermesTurnFailed) as excinfo:
        asyncio.run(run())
    assert spoken == []
    assert "credit" not in str(excinfo.value)             # the reason is logged, not carried
    assert excinfo.value.retryable is False


def test_a_stream_that_just_stops_is_a_failure_and_its_tail_is_dropped():
    conv = conversation(gateway([_frame("I was about to say something important")]))
    with pytest.raises(HermesTurnFailed):
        turn(conv)


@pytest.mark.parametrize("status,retryable", [(503, True), (429, True), (401, False),
                                              (400, False)])
def test_only_a_refusal_before_any_response_is_retryable(status, retryable):
    """Resending a user message is only safe when the turn cannot have run."""
    conv = conversation(gateway([], status=status))
    with pytest.raises(HermesTurnFailed) as excinfo:
        turn(conv)
    assert excinfo.value.retryable is retryable


def test_a_403_names_its_likely_cause_in_the_log(caplog):
    """Upstream answers 403 to X-Hermes-Session-Id when the gateway has no API key. The
    pickup check cannot see it (/health is unauthenticated), so without this line the
    lane answers, apologises on every turn, and the log says only 'HTTP 403'."""
    conv = conversation(gateway([], status=403))
    with caplog.at_level("ERROR", logger="voice.hermes_voice"):
        with pytest.raises(HermesTurnFailed) as excinfo:
            turn(conv)
    assert excinfo.value.retryable is False
    assert "API_SERVER_KEY" in caplog.text and "X-Hermes-Session-Id" in caplog.text


def test_a_connection_that_never_opens_is_retryable():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    conv = conversation(httpx.MockTransport(refuse))
    with pytest.raises(HermesTurnFailed) as excinfo:
        turn(conv)
    assert excinfo.value.retryable is True


# --------------------------------------------------------------- the request --

def test_one_conversation_per_call_and_no_history_is_resent():
    seen: list = []
    conv = conversation(gateway(ok_stream("Hello there, it is good to hear from you."),
                                seen=seen),
                        instructions="speak plainly", model="voice")
    turn(conv, "first thing")
    turn(conv, "second thing")
    bodies = [json.loads(r.content) for r in seen]
    assert [r.url.path for r in seen] == ["/v1/chat/completions"] * 2
    assert {r.headers["X-Hermes-Session-Id"] for r in seen} == {"voice-MZ123"}
    assert {r.headers["Authorization"] for r in seen} == {"Bearer gw-token"}
    assert all("X-Hermes-Session-Key" not in r.headers for r in seen)
    # ONE user message per turn: Hermes holds the history under the session id.
    assert bodies[1]["messages"] == [{"role": "system", "content": "speak plainly"},
                                     {"role": "user", "content": "second thing"}]
    assert bodies[1]["stream"] is True and bodies[1]["model"] == "voice"
    # The tools switch rides every turn, first and later (tests/test_tool_toggle.py pins it
    # per Agent). `tools: []` is never sent: it enforces nothing.
    assert [b["tool_choice"] for b in bodies] == ["auto", "auto"]
    assert all("tools" not in b for b in bodies)


def test_no_model_is_sent_unless_the_agent_names_a_route():
    seen: list = []
    turn(conversation(gateway(ok_stream("Fine, thank you for asking."), seen=seen)))
    assert "model" not in json.loads(seen[0].content)


def test_the_session_id_is_safe_to_put_in_a_file_name():
    assert hermes_voice.session_id_for_call("MZ9f/../etc") == "voice-MZ9f-etc"
    assert hermes_voice.session_id_for_call("") == "voice-call"
    assert len(hermes_voice.session_id_for_call("x" * 999)) <= 200


def test_this_client_is_not_the_listen_only_one():
    """ask_chat refuses any body that could let an Agent act. This client has to be able
    to, so it must not be a flag on that guard - the guard has to still be standing."""
    body = conversation(gateway([]))._body("send the email")
    with pytest.raises(ValueError):
        hermes_gateway.assert_listen_only(body)


# ------------------------------------------------------------ the pickup check --

def test_the_pickup_check_passes_on_a_healthy_gateway_and_never_raises():
    def health(request):
        assert request.url.path == "/health" and "Authorization" not in request.headers
        return httpx.Response(200, json={"status": "ok"})

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    assert asyncio.run(hermes_voice.probe(GATEWAY, transport=httpx.MockTransport(health)))
    assert not asyncio.run(hermes_voice.probe(GATEWAY, transport=httpx.MockTransport(down)))
    assert not asyncio.run(hermes_voice.probe(
        GATEWAY, transport=httpx.MockTransport(lambda r: httpx.Response(502))))
    assert not asyncio.run(hermes_voice.probe(None))


def test_the_pickup_check_is_bounded():
    async def hang(request):
        await asyncio.sleep(5)
        return httpx.Response(200)

    async def run():
        loop = asyncio.get_running_loop()
        started = loop.time()
        ok = await hermes_voice.probe(GATEWAY, budget_s=0.1,
                                      transport=httpx.MockTransport(hang))
        return ok, loop.time() - started
    ok, took = asyncio.run(run())
    assert ok is False and took < 1.0
