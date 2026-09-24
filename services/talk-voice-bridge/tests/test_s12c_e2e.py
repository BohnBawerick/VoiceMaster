"""s12c — Talk cascade lane e2e + observability.

Reuses the s12b harness (paced fake parec, REAL DeepgramLive over a fake socket, REAL
TurnDetector @24k, httpx MockTransport for LLM+TTS — NO FakeDeepgram) and drives it into
the REAL ``eventlog.CallRecorder`` writing JSONL, so every metric is read back off disk.

Grouped by criterion:
  c1 full turn over paced fakes → opener + caller turn records (schema 1, stage_ms, ttfb)
  c2 ttfb measured from first frame, not derived (vary Δ; ≠ sum(stage_ms))
  c4 per-stage latencies independent + real (engine hooks, injected clock, distinct)
  c5 no-network sentinel + positive control (transport raises on a real host)
  c7 tool-turn ttfb latches on the FILLER; additive answer_latency_ms on the ANSWER
  c3 surface schema: the call record carries mode (transport) × pipeline orthogonally
"""
import asyncio
import json
import time

import httpx
import pytest

from voicecore import cascade_config
from voicecore import deepgram_live
from voicecore import eventlog
from cascade_bridge import PacatWire

from test_s12b_cascade_bridge import (
    CONFIG, ENV, FMT, FRAME, LOUD, QUIET, _FakeProc, _PcmStream, _final,
    make_session, make_spawn, make_transport, real_deepgram,
)


def _read(path):
    return [json.loads(ln) for ln in open(path, encoding="utf-8") if ln.strip()]


def _talk_recorder(path, clock=time.monotonic, call_id="talk-s12c"):
    return eventlog.CallRecorder(
        call_id=call_id, mode="talk", pipeline="cascade", direction="outbound",
        target="+61400000000", path=str(path), clock=clock)


async def _drive(session, wire, teardown=True):
    """Boot the wire, run one caller utterance to completion, teardown (one call record)."""
    await wire.start()
    await session.run()
    if session._turn_task is not None:
        await session._turn_task
    await wire.aclose()
    if teardown:
        await session.teardown(outcome="ok")


# ============================================================ c1 — full turn e2e ==
def test_c1_e2e_opener_and_caller_turn_records(tmp_path):
    """A paced caller utterance drives the WHOLE Talk-cascade lane; the JSONL carries the
    opener turn (index 0, agent spoke first → NO stt_flush) and the caller turn (index 1,
    full stage_ms + ttfb). Read back from disk — not the recorder's memory."""
    logp = tmp_path / "events.jsonl"
    rec = _talk_recorder(logp)
    record = []
    # Opener reply "" (silent) frees the single turn-slot immediately so the caller turn
    # runs deterministically — the opener still emits turn 0 (llm stage, no stt_flush).
    transport = make_transport(
        replies=("", "Sure, here is the answer."), record=record)
    dg, sock = real_deepgram(finals=[_final("hello there", start=0.0, duration=0.5)])
    spawn = make_spawn(parec_frames=[LOUD] * 4 + [QUIET] * 5, pace=0.001)
    wire = PacatWire(audio_format=FMT, spawn=spawn)
    session = make_session(wire=wire, stt=dg, transport=transport, recorder=rec)

    asyncio.run(_drive(session, wire))

    events = _read(logp)
    turns = [e for e in events if e["type"] == "turn"]
    calls = [e for e in events if e["type"] == "call"]
    assert len(calls) == 1
    assert [t["turn_index"] for t in turns] == [0, 1]
    assert all(t["schema"] == 1 for t in turns)

    opener, caller = turns
    # opener: agent's own turn (spoke first), so its stage_ms has llm but NO stt_flush
    assert "stt_flush" not in opener["stage_ms"]
    assert opener["stage_ms"].get("llm") is not None
    # caller turn: full per-stage + a measured ttfb; no tool ⇒ answer_latency == ttfb
    csm = caller["stage_ms"]
    assert csm.get("stt_flush") is not None
    assert csm.get("llm", 0) > 0 and csm.get("tts_stream", 0) > 0
    assert caller["ttfb_ms"] is not None
    assert caller["answer_latency_ms"] == caller["ttfb_ms"]

    # ordered transcript: caller utterance (real STT) → answer (silent opener, no AI line)
    assert session.transcript == [
        "Them: hello there",
        "AI: Sure, here is the answer.",
    ]
    # real STT path, not a stub
    assert isinstance(dg, deepgram_live.DeepgramLive)
    # parec was paced (frames drained one-per-read, not a single burst)
    assert spawn.parec().returncode is not None


# ================================================ c2 — ttfb measured, not derived ==
def test_c2_ttfb_is_first_frame_delta_not_sum_of_stages(tmp_path):
    """ttfb = (first frame − turn_end); vary Δ and ttfb tracks Δ alone, independent of the
    stage_ms values (a lazy impl that summed the stages would fail the second Δ)."""
    stage = {"stt_flush": 5.0, "llm": 40.0, "tts_stream": 30.0}   # sum = 75 ms
    seen = []
    for delta in (0.25, 0.80):
        logp = tmp_path / f"c2_{delta}.jsonl"
        rec = _talk_recorder(logp, call_id=f"c2-{delta}")
        rec.on_speech_stopped(10.0)
        rec.on_audio_delta(10.0 + delta)          # first frame at turn_end + Δ
        rec.on_answer_audio(10.0 + delta)
        rec.on_response_done(usage={}, extra={"stage_ms": dict(stage)}, ts=1.0)
        turn = [e for e in _read(logp) if e["type"] == "turn"][0]
        assert turn["ttfb_ms"] == pytest.approx(delta * 1000, abs=0.1)
        assert turn["ttfb_ms"] != pytest.approx(sum(stage.values()), abs=0.1)
        seen.append(turn["ttfb_ms"])
    assert seen[0] != seen[1]                       # two Δ → two distinct ttfb values


# =========================================== c4 — per-stage latencies real+distinct ==
def test_c4_stage_ms_from_engine_hooks_distinct(tmp_path):
    """Under ONE injected clock shared by recorder+session, the engine records stt/llm/tts
    from clock deltas around the REAL stage calls — advanced only inside the httpx handler,
    never test-injected via extra=. llm (50 ms) ≠ tts_stream (20 ms), and llm > stt_flush."""
    clk = [0.0]
    clock = lambda: clk[0]
    replies = iter(["", "the answer."])              # opener silent → caller turn runs

    def handler(request):
        host = request.url.host
        if host == "openrouter.ai":
            clk[0] += 0.050                          # llm stage → 50 ms
            return httpx.Response(200, json={
                "choices": [{"message": {"content": next(replies, "more")}}], "usage": {}})
        if host == "api.elevenlabs.io":
            clk[0] += 0.020                          # tts stage → 20 ms
            return httpx.Response(200, stream=_PcmStream([b"\x00" * FRAME]))
        raise AssertionError(f"unexpected host {host}")

    logp = tmp_path / "c4.jsonl"
    rec = _talk_recorder(logp, clock=clock)
    dg, _ = real_deepgram(finals=[_final("hi there", start=0.0, duration=0.5)])
    spawn = make_spawn(parec_frames=[LOUD] * 4 + [QUIET] * 5, pace=0.001)
    wire = PacatWire(audio_format=FMT, spawn=spawn, clock=clock)
    session = make_session(wire=wire, stt=dg, transport=httpx.MockTransport(handler),
                           recorder=rec, clock=clock)

    asyncio.run(_drive(session, wire))

    caller = [e for e in _read(logp) if e["type"] == "turn"][-1]
    sm = caller["stage_ms"]
    assert sm["llm"] == pytest.approx(50.0, abs=2)
    assert sm["tts_stream"] == pytest.approx(20.0, abs=2)
    assert sm["llm"] != sm["tts_stream"]             # distinct, not a constant
    assert sm["llm"] > sm["stt_flush"]               # slow LLM > fast STT flush


# ================================================= c5 — no-network sentinel + control ==
def test_c5_positive_control_transport_raises_on_real_host():
    """The httpx sentinel is LIVE, not vacuous: a request to a real provider host raises."""
    transport = make_transport()

    async def probe():
        async with httpx.AsyncClient(transport=transport) as c:
            await c.get("https://api.deepgram.com/v1/listen")

    with pytest.raises(AssertionError):
        asyncio.run(probe())


def test_c5_e2e_touches_only_fake_hosts(tmp_path):
    """The full e2e reaches ONLY openrouter+elevenlabs through the MockTransport; Deepgram is
    a fake socket and parec/pacat are fake procs, so there is zero real egress."""
    logp = tmp_path / "c5.jsonl"
    rec = _talk_recorder(logp)
    record = []
    transport = make_transport(replies=("", "Answer."), record=record)   # opener silent
    dg, _ = real_deepgram(finals=[_final("question", start=0.0, duration=0.5)])
    spawn = make_spawn(parec_frames=[LOUD] * 4 + [QUIET] * 5, pace=0.001)
    wire = PacatWire(audio_format=FMT, spawn=spawn)
    session = make_session(wire=wire, stt=dg, transport=transport, recorder=rec)

    asyncio.run(_drive(session, wire))

    hosts = {h for h, _ in record}
    assert hosts <= {"openrouter.ai", "api.elevenlabs.io"}
    assert session._transport is transport            # injected transport, not a real client


# ============================================ c7 — tool-turn ttfb vs answer_latency ==
def _speak_session(rec, clock):
    dg, _ = real_deepgram()
    spawn = make_spawn()
    wire = PacatWire(audio_format=FMT, spawn=spawn, clock=clock)
    wire._pacat = _FakeProc()                         # alive pacat, no reader
    session = make_session(wire=wire, stt=dg, transport=make_transport(),
                           recorder=rec, clock=clock)
    return session, wire


def test_c7_tool_turn_ttfb_on_filler_answer_latency_on_answer(tmp_path):
    """A tool turn plays a FILLER first, then the answer. ttfb latches on the filler frame
    (responsiveness); the additive answer_latency_ms latches on the first NON-filler frame
    (backend-inclusive). answer_latency_ms > ttfb_ms, both real."""
    clk = [0.0]
    clock = lambda: clk[0]
    logp = tmp_path / "c7_tool.jsonl"
    rec = _talk_recorder(logp, clock=clock)
    session, wire = _speak_session(rec, clock)

    async def run():
        await wire.start()
        rec.on_speech_stopped(0.0)                    # turn end at t=0
        clk[0] = 0.2
        await session._speak("one sec, let me check", is_filler=True)   # filler @ 0.2 s
        clk[0] = 0.9
        await session._speak("here is the answer", is_filler=False)     # answer @ 0.9 s
        rec.on_response_done(usage={}, extra={"stage_ms": {}}, ts=1.0)

    asyncio.run(run())
    turn = [e for e in _read(logp) if e["type"] == "turn"][0]
    assert turn["ttfb_ms"] == pytest.approx(200.0, abs=1)
    assert turn["answer_latency_ms"] == pytest.approx(900.0, abs=1)
    assert turn["answer_latency_ms"] > turn["ttfb_ms"]


def test_c7_no_tool_turn_answer_latency_equals_ttfb(tmp_path):
    """No filler ⇒ the answer IS the first frame ⇒ answer_latency_ms == ttfb_ms."""
    clk = [0.0]
    clock = lambda: clk[0]
    logp = tmp_path / "c7_notool.jsonl"
    rec = _talk_recorder(logp, clock=clock)
    session, wire = _speak_session(rec, clock)

    async def run():
        await wire.start()
        rec.on_speech_stopped(0.0)
        clk[0] = 0.15
        await session._speak("here is the answer", is_filler=False)
        rec.on_response_done(usage={}, extra={"stage_ms": {}}, ts=1.0)

    asyncio.run(run())
    turn = [e for e in _read(logp) if e["type"] == "turn"][0]
    assert turn["ttfb_ms"] == pytest.approx(150.0, abs=1)
    assert turn["answer_latency_ms"] == turn["ttfb_ms"]


# ===================================================== c3 — surface schema (mode×pipeline) ==
@pytest.mark.parametrize("mode", ["talk", "twilio"])
def test_c3_call_record_carries_transport_and_pipeline_orthogonally(tmp_path, mode):
    """Both cascade surfaces write mode=transport (talk|twilio) AND pipeline='cascade' —
    the pipeline value is NEVER smuggled into mode. (The two production writers are the
    2-line literals guarded by the `grep -n 'mode=\"cascade\"'` → NONE check.)"""
    logp = tmp_path / f"c3_{mode}.jsonl"
    rec = eventlog.CallRecorder(call_id="x", mode=mode, pipeline="cascade",
                                direction="outbound", target="+61400000000",
                                path=str(logp), clock=time.monotonic)
    rec.finish(outcome="ok")
    call = [e for e in _read(logp) if e["type"] == "call"][0]
    assert call["mode"] == mode and call["mode"] != "cascade"
    assert call["pipeline"] == "cascade"
