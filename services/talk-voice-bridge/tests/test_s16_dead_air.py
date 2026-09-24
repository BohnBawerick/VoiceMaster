"""s16 c2: the ~47s of dead air on s14b call 7, root-caused on the REAL Talk wire.

ROOT CAUSE — the filler is CANCEL-ONCE.

`_handle_barge_in`'s soft branch (`cascade_live.py:600-608`) fires when the caller speaks
while a tool is in flight. It calls `_cancel_filler()`, sets `_filler_task = None`, and
**nothing ever restarts it**. The tool keeps running — correctly, that is the s8 c1
contract — but from that moment until the budget expires the caller hears NOTHING.

On the TALK lane this is far easier to trigger than on PSTN: `cascade_bridge`'s own
docstring notes there is no AEC on the null-sinks, so `_playing` gating is the only echo
guard. Once the first filler line finishes, `_playing` is False and the barge bar drops to
the normal speech threshold — a cough, a "hmm", or room noise is enough. One such blip
buys ~45 seconds of silence.

A rejected hypothesis, recorded because it is a real (if secondary) bug that was fixed
here too: `_cancel_filler` only awaits a task that is `not task.done()`, so a filler task
that RAISED is never retrieved and its exception vanishes. That would also produce silent
dead air — but TTS was demonstrably healthy on call 7 (the owner heard eight turns), so it
is not what happened. It is the same "fails silently" family, so it is closed alongside.

These tests drive the REAL `PacatWire` over fake subprocesses and count EGRESS BYTES into
pacat's stdin. Not `_speak` call counts, not log lines, not `on_audio_delta` — the bytes
that would have become sound in the owner's ear.
"""
import asyncio

import pytest

from voicecore import cascade_config
from voicecore import cascade_live
from cascade_bridge import PacatWire
from test_s12b_cascade_bridge import make_spawn
from tts_fake import http_tts_connect

FMT = cascade_config.PCM_24K

CONFIG = {
    "pipeline": "cascade",
    "stt": {"provider": "deepgram", "secret_env": "DEEPGRAM_API_KEY"},
    "llm": {"provider": "openrouter", "secret_env": "OPENROUTER_API_KEY",
            "endpoint": "https://openrouter.ai/api/v1/chat/completions",
            "model": "openai/gpt-4o-mini", "temperature": 0.7,
            "system_prompt": "base"},
    "tts": {"provider": "elevenlabs", "secret_env": "ELEVENLABS_API_KEY",
            "voice": "v-1", "speed": 1.0, "format": "pcm_24000",
            "model": "eleven_turbo_v2_5"},
}
ENV = {"OPENROUTER_API_KEY": "or-key", "ELEVENLABS_API_KEY": "el-key",
       "DEEPGRAM_API_KEY": "dg-key"}


class _Rec:
    def __init__(self):
        self.tools = []

    def on_audio_delta(self, *a, **k): pass
    def on_speech_stopped(self, *a, **k): pass
    def on_answer_audio(self, *a, **k): pass
    def on_tool_call(self, name, ms, ok=True): self.tools.append((name, ms, ok))
    def on_response_done(self, *a, **k): pass
    def on_transcript(self, *a, **k): pass


class _DG:
    async def send(self, *a, **k): pass
    async def finalize(self): return ""
    async def close(self): pass


def _tts_transport(chunk=b"\xab" * 2048):
    """TTS answers with a streamed body, so any spoken line — filler or answer — becomes
    real bytes on the wire. The FIRST LLM call returns a hermes_agent tool call (that is
    what arms the filler at all); later ones return plain content."""
    import httpx

    class _FreshStream(httpx.AsyncByteStream):
        """A NEW stream per response — a shared/consumed body raises StreamConsumed on the
        second spoken line, which would fake a dead-air failure that is my fixture's fault
        rather than the product's."""

        def __init__(self, data):
            self._data = data

        async def __aiter__(self):
            yield self._data

    state = {"llm_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "elevenlabs" in str(request.url):
            return httpx.Response(200, stream=_FreshStream(chunk))
        state["llm_calls"] += 1
        if state["llm_calls"] == 1:
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {
                    "name": "hermes_agent",
                    "arguments": '{"instruction": "what is on my calendar"}'}}]}}],
                "usage": {}})
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {}})

    return httpx.MockTransport(handler)


async def _wired_session(*, hermes_call, debounce, budget, monkeypatch):
    monkeypatch.setattr(cascade_live, "TOOL_BUDGET_S", budget)
    spawn = make_spawn()
    wire = PacatWire(audio_format=FMT, spawn=spawn)
    await wire.start()
    session = cascade_live.CascadeLiveSession(
        twilio_ws=None, stream_sid=None, config=CONFIG, profile=None,
        recorder=_Rec(), env=ENV, stt=_DG(),
        hermes_call=hermes_call, tools_enabled=True,
        filler_debounce_s=debounce, transport=(tr := _tts_transport()), tts_connect=http_tts_connect(tr),
        audio_format=FMT, wire=wire)
    return session, spawn


def _egress_bytes(spawn):
    """Total bytes written to pacat stdin — what the caller would actually hear."""
    return sum(len(p.stdin.buf) for p in spawn.pacats() if p.stdin)


@pytest.mark.asyncio
async def test_c2_a_barge_during_a_long_tool_does_not_buy_permanent_silence(monkeypatch):
    """THE DEFECT, on the real Talk wire.

    RED before s16 c2: after the soft barge the filler is dead, so no further bytes reach
    pacat for the rest of the tool call. The owner heard exactly this for ~45s.
    """
    async def slow(instruction):
        await asyncio.sleep(1.2)
        return "the answer"

    session, spawn = await _wired_session(
        hermes_call=slow, debounce=0.05, budget=5.0, monkeypatch=monkeypatch)

    turn = asyncio.create_task(session._agent_turn(opener=True))
    await asyncio.sleep(0.25)                       # first filler line has played
    before = _egress_bytes(spawn)
    assert before > 0, "precondition: the filler was audible before the barge"

    await session._handle_barge_in()                # the caller says something
    after_barge = _egress_bytes(spawn)

    await asyncio.sleep(0.7)                        # ...tool still running, budget not yet reached
    during_silence = _egress_bytes(spawn)

    await turn

    assert during_silence > after_barge, (
        "ZERO bytes reached the caller between the barge and the tool returning — the "
        "filler is cancel-once, so one cough buys the rest of the tool call in silence. "
        "This is the s14b call-7 dead air.")


@pytest.mark.asyncio
async def test_c2_the_tool_result_still_survives_the_barge(monkeypatch):
    """Restarting the filler must not break s8 c1: the dispatch keeps running and its
    result is still the tool message. A 'fix' that restarts audio by cancelling the tool
    would trade dead air for a lost answer."""
    finished = asyncio.Event()

    async def slow(instruction):
        await asyncio.sleep(0.5)
        finished.set()
        return "the real answer"

    session, _ = await _wired_session(
        hermes_call=slow, debounce=0.05, budget=5.0, monkeypatch=monkeypatch)

    turn = asyncio.create_task(session._agent_turn(opener=True))
    await asyncio.sleep(0.2)
    await session._handle_barge_in()
    await turn

    assert finished.is_set(), "the barge killed the dispatch (s8 c1 regression)"
    tool_msgs = [m for m in session.messages if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["content"] == "the real answer"


@pytest.mark.asyncio
async def test_c2_a_filler_that_raises_is_surfaced_not_swallowed(monkeypatch):
    """The secondary bug in the same family. `_cancel_filler` only awaited a task that was
    `not done()`, so a filler that RAISED had its exception discarded — silent dead air
    with nothing in the log to explain it."""
    async def slow(instruction):
        await asyncio.sleep(0.4)
        return "answer"

    session, _ = await _wired_session(
        hermes_call=slow, debounce=0.05, budget=5.0, monkeypatch=monkeypatch)

    boom = RuntimeError("tts exploded")

    async def exploding_speak(text, *, is_filler=False):
        if is_filler:
            raise boom

    monkeypatch.setattr(session, "_speak", exploding_speak)

    seen = []
    monkeypatch.setattr(cascade_live.logger, "warning",
                        lambda *a, **k: seen.append(a))
    monkeypatch.setattr(cascade_live.logger, "exception",
                        lambda *a, **k: seen.append(a))

    await session._agent_turn(opener=True)

    assert seen, ("a filler task raised and NOTHING was logged — the exception was "
                  "swallowed by _cancel_filler's `not task.done()` guard")


@pytest.mark.asyncio
async def test_c2_the_timeout_apology_is_spoken_without_a_second_llm_round(monkeypatch):
    """After the budget expires the caller must hear something even if the follow-up LLM
    round never produces audio. Pre-s16 `TOOL_TIMEOUT_REPLY` was only tool-message CONTENT,
    so the apology depended on a healthy LLM+TTS round happening AFTER the cap — more
    silence stacked on top of the wait that just failed."""
    async def hung(instruction):
        await asyncio.sleep(30)
        return "never"

    session, spawn = await _wired_session(
        hermes_call=hung, debounce=0.05, budget=0.3, monkeypatch=monkeypatch)

    # Kill ONLY the follow-up round (depth > 0). Stubbing _llm_round outright would also
    # remove the depth-0 call that issues the tool call, so nothing would ever time out.
    original = session._llm_round

    async def llm_round(depth: int = 0):
        if depth:
            return None, {}          # the post-timeout continuation produces no audio
        return await original(depth)

    monkeypatch.setattr(session, "_llm_round", llm_round)

    # Record what is SAID, not merely that bytes flowed: with the filler re-arm (also
    # landed in c2) a still-running filler keeps pacat fed forever, so a byte count alone
    # cannot tell "the apology was delivered" from "the caller is still being told to hold".
    spoken: list = []
    original_speak = session._speak

    async def spy(text, *, is_filler=False):
        spoken.append((text, is_filler))
        await original_speak(text, is_filler=is_filler)

    monkeypatch.setattr(session, "_speak", spy)

    await session._agent_turn(opener=True)

    assert _egress_bytes(spawn) > 0, "no audio reached the caller at all"
    assert any(text == cascade_live.TOOL_TIMEOUT_REPLY and not is_filler
               for text, is_filler in spoken), (
        "the budget expired and the APOLOGY was never spoken — with the follow-up LLM "
        f"round dead the caller only ever heard filler. Spoken: {spoken}")
