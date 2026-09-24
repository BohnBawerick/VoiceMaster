"""s7 units — cascade_live: streaming-TTS pipelining (c4), barge-in (c5), tool path
with filler watchdog (c6), teardown/retention honesty (c8)."""
import asyncio
import audioop
import base64
import json
import time

import httpx
import pytest

from voicecore import cascade_config
from voicecore import cascade_live
from voicecore import hindsight
from voicecore.cascade_live import CascadeLiveSession, FRAME_BYTES
from voicecore.turn_detect import TurnDetector
from tts_fake import http_tts_connect

# ---------------------------------------------------------------- fixtures --

FRAME_MS_BYTES = 160


def _mulaw(amplitude: int) -> bytes:
    pcm = (amplitude.to_bytes(2, "little", signed=True) * FRAME_MS_BYTES)
    return audioop.lin2ulaw(pcm[:FRAME_MS_BYTES * 2], 2)


LOUD = _mulaw(9000)
QUIET = _mulaw(30)


class FakeTwilioWS:
    """Scripted inbound events; records outbound send_json with timestamps."""

    def __init__(self, script=None):
        self.script = script or []           # list of (delay_s, message-dict)
        self.sent: list = []                 # (mono, obj)

    async def send_json(self, obj):
        self.sent.append((time.monotonic(), obj))

    def iter_text(self):
        async def gen():
            for delay, msg in self.script:
                if delay:
                    await asyncio.sleep(delay)
                yield json.dumps(msg)
        return gen()

    def events(self, kind):
        return [(t, o) for t, o in self.sent if o.get("event") == kind]


class FakeDeepgram:
    def __init__(self, utterances=None):
        self.utterances = list(utterances or [])
        self.fed: list = []
        self.resets = 0
        self.closed = 0
        self.started = 0

    async def start(self):
        self.started += 1

    async def feed(self, b):
        self.fed.append(b)

    def reset(self):
        self.resets += 1

    async def take_utterance(self, **kw):
        return self.utterances.pop(0) if self.utterances else ""

    async def close(self):
        self.closed += 1


class FakeRecorder:
    call_id = "CA-test"
    direction = "outbound"
    target = "+61400000000"
    caller = ""
    # s5: the double stands in for the real CallRecorder, so it carries the fields the
    # retain path reads off one - including the Outlet, which the phone lanes record.
    outlet = "phone"
    start_ts = 1_787_000_000.0

    def __init__(self):
        self.speech_stopped: list = []
        self.audio_deltas: list = []
        self.answer_deltas: list = []
        self.turns: list = []
        self.tools: list = []
        self.finishes: list = []
        self.retains: list = []

    def elapsed_s(self):
        return 42.0

    def record_retain(self, **kw):
        self.retains.append(kw)

    def on_speech_stopped(self, mono):
        self.speech_stopped.append(mono)

    def on_audio_delta(self, mono):
        self.audio_deltas.append(mono)

    def on_answer_audio(self, mono):
        self.answer_deltas.append(mono)

    def on_tool_call(self, name, duration_ms, ok=True):
        self.tools.append((name, duration_ms, ok))

    def on_response_done(self, usage=None, extra=None, ts=None):
        self.turns.append({"usage": usage, "extra": extra})

    def finish(self, **kw):
        self.finishes.append(kw)


class SlowStream(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0.0, done: "list | None" = None):
        self._chunks, self._delay, self._done = chunks, delay, done

    async def __aiter__(self):
        for c in self._chunks:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield c
        if self._done is not None:
            self._done.append(time.monotonic())

    async def aclose(self):
        pass


CONFIG = {
    "pipeline": "cascade",
    "stt": {"provider": "deepgram", "secret_env": "DEEPGRAM_API_KEY"},
    "llm": {"provider": "openrouter", "secret_env": "OPENROUTER_API_KEY",
            "endpoint": "https://openrouter.ai/api/v1/chat/completions",
            "model": "openai/gpt-4o-mini", "temperature": 0.7,
            "system_prompt": "base"},
    "tts": {"provider": "elevenlabs", "secret_env": "ELEVENLABS_API_KEY",
            "voice": "v-1", "speed": 1.0, "format": "ulaw_8000",
            "model": "eleven_turbo_v2_5"},
}
ENV = {"OPENROUTER_API_KEY": "or-key", "ELEVENLABS_API_KEY": "el-key",
       "DEEPGRAM_API_KEY": "dg-key"}


def make_transport(*, replies=("Hello caller.",), tts_chunks=None, tts_delay=0.0,
                   tts_done=None, tool_call_first=False, record=None):
    """Routes openrouter (LLM) + elevenlabs (TTS). ``record`` collects request JSON."""
    reply_iter = iter(replies)
    state = {"llm_calls": 0}

    def handler(request):
        if record is not None:
            record.append((request.url.host,
                           json.loads(request.content) if request.content else {}))
        if request.url.host == "openrouter.ai":
            state["llm_calls"] += 1
            if tool_call_first and state["llm_calls"] == 1:
                msg = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "tc-1", "type": "function",
                     "function": {"name": "hermes_agent",
                                  "arguments": json.dumps(
                                      {"instruction": "check the NAS"})}}]}
            else:
                msg = {"role": "assistant", "content": next(reply_iter, "More.")}
            return httpx.Response(200, json={
                "choices": [{"message": msg}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        if request.url.host == "api.elevenlabs.io":
            chunks = tts_chunks if tts_chunks is not None else [b"\xff" * 480]
            return httpx.Response(200, stream=SlowStream(chunks, tts_delay, tts_done))
        raise AssertionError(f"unexpected host {request.url.host}")
    return httpx.MockTransport(handler)


def make_session(ws, dg, rec, *, transport, tools_enabled=False, hermes_call=None,
                 profile=None, detector=None, retain_default=True,
                 hindsight_url="", filler_debounce_s=2.0):
    return CascadeLiveSession(
        twilio_ws=ws, stream_sid="MZtest", config=CONFIG, profile=profile,
        recorder=rec, env=ENV, stt=dg,
        hermes_call=hermes_call, tools_enabled=tools_enabled,
        filler_debounce_s=filler_debounce_s, detector=detector,
        transport=transport, tts_connect=http_tts_connect(transport), retain_default=retain_default,
        hindsight_url=hindsight_url)


# ------------------------------------------------------------------- c4 -----

def test_frames_stream_while_tts_body_is_still_in_flight():
    """TRUE pipelining: with a multi-chunk, delayed TTS body, ≥2 Twilio media frames
    must be sent BEFORE the body completes (not one-byte-then-block)."""
    done: list = []
    ws = FakeTwilioWS()
    rec = FakeRecorder()
    transport = make_transport(tts_chunks=[b"\xff" * 320] * 4, tts_delay=0.03,
                               tts_done=done)
    session = make_session(ws, FakeDeepgram(), rec, transport=transport)

    asyncio.run(session._agent_turn(opener=True))
    assert done, "TTS stream never completed"
    frames_before_done = [t for t, o in ws.events("media") if t < done[0]]
    assert len(frames_before_done) >= 2
    # every full frame is exactly 20ms of μ-law
    payloads = [base64.b64decode(o["media"]["payload"]) for _, o in ws.events("media")]
    assert all(len(p) == FRAME_BYTES for p in payloads[:-1])
    # a mark closes the utterance so playback drain is observable
    assert ws.events("mark")


def test_tts_is_paced_to_realtime_not_flooded():
    """s8-polish: outbound TTS is paced to ~real-time so Twilio never buffers more
    than PACING_LEAD_S of audio — the fix for 'won't shut up' (a barge clear had to
    fight seconds of pre-buffered speech). 50 frames = 1.0s of audio delivered in one
    instant chunk must take ~0.7s of wall-clock to send, not flood out near-instant."""
    ws = FakeTwilioWS()
    rec = FakeRecorder()
    n_frames = 50
    transport = make_transport(tts_chunks=[b"\xff" * (FRAME_BYTES * n_frames)])
    session = make_session(ws, FakeDeepgram(), rec, transport=transport)
    t0 = time.monotonic()
    asyncio.run(session._agent_turn(opener=True))
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.4, f"TTS not paced (sent {n_frames} frames in {elapsed:.2f}s)"
    assert len(ws.events("media")) == n_frames


def test_long_tool_wait_speaks_a_reassurance(monkeypatch):
    """s8-polish: a slow backend lookup gets a first filler AND a 'still working'
    reassurance so the caller is never left in silence during a long wait."""
    monkeypatch.setattr(cascade_live, "TOOL_REASSURE_AFTER_S", 0.1)

    async def slow_tool(instruction):
        await asyncio.sleep(0.35)
        return "done looking"

    ws = FakeTwilioWS()
    rec = FakeRecorder()
    record: list = []
    transport = make_transport(replies=("Here it is.",), tool_call_first=True,
                               record=record)
    session = make_session(ws, FakeDeepgram(), rec, transport=transport,
                           tools_enabled=True, hermes_call=slow_tool,
                           filler_debounce_s=0.02)
    asyncio.run(session._agent_turn(opener=True))
    spoken = [b.get("text") for h, b in record if h == "api.elevenlabs.io"]
    assert "One sec, let me check that." in spoken        # first filler
    assert cascade_live.FILLER_REASSURE_TEXT in spoken     # reassurance on the long wait


def test_ttfb_is_first_twilio_frame_not_per_chunk():
    ws = FakeTwilioWS()
    rec = FakeRecorder()
    transport = make_transport(tts_chunks=[b"\xff" * 320] * 3)
    session = make_session(ws, FakeDeepgram(), rec, transport=transport)
    asyncio.run(session._agent_turn(opener=True))
    assert len(rec.audio_deltas) == 1                  # first frame only
    assert len(rec.speech_stopped) == 1                # opener turn-end marker
    assert rec.audio_deltas[0] >= rec.speech_stopped[0]
    assert rec.turns and rec.turns[0]["extra"]["stage_ms"].get("tts_stream") is not None


# ------------------------------------------------------------------- c5 -----

def _barge_script():
    # let the opener turn reach the (slow) TTS, then talk over it, then hang up
    return ([(0.15, {"event": "media",
                     "media": {"payload": base64.b64encode(LOUD).decode()}})]
            + [(0.0, {"event": "media",
                      "media": {"payload": base64.b64encode(LOUD).decode()}})] * 12
            + [(0.05, {"event": "stop"})])


def _run_barge_session(**kw):
    ws = FakeTwilioWS(_barge_script())
    dg = FakeDeepgram()
    rec = FakeRecorder()
    transport = make_transport(replies=("This is a long reply that will be cut off "
                                        "by the caller mid sentence for sure.",),
                               tts_chunks=[b"\xff" * 320] * 40, tts_delay=0.05)
    detector = TurnDetector(barge_speech_ms=60, prebuffer_ms=300)
    session = make_session(ws, dg, rec, transport=transport, detector=detector, **kw)

    async def run():
        await session.run()
        await session.teardown()
    asyncio.run(run())
    return ws, dg, rec, session


def test_barge_in_clears_cancels_truncates_and_resets_stt():
    ws, dg, rec, session = _run_barge_session()
    assert ws.events("clear"), "no Twilio clear sent on barge-in"
    assert dg.resets >= 1                              # stale partials discarded
    assert dg.fed, "barge head was not replayed into STT"
    interrupted = [m for m in session.messages if m.get("role") == "assistant"
                   and "(interrupted by the caller)" in (m.get("content") or "")]
    assert interrupted, "assistant context was not truncated"
    full = "This is a long reply that will be cut off by the caller mid sentence for sure."
    assert len(interrupted[0]["content"]) < len(full) + len(" ...(interrupted by the caller)")
    assert interrupted[0]["content"] != full + " ...(interrupted by the caller)"


def test_double_clear_is_safe():
    ws = FakeTwilioWS()
    rec = FakeRecorder()
    session = make_session(ws, FakeDeepgram(), rec, transport=make_transport())

    async def run():
        await session._handle_barge_in()
        await session._handle_barge_in()               # second barge: no turn, no audio
    asyncio.run(run())
    assert len(ws.events("clear")) == 2                # idempotent, no crash
    assert not [m for m in session.messages if m.get("role") == "assistant"]


# ------------------------------------------------------------------- c6 -----

def test_filler_fires_only_when_the_tool_is_slow():
    async def slow_tool(instruction):
        await asyncio.sleep(0.15)
        return "tool says hi"

    async def fast_tool(instruction):
        return "instant"

    def run_with(tool):
        ws = FakeTwilioWS()
        rec = FakeRecorder()
        record: list = []
        transport = make_transport(replies=("Done!", "Done!"), tool_call_first=True,
                                   record=record)
        session = make_session(ws, FakeDeepgram(), rec, transport=transport,
                               tools_enabled=True, hermes_call=tool,
                               filler_debounce_s=0.05)
        asyncio.run(session._agent_turn(opener=True))
        tts_texts = [body.get("text") for host, body in record
                     if host == "api.elevenlabs.io"]
        return tts_texts, rec, session

    slow_texts, slow_rec, slow_session = run_with(slow_tool)
    assert "One sec, let me check that." in slow_texts   # watchdog fired
    fast_texts, fast_rec, _ = run_with(fast_tool)
    assert "One sec, let me check that." not in fast_texts  # fast tool: silence
    # the result came back as a REAL tool-role message feeding the next round
    tool_msgs = [m for m in slow_session.messages if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "tc-1"
    assert slow_rec.tools and slow_rec.tools[0][0] == "hermes_agent"


def test_profile_toggle_drives_cascade_advertised_schema():
    """s8 c3: the cascade lane advertises the hermes tool iff the PROFILE's
    guardrails.on_call_tools is true — the same load-per-session snapshot the live
    wiring reads (server.py: tools_enabled=snapshot.on_call_tools). No restart is in
    the path: a fresh profile object flips the advertised schema."""
    from voicecore import profiles

    def advertised(on_call_tools_doc):
        prof = profiles.ActiveProfile(
            agent_id="p", source="<t>",
            doc={"id": "p", "pipeline": "cascade",
                 "providers": {"stt": "deepgram", "llm": "nvidia-nemotron",
                               "tts": "elevenlabs"},
                 **on_call_tools_doc},
            registry={})
        record: list = []
        ws = FakeTwilioWS()
        session = make_session(
            ws, FakeDeepgram(), FakeRecorder(),
            transport=make_transport(record=record),
            tools_enabled=prof.on_call_tools,               # the live wiring
            hermes_call=(lambda i: None) if prof.on_call_tools else None,
            profile=prof)
        asyncio.run(session._agent_turn(opener=True))
        return [b for h, b in record if h == "openrouter.ai"]

    on = advertised({"guardrails": {"on_call_tools": True}})
    off = advertised({"guardrails": {"on_call_tools": False}})
    absent = advertised({})
    assert all(b.get("tools") == cascade_live.CHAT_TOOLS for b in on)
    assert all("tools" not in b for b in off)               # fail-closed
    assert all("tools" not in b for b in absent)            # absent == off


def test_tools_advertised_only_with_profile_opt_in():
    def llm_bodies(tools_enabled):
        record: list = []
        ws = FakeTwilioWS()
        session = make_session(ws, FakeDeepgram(), FakeRecorder(),
                               transport=make_transport(record=record),
                               tools_enabled=tools_enabled,
                               hermes_call=(lambda i: None) if tools_enabled else None)
        asyncio.run(session._agent_turn(opener=True))
        return [b for h, b in record if h == "openrouter.ai"]

    assert all("tools" not in b for b in llm_bodies(False))   # schema-level removal
    assert all(b.get("tools") == cascade_live.CHAT_TOOLS for b in llm_bodies(True))


def test_barge_during_tool_protects_backend_and_speaks_result():
    """s8 c1/c2: a caller barge WHILE a tool is in flight is SOFT — it stops the
    filler audio (Twilio clear) but does NOT cancel the backend call. The dispatch
    happens exactly once, the tool result still comes back as a tool-role message,
    and the final reply IS spoken. (s7 shipped the opposite — the barge killed the
    tool → dead air; this test pins the fix.)"""
    dispatches: list = []
    tool_done = asyncio.Event()

    async def slow_tool(instruction):
        dispatches.append(instruction)
        await asyncio.sleep(0.2)
        tool_done.set()
        return "the NAS is healthy"

    ws = FakeTwilioWS()
    rec = FakeRecorder()
    record: list = []
    transport = make_transport(replies=("All good, the NAS is healthy.",),
                               tool_call_first=True, record=record)
    session = make_session(ws, FakeDeepgram(), rec, transport=transport,
                           tools_enabled=True, hermes_call=slow_tool,
                           filler_debounce_s=0.05)

    async def run():
        session._turn_task = asyncio.create_task(session._agent_turn(opener=True))
        await asyncio.sleep(0.1)                       # filler now playing, tool in flight
        soft = await session._handle_barge_in()        # caller talks over the filler
        assert soft is True                            # soft barge: tool protected
        await session._turn_task                        # let the turn finish
    asyncio.run(run())
    assert dispatches == ["check the NAS"]             # exactly one dispatch, ever
    assert tool_done.is_set()                          # backend ran to completion
    tool_msgs = [m for m in session.messages if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["content"] == "the NAS is healthy"
    assert ws.events("clear")                          # filler audio was stopped
    # the RESULT reply was spoken (one final utterance after the tool returned)
    final_tts = [b.get("text") for h, b in record if h == "api.elevenlabs.io"]
    assert "All good, the NAS is healthy." in final_tts


def test_teardown_mid_tool_cancels_dispatch_exactly_once():
    """Caller hangup while a tool is in flight: teardown cancels the dispatch (one
    dispatch, ever) and still emits exactly one call record — no wedge, no duplicate."""
    dispatches: list = []

    async def hung_tool(instruction):
        dispatches.append(instruction)
        await asyncio.sleep(30)
        return "never"

    ws = FakeTwilioWS()
    rec = FakeRecorder()
    transport = make_transport(tool_call_first=True)
    session = make_session(ws, FakeDeepgram(), rec, transport=transport,
                           tools_enabled=True, hermes_call=hung_tool,
                           filler_debounce_s=10.0)

    async def run():
        session._turn_task = asyncio.create_task(session._agent_turn(opener=True))
        await asyncio.sleep(0.1)                       # tool now in flight
        await session.teardown(outcome="ok")           # caller hung up
    asyncio.run(run())
    assert dispatches == ["check the NAS"]             # exactly one dispatch, ever
    assert len(rec.finishes) == 1                      # single call record


def test_tool_timeout_speaks_apology_not_silence(monkeypatch):
    """A backend that blows TOOL_BUDGET_S yields a SPOKEN tool message, never a
    null-content hang, and the turn machine keeps going."""
    async def hung_tool(instruction):
        await asyncio.sleep(30)
        return "never"

    ws = FakeTwilioWS()
    rec = FakeRecorder()
    record: list = []
    transport = make_transport(replies=("Sorry about that.",), tool_call_first=True,
                               record=record)
    session = make_session(ws, FakeDeepgram(), rec, transport=transport,
                           tools_enabled=True, hermes_call=hung_tool,
                           filler_debounce_s=0.02)
    monkeypatch.setattr(cascade_live, "TOOL_BUDGET_S", 0.1)
    spoken: list = []
    original_speak = session._speak

    async def spy(text, *, is_filler=False):
        spoken.append(text)
        await original_speak(text, is_filler=is_filler)

    monkeypatch.setattr(session, "_speak", spy)
    asyncio.run(session._agent_turn(opener=True))

    # s16 c2: this used to assert only that TOOL_TIMEOUT_REPLY became the tool MESSAGE,
    # which the s16 consult correctly named as a near-miss -- it passed unchanged on a
    # tree where the caller heard 47s of nothing, because tool-message content is not
    # speech. The apology must be SPOKEN by the timeout path itself.
    assert cascade_live.TOOL_TIMEOUT_REPLY in spoken, (
        f"the timeout apology was never spoken; said: {spoken}")
    tool_msgs = [m for m in session.messages if m.get("role") == "tool"]
    assert tool_msgs and "timed out" in tool_msgs[0]["content"], (
        "the model must be told the lookup timed out")
    assert "ALREADY" in tool_msgs[0]["content"], (
        "the model must be told the apology was already delivered, or it repeats it")
    assert rec.tools and rec.tools[0][2] is False      # recorded as a failed tool call


class ScriptedDetector:
    """Emits a scripted FrameVerdict per feed() call (pads with a plain forward)."""
    state = "idle"

    def __init__(self, verdicts):
        from voicecore.turn_detect import FrameVerdict
        self._verdicts = list(verdicts)
        self._pad = FrameVerdict(forward_to_stt=True)

    def feed(self, raw, *, agent_playing):
        return self._verdicts.pop(0) if self._verdicts else self._pad

    def turn_ended(self):
        pass

    def extend(self):
        pass


def test_vad_turn_end_while_busy_queues_instead_of_racing():
    """s8 c1: a caller utterance that ENDS while a turn is busy must NOT spawn a
    second, competing turn — it is remembered and drained the moment the turn frees."""
    from voicecore.turn_detect import FrameVerdict
    ws = FakeTwilioWS()
    rec = FakeRecorder()
    detector = ScriptedDetector([
        FrameVerdict(forward_to_stt=True, events=["vad_turn_end"]),   # busy: queue it
        FrameVerdict(forward_to_stt=True),                            # idle: drain it
    ])
    dg = FakeDeepgram(utterances=["what's the price?"])
    session = make_session(ws, dg, rec, transport=make_transport(replies=("A dollar.",)),
                           detector=detector)

    async def busy():
        await asyncio.sleep(0.2)

    async def run():
        session._turn_task = asyncio.create_task(busy(), name="busy")
        await session._on_media(QUIET)                 # vad_turn_end while busy
        assert session._pending_turn_end is True
        assert session._turn_task.get_name() == "busy"  # NO competing turn spawned
        await session._turn_task                        # busy turn ends
        await session._on_media(QUIET)                 # next frame drains the queue
        assert session._pending_turn_end is False
        assert session._turn_task.get_name() == "cascade-turn"
        await session._turn_task
    asyncio.run(run())
    # the buffered utterance became the next turn (its reply was produced)
    assert any(m.get("content") == "what's the price?" for m in session.messages)


def test_tool_call_without_opt_in_is_denied_with_speech():
    """s8 c3 defence-in-depth: the model returns tool_calls but tools were never
    advertised (opt-in off) → a SPOKEN denial, not a dead-air null message."""
    ws = FakeTwilioWS()
    rec = FakeRecorder()
    record: list = []
    transport = make_transport(tool_call_first=True, record=record)
    session = make_session(ws, FakeDeepgram(), rec, transport=transport,
                           tools_enabled=False, hermes_call=None)
    asyncio.run(session._agent_turn(opener=True))
    spoken = [b.get("text") for h, b in record if h == "api.elevenlabs.io"]
    assert spoken == [cascade_live.TOOL_DENIED_REPLY]
    assert not [m for m in session.messages if m.get("role") == "tool"]


# ------------------------------------------------------------------- c8 -----

def test_teardown_is_single_shot_and_closes_everything(monkeypatch):
    retains: list = []
    monkeypatch.setattr(hindsight, "retain_detached",
                        lambda *a, **k: retains.append((a, k)) or True)
    ws = FakeTwilioWS()
    dg = FakeDeepgram()
    rec = FakeRecorder()
    session = make_session(ws, dg, rec, transport=make_transport(),
                           hindsight_url="http://hs:8888", retain_default=True)
    session.transcript.append("Them: hi")

    async def run():
        await session.teardown(outcome="ok")
        await session.teardown(outcome="ok")           # hangup-mid-TTS double path
    asyncio.run(run())
    assert dg.closed == 1
    assert len(rec.finishes) == 1                      # ONE call record
    assert len(retains) == 1                           # ONE retain
    assert rec.finishes[0]["transcript_ref"] == "voice-cascade-CA-test"
    assert rec.finishes[0]["retain_status"] == "dispatched"
    tags = retains[0][1]["tags"]
    assert "cascade" in tags
    # s5 (ticket 05): a call with NO selected profile records no agent, anywhere. It used
    # to tag the document with the literal "no-profile", which the Calls screen would have
    # rendered as though an Agent by that name had made the call.
    assert "no-profile" not in tags
    assert "agent" not in (retains[0][1]["metadata"] or {})


# ------------------------------------------------------------------- s9 -----

def test_opencodego_live_llm_request_carries_browser_ua():
    """s9 c3 (LIVE lane): the OpenCode Zen CF-1010 browser UA reaches the outgoing
    chat request built by cascade_live._llm_round — the copy the REAL outbound call
    uses (the bench-lane test alone would leave this path unproven). The UA comes from
    the ONE shared source (cascade_config.LLM_EXTRA_HEADERS), never a local literal."""
    recorded: list = []

    def handler(request):
        recorded.append((request.url.host, dict(request.headers)))
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "Hi caller."}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    cfg = {
        "pipeline": "cascade",
        "stt": {"provider": "deepgram", "secret_env": "DEEPGRAM_API_KEY"},
        "llm": {"provider": "opencodego", "secret_env": "OPENCODE_GO_API_KEY",
                "endpoint": cascade_config.LLM_ENDPOINTS["opencodego"],
                "model": "minimax-m3", "temperature": 0.7, "system_prompt": "base",
                "extra_headers": dict(cascade_config.LLM_EXTRA_HEADERS["opencodego"])},
        "tts": CONFIG["tts"],
    }
    session = CascadeLiveSession(
        twilio_ws=FakeTwilioWS(), stream_sid="MZtest", config=cfg, profile=None,
        recorder=FakeRecorder(), env={"OPENCODE_GO_API_KEY": "zen-key"},
        stt=FakeDeepgram(), hermes_call=None, tools_enabled=False,
        filler_debounce_s=2.0, detector=None, transport=(tr := httpx.MockTransport(handler)), tts_connect=http_tts_connect(tr),
        retain_default=True, hindsight_url="")
    session.messages.append({"role": "user", "content": "hello"})
    reply, _ = asyncio.run(session._llm_round())

    assert reply == "Hi caller."
    zen = [h for host, h in recorded if host == "opencode.ai"]
    assert zen, "no request reached the Zen host"
    assert zen[0]["user-agent"] == cascade_config.BROWSER_UA   # the shared UA, verbatim


def test_live_llm_request_carries_the_extra_body():
    """The per-provider ``extra_body`` reaches the outgoing chat request.

    gemini-flash-latest thinks by default and burns the token budget on hidden
    reasoning, so the shared builder pins ``reasoning_effort: low`` into
    ``llm.extra_body``. Until ticket 15 the ONLY thing that proved that value ever
    left the process was a bench-lane test in voice-control; the bench is gone and
    ``_llm_round`` is where a real call sends it. The value comes from the ONE shared
    table (cascade_config.LLM_EXTRA_BODY), never a local literal.
    """
    recorded: list = []

    def handler(request):
        recorded.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "Hi caller."}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    def session_for(provider):
        cfg = {
            "pipeline": "cascade",
            "stt": {"provider": "deepgram", "secret_env": "DEEPGRAM_API_KEY"},
            "llm": {"provider": provider, "secret_env": "LLM_KEY",
                    "endpoint": cascade_config.LLM_ENDPOINTS[provider],
                    "model": "m", "temperature": 0.7, "system_prompt": "base",
                    "extra_body": dict(cascade_config.LLM_EXTRA_BODY.get(provider, {}))},
            "tts": CONFIG["tts"],
        }
        session = CascadeLiveSession(
            twilio_ws=FakeTwilioWS(), stream_sid="MZtest", config=cfg, profile=None,
            recorder=FakeRecorder(), env={"LLM_KEY": "k"},
            stt=FakeDeepgram(), hermes_call=None,
            tools_enabled=False, filler_debounce_s=2.0, detector=None,
            transport=(tr := httpx.MockTransport(handler)), tts_connect=http_tts_connect(tr), retain_default=True,
            hindsight_url="")
        session.messages.append({"role": "user", "content": "hello"})
        return session

    reply, _ = asyncio.run(session_for("gemini-2.5-flash")._llm_round())
    assert reply == "Hi caller."
    assert recorded[-1]["reasoning_effort"] == "low", recorded[-1]

    # Control: a provider with no extra_body sends none, so the assertion above is
    # about this provider and not about a key the lane adds unconditionally.
    asyncio.run(session_for("openrouter")._llm_round())
    assert "reasoning_effort" not in recorded[-1], recorded[-1]


def test_live_lane_strips_think_block_before_speaking():
    """s9: minimax-m3 leaks <think>…</think> into content — the LIVE lane must return
    the spoken reply only, never the monologue (TTS would otherwise voice it)."""
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant",
                                     "content": "<think>be brief</think>All good."}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    session = CascadeLiveSession(
        twilio_ws=FakeTwilioWS(), stream_sid="MZtest", config=CONFIG, profile=None,
        recorder=FakeRecorder(), env=ENV, stt=FakeDeepgram(),
        hermes_call=None, tools_enabled=False, filler_debounce_s=2.0, detector=None,
        transport=(tr := httpx.MockTransport(handler)), tts_connect=http_tts_connect(tr), retain_default=True, hindsight_url="")
    session.messages.append({"role": "user", "content": "hello"})
    reply, _ = asyncio.run(session._llm_round())
    assert reply == "All good."


def test_retain_opt_out_never_touches_hindsight(monkeypatch):
    def boom(*a, **k):                                 # sentinel at the outbound seam
        raise AssertionError("retain dispatched despite memory.retain: false")
    monkeypatch.setattr(hindsight, "retain_detached", boom)

    class OptOutProfile:
        agent_id = "friend-caller"

        def retain_enabled(self, default):
            return False

    ws = FakeTwilioWS()
    rec = FakeRecorder()
    session = make_session(ws, FakeDeepgram(), rec, transport=make_transport(),
                           hindsight_url="http://hs:8888", profile=OptOutProfile())
    session.transcript.append("Them: secret")
    asyncio.run(session.teardown(outcome="ok"))
    assert rec.finishes[0]["retain_status"] == "skipped"


def test_end_of_turn_extra_lands_in_the_turn_record():
    """Each turn record says who decided the turn ended. Deepgram gives no verdict, so
    the VAD's stands and the record says so."""
    ws = FakeTwilioWS()
    rec = FakeRecorder()
    session = make_session(ws, FakeDeepgram(["what time is it"]), rec,
                           transport=make_transport())
    asyncio.run(session._finish_caller_turn())
    extra = rec.turns[0]["extra"]
    assert extra["end_of_turn"] == {"source": "vad", "verdict": None, "extends": 0}
    assert "stt_flush" in extra["stage_ms"]
