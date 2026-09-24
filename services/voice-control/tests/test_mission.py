"""Ticket 10: the Agent writes the Mission, through its own Hermes gateway.

These pin the rules that a refactor is most likely to break:

* the named Agent's hermes_profile picks the gateway (ticket 06 seam)
* an unknown profile does not borrow another Agent's backend
* a failure returns no Mission (so a form cannot be cleared by applying it)
* neither path requests spoken audio or can reach the outbound dial
* silence / empty audio never reaches a writer that would invent a Mission

Sabotage (remove the guard, expect red) is how each claim earned its keep.
"""
import asyncio
import inspect
import json

import httpx
import pytest

from voicecore import hermes_gateway
from voicecore import mission as mission_author
from voicecore import summary as call_summary


def test_gateway_url_matches_the_in_call_seam():
    """Same mapping the in-call hermes_agent tool uses. See voice/server.py."""
    env = {
        "HERMES_GATEWAY_URL": "http://hermes:18789",
        "HERMES_PROFILE_GATEWAY_URLS": "scout=http://localhost:18790,x=http://h:1",
    }
    assert hermes_gateway.gateway_url_for_profile("default", env) == "http://hermes:18789"
    assert hermes_gateway.gateway_url_for_profile("", env) == "http://hermes:18789"
    assert hermes_gateway.gateway_url_for_profile(None, env) == "http://hermes:18789"
    assert hermes_gateway.gateway_url_for_profile("scout", env) == "http://localhost:18790"
    assert hermes_gateway.gateway_url_for_profile("x", env) == "http://h:1"
    assert hermes_gateway.gateway_url_for_profile("nope", env) is None


def test_an_unknown_profile_does_not_borrow_the_default():
    env = {
        "HERMES_GATEWAY_URL": "http://default.test",
        "HERMES_PROFILE_GATEWAY_URLS": "scout=http://sprint.test",
    }
    assert hermes_gateway.gateway_url_for_profile("nobody-configured", env) is None
    assert hermes_gateway.gateway_url_for_profile("default", env) == "http://default.test"


def test_hermes_profile_of_a_document():
    assert hermes_gateway.hermes_profile_of({"hermes_profile": "scout"}) == "scout"
    assert hermes_gateway.hermes_profile_of({"hermes_profile": "  "}) == "default"
    assert hermes_gateway.hermes_profile_of({}) == "default"
    assert hermes_gateway.hermes_profile_of(None) == "default"


def test_listen_only_payload_cannot_ask_the_model_to_speak():
    body = hermes_gateway.listen_only_chat_body(
        [{"role": "user", "content": "write a Mission"}])
    hermes_gateway.assert_listen_only(body)
    assert body["tool_choice"] == "none"
    assert "audio" not in body
    assert "modalities" not in body


def test_assert_listen_only_rejects_a_speak_back_body():
    with pytest.raises(ValueError):
        hermes_gateway.assert_listen_only({"messages": [], "tool_choice": "auto"})
    with pytest.raises(ValueError):
        hermes_gateway.assert_listen_only({
            "messages": [], "tool_choice": "none", "modalities": ["text", "audio"]})
    with pytest.raises(ValueError):
        hermes_gateway.assert_listen_only({
            "messages": [], "tool_choice": "none", "audio": {"voice": "ash"}})


def test_client_refuses_a_path_that_is_not_listen_only():
    with pytest.raises(ValueError, match="not a listen-only"):
        asyncio.run(hermes_gateway.post_json(
            "/v1/audio/speech", gateway_url="http://g", token="",
            body={"tool_choice": "none"}, timeout_s=1))
    with pytest.raises(ValueError, match="not a listen-only"):
        asyncio.run(hermes_gateway.post_json(
            "/voice/outbound", gateway_url="http://g", token="",
            body={"tool_choice": "none"}, timeout_s=1))


def test_mission_module_cannot_reach_the_dial():
    """Recording is not a call. Nothing in this module can place one."""
    for module in (mission_author, hermes_gateway):
        src = inspect.getsource(module)
        assert "place_phone_call" not in src
        assert "/voice/outbound" not in src
        assert "twilio" not in src.lower()
        assert "Calls.create" not in src
        assert "place_call" not in src


def test_reply_text_ignores_audio_output():
    """A spoken reply is not a Mission. Only assistant text counts."""
    assert hermes_gateway.reply_text({
        "choices": [{"message": {"content": None, "audio": {"data": "xxx"}}}]
    }) is None
    assert hermes_gateway.reply_text({
        "choices": [{"message": {"content": "  Book Friday.  "}}]
    }) == "Book Friday."


async def _expand(prompt, reply, **kwargs):
    async def ask(_prompt):
        if isinstance(reply, Exception):
            raise reply
        return reply
    return await mission_author.expand_mission(
        prompt, gateway_url="http://gateway.test", ask=ask, **kwargs)


@pytest.mark.parametrize("reply,why", [
    (None, "gateway returned nothing"),
    ("", "gateway returned an empty string"),
    ("   \n  ", "gateway returned whitespace"),
    (mission_author.NO_SPEECH, "gateway said it heard nothing"),
    (RuntimeError("down"), "gateway raised"),
])
def test_expand_failure_returns_no_mission(reply, why):
    text, err = asyncio.run(_expand("book friday", reply))
    assert text is None, why
    assert err, why


def test_expand_success_is_the_positive_control():
    text, err = asyncio.run(_expand("book friday", "Book a table for Friday at 7."))
    assert err is None
    assert text == "Book a table for Friday at 7."


def test_expand_without_a_gateway_does_not_ask():
    asked = []

    async def ask(prompt):
        asked.append(prompt)
        return "should not run"

    text, err = asyncio.run(mission_author.expand_mission(
        "book friday", gateway_url=None, ask=ask))
    assert text is None
    assert "no Hermes gateway" in err
    assert asked == []


def test_silence_never_reaches_the_writer():
    """A silent / empty recording is refused BEFORE the Agent is asked.

    Sabotage: drop the MIN_AUDIO_BYTES guard and this goes green against a
    writer that invents a Mission from nothing — the exact failure the
    ticket forbids.
    """
    asked = []

    async def ask(prompt):
        asked.append(prompt)
        return "Invented a Mission from silence."

    async def transcribe(audio):
        asked.append(audio)
        return "hallucinated speech"

    for blob in (b"", b"\x00" * 10, b"RIFF" + b"\x00" * 20):
        text, err = asyncio.run(mission_author.dictate_mission(
            blob, gateway_url="http://gateway.test", ask=ask, transcribe=transcribe))
        assert text is None, f"silence produced a Mission: {blob!r}"
        assert "silent" in err
    assert asked == [], "the Agent was asked to write a Mission from silence"


def test_dictate_from_a_transcript_uses_the_same_agent():
    seen = []

    async def ask(prompt):
        seen.append(prompt)
        return "Call the dentist and move Thursday to Friday."

    async def transcribe(audio):
        assert audio == b"x" * 400
        return "move thursday to friday"

    text, err = asyncio.run(mission_author.dictate_mission(
        b"x" * 400, gateway_url="http://gateway.test", ask=ask, transcribe=transcribe))
    assert err is None
    assert text == "Call the dentist and move Thursday to Friday."
    assert len(seen) == 1
    assert "move thursday to friday" in seen[0]
    assert "dictation, not a conversation" in seen[0].lower()


def test_dictate_without_a_gateway_does_not_ask():
    asked = []

    async def ask(prompt):
        asked.append(prompt)
        return "nope"

    text, err = asyncio.run(mission_author.dictate_mission(
        b"x" * 400, gateway_url=None, ask=ask))
    assert text is None
    assert "no Hermes gateway" in err
    assert asked == []


def test_expand_prompt_forbids_talking_back_and_dialing():
    src = mission_author.EXPAND_PROMPT + mission_author.DICTATE_FROM_AUDIO_PROMPT
    assert "not a conversation" in src.lower() or "Do not speak" in src
    assert "Do not place a call" in src
    assert "Do not use tools" in src
    assert "ONLY the Mission" in src


class _HandlerTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def handle_async_request(self, request):
        self.calls.append(request)
        return await self.handler(request)


_SUMMARISABLE = [
    "Them: hi, I'm calling about the delivery that was meant to arrive on Tuesday",
    "AI: sure, let me look that up for you - it went out Monday and is due tomorrow",
    "Them: great, thanks, that's all I needed",
]


def test_sabotaging_hermes_gateway_hits_summary_and_mission(monkeypatch):
    """Prove there is one seam. If one consumer stays green, there are two.

    Recipe (run by hand):

    1. In ``services/voicecore/hermes_gateway.py``, replace the body of
       ``ask_chat`` with ``return None`` (one edit, that module only).
    2. Run both consumers:

         cd services/voice && .venv/bin/python -m pytest \\
           tests/test_call_summary.py::test_the_agent_on_the_call_writes_the_summary_and_it_is_stored_with_the_call -q

         cd services/voice-control && .venv/bin/python -m pytest \\
           tests/test_mission.py::test_real_client_is_listen_only_on_the_wire -q

    3. Both must be red. If one stays green, there are still two seams.
    4. Revert the edit.

    This test does the same sabotage in-process (patching ``ask_chat``
    only) and drives both public consumers without their ``ask=`` seams.
    HTTP is mocked to a successful reply: a leftover private client in
    ``summary.py`` would take that reply and this assertion would fail.
    """
    async def sabotaged(prompt, **kwargs):
        return None

    monkeypatch.setattr(hermes_gateway, "ask_chat", sabotaged)

    leaked = []

    async def fake_post(self, url, **kw):
        leaked.append(str(url))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "SHOULD NOT BE USED"}}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    text, state = asyncio.run(call_summary.summarise_call(
        _SUMMARISABLE, gateway_url="http://gateway.test", token="",
        timeout_s=5))
    assert text is None, "summary still has its own gateway client"
    assert state == call_summary.STATE_UNAVAILABLE

    mission, err = asyncio.run(mission_author.expand_mission(
        "book friday", gateway_url="http://gateway.test", token="",
        timeout_s=5))
    assert mission is None, "mission no longer goes through hermes_gateway"
    assert err
    assert leaked == [], "a leftover client posted past ask_chat"


def test_real_client_is_listen_only_on_the_wire():
    """Asserted on the bytes, not on a seam a fixture could agree with."""

    async def handler(request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        hermes_gateway.assert_listen_only(body)
        assert "audio" not in body
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "Call them about Friday."}}]})

    transport = _HandlerTransport(handler)
    text = asyncio.run(hermes_gateway.ask_chat(
        "write it", gateway_url="http://gateway.test", token="tok",
        timeout_s=5, transport=transport))
    assert text == "Call them about Friday."
    assert transport.calls[0].headers.get("authorization") == "Bearer tok"


def test_real_client_turns_every_failure_into_none():
    async def down(request):
        return httpx.Response(503, text="down")

    async def garbage(request):
        return httpx.Response(200, text="not json")

    async def empty(request):
        return httpx.Response(200, json={"choices": []})

    for handler in (down, garbage, empty):
        transport = _HandlerTransport(handler)
        text = asyncio.run(hermes_gateway.ask_chat(
            "x", gateway_url="http://gateway.test", token="",
            timeout_s=5, transport=transport))
        assert text is None

    # Positive control, or the three above pass against a client that
    # always returns None.
    async def ok(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "a real Mission"}}]})

    assert asyncio.run(hermes_gateway.ask_chat(
        "x", gateway_url="http://gateway.test", token="", timeout_s=5,
        transport=_HandlerTransport(ok))) == "a real Mission"


def test_unreachable_gateway_is_none_not_an_exception():
    class Boom(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("refused")

    text = asyncio.run(hermes_gateway.ask_chat(
        "x", gateway_url="http://gateway.test", token="", timeout_s=5,
        transport=Boom()))
    assert text is None


def test_slow_gateway_is_none_not_an_exception():
    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ReadTimeout("slow")

    text = asyncio.run(hermes_gateway.ask_chat(
        "x", gateway_url="http://gateway.test", token="", timeout_s=0.05,
        transport=Slow()))
    assert text is None
