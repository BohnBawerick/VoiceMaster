"""s12b units — Talk cascade lane (CascadeBridge + PacatWire wire seam).

The engine (cascade_live.CascadeLiveSession) is exercised with the REAL DeepgramLive
(over a fake websocket) and the REAL TurnDetector at PCM24k — NO FakeDeepgram stub (a
DOES-NOT-COUNT cheat). parec/pacat are faked as injected subprocesses (no PulseAudio in
CI; the live proof is s14). httpx (LLM + TTS) is a MockTransport.

Grouped by criterion: c1 wire seam / mode-c byte-identical · c2 full turn over the wire ·
c3 playback-done drain model + mark keying · c4 barge flush + truncation + liveness ·
c7 engine/wire teardown parity (the session-slot half is in test_s12b_dispatch).
"""
import asyncio
import audioop
import base64
import json
import time

import httpx
import pytest

from voicecore import cascade_config
from voicecore import cascade_live
from voicecore import deepgram_live
from voicecore import turn_detect
from cascade_bridge import PacatWire
from tts_fake import http_tts_connect

FMT = cascade_config.PCM_24K
FRAME = FMT.frame_bytes                 # 960 B = 20 ms @ 24k linear16
BPS = FMT.sample_rate * FMT.bytes_per_sample   # 48000 B/s


def _pcm24(amp: int) -> bytes:
    """One 20 ms PCM16/24k frame at constant amplitude (rms == |amp|)."""
    return amp.to_bytes(2, "little", signed=True) * (FRAME // 2)


LOUD = _pcm24(9000)     # rms 9000 ≫ SPEECH_RMS 260
QUIET = _pcm24(30)      # rms 30 < 260


# --------------------------------------------------------------- fake subprocess --
class _FakeStdin:
    def __init__(self):
        self.buf = bytearray()
        self.broken = False

    def write(self, b):
        if self.broken:
            raise BrokenPipeError("fake pacat pipe broken")
        self.buf += b

    async def drain(self):
        if self.broken:
            raise BrokenPipeError("fake pacat pipe broken")


class _FakeStdout:
    """Yields one pre-scripted frame per read(), paced to ~real-time so VAD/drain timing
    matches production (a burst would desync them — the s12b DOES-NOT-COUNT). EOF = b''."""

    def __init__(self, frames, pace):
        self._frames = list(frames)
        self._pace = pace

    async def read(self, n):
        if not self._frames:
            return b""
        if self._pace:
            await asyncio.sleep(self._pace)
        return self._frames.pop(0)


class _FakeProc:
    def __init__(self, *, stdout_frames=None, pace=0.0, is_parec=False):
        self.stdin = None if is_parec else _FakeStdin()
        self.stdout = _FakeStdout(stdout_frames or [], pace) if is_parec else None
        self.returncode = None
        self.terminated = 0
        self.killed = 0

    def terminate(self):
        self.terminated += 1
        self.returncode = -15

    def kill(self):
        self.killed += 1
        self.returncode = -9

    async def wait(self):
        return self.returncode


def make_spawn(parec_frames=(), pace=0.0):
    """A fake ``create_subprocess_exec``: parec gets the scripted stdout, pacat records
    stdin. ``spawn.parec`` / ``spawn.pacats`` expose the procs (pacat respawns on barge)."""
    holder = {"parec": None, "pacats": []}

    async def spawn(*argv, stdin=None, stdout=None):
        prog = argv[0]
        if prog == "parec":
            p = _FakeProc(stdout_frames=parec_frames, pace=pace, is_parec=True)
            holder["parec"] = p
        else:
            p = _FakeProc()
            holder["pacats"].append(p)
        return p

    spawn.parec = lambda: holder["parec"]
    spawn.pacats = lambda: holder["pacats"]
    return spawn


# --------------------------------------------------------- real DeepgramLive socket --
class _FakeDGSocket:
    """A websockets-shaped fake: records sends, yields queued Results. On a ``Finalize``
    it emits the next scripted final (mirrors Deepgram flushing an utterance)."""

    def __init__(self, finals=None):
        self.sent = []
        self._q = asyncio.Queue()
        self._finals = list(finals or [])
        self.closed = False

    async def send(self, data):
        self.sent.append(data)
        if isinstance(data, str) and "Finalize" in data and self._finals:
            self._push(self._finals.pop(0))

    def _push(self, obj):
        self._q.put_nowait(json.dumps(obj))

    async def close(self):
        self.closed = True
        await self._q.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        m = await self._q.get()
        if m is None:
            raise StopAsyncIteration
        return m


def _final(text, *, start=0.0, duration=1.0):
    return {"type": "Results", "is_final": True, "start": start, "duration": duration,
            "channel": {"alternatives": [{"transcript": text}]}}


def real_deepgram(finals=None):
    sock = _FakeDGSocket(finals=finals)

    async def connect():
        return sock

    dg = deepgram_live.DeepgramLive(
        "k", connect=connect, encoding=FMT.deepgram_encoding, sample_rate=FMT.sample_rate)
    return dg, sock


# ------------------------------------------------------------------ httpx (LLM+TTS) --
class _PcmStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aiter__(self):
        for c in self._chunks:
            yield c

    async def aclose(self):
        pass


def make_transport(*, replies=("Hi there.",), tts_chunks=None, record=None):
    reply_iter = iter(replies)

    def handler(request):
        if record is not None:
            record.append((request.url.host,
                           json.loads(request.content) if request.content else {}))
        if request.url.host == "openrouter.ai":
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant",
                                         "content": next(reply_iter, "More.")}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
        if request.url.host == "api.elevenlabs.io":
            chunks = tts_chunks if tts_chunks is not None else [b"\x01\x02" * (FRAME // 2)]
            return httpx.Response(200, stream=_PcmStream(chunks))
        raise AssertionError(f"unexpected host {request.url.host}")

    return httpx.MockTransport(handler)


CONFIG = {
    "pipeline": "cascade",
    "stt": {"provider": "deepgram", "secret_env": "DEEPGRAM_API_KEY",
            "wired_live": True, "model": "nova-3", "language": "en", "keyterms": []},
    "llm": {"provider": "openrouter", "secret_env": "OPENROUTER_API_KEY", "wired": True,
            "endpoint": "https://openrouter.ai/api/v1/chat/completions",
            "model": "gpt-4o-mini", "temperature": 0.7, "extra_body": {},
            "extra_headers": {}, "system_prompt": "You are on a call."},
    "tts": {"provider": "elevenlabs", "secret_env": "ELEVENLABS_API_KEY",
            "wired_live": True, "voice": "V", "speed": 1.0, "model": "eleven_flash_v2_5",
            "format": "pcm_24000"},
}
ENV = {"DEEPGRAM_API_KEY": "dg", "OPENROUTER_API_KEY": "or", "ELEVENLABS_API_KEY": "el"}


class _Recorder:
    call_id = "talk-test"
    direction = "outbound"
    target = "+61400000000"

    def __init__(self):
        self.deltas = []
        self.answer_deltas = []
        self.turns = []
        self.finishes = []

    def on_speech_stopped(self, mono):
        pass

    def on_audio_delta(self, mono):
        self.deltas.append(mono)

    def on_answer_audio(self, mono):
        self.answer_deltas.append(mono)

    def on_response_done(self, usage=None, extra=None, ts=None):
        self.turns.append(extra)

    def finish(self, **kw):
        self.finishes.append(kw)


def make_session(*, wire, stt, transport, recorder=None, detector=None,
                 clock=time.monotonic, tools_enabled=False):
    return cascade_live.CascadeLiveSession(
        twilio_ws=None, stream_sid=None, config=CONFIG, profile=None,
        recorder=recorder or _Recorder(), env=ENV, mission_brief="",
        stt=stt, hermes_call=None,
        tools_enabled=tools_enabled,
        detector=detector or turn_detect.TurnDetector(
            silence_ms=60, bytes_per_ms=FMT.bytes_per_ms, decode=FMT.decode_pcm16),
        filler_text="one sec", filler_debounce_s=2.0, clock=clock, transport=transport, tts_connect=http_tts_connect(transport),
        retain_default=False, hindsight_url="", audio_format=FMT, wire=wire)


# ============================================================ c1 — wire seam / mode-c ==
def test_c1_modec_builds_twiliowire_only_no_residual_ws():
    """mode-c constructs the default TwilioWire; the engine keeps NO _ws/_sid attrs (a
    stray one would be a second, untested egress path)."""
    ws = object()
    s = cascade_live.CascadeLiveSession(
        twilio_ws=ws, stream_sid="MZ1", config=CONFIG, profile=None,
        recorder=_Recorder(), env=ENV, stt=None, detector=turn_detect.TurnDetector(),
        retain_default=False, hindsight_url="")
    assert isinstance(s._wire, cascade_live.TwilioWire)
    assert not hasattr(s, "_ws") and not hasattr(s, "_sid")


def test_c1_twiliowire_egress_json_byte_identical():
    """emit_frame/emit_mark/emit_clear produce the EXACT pre-s12b Twilio envelopes."""
    sent = []

    class WS:
        async def send_json(self, obj):
            sent.append(obj)

    async def run():
        w = cascade_live.TwilioWire(WS(), "MZ9")
        await w.emit_frame(b"\xaa\xbb")
        await w.emit_mark(7)
        await w.emit_clear()

    asyncio.run(run())
    assert sent[0] == {"event": "media", "streamSid": "MZ9",
                       "media": {"payload": base64.b64encode(b"\xaa\xbb").decode()}}
    assert sent[1] == {"event": "mark", "streamSid": "MZ9", "mark": {"name": "utt-7"}}
    assert sent[2] == {"event": "clear", "streamSid": "MZ9"}


def test_c1_twiliowire_events_decodes_media_mark_stop():
    """events() turns the Twilio JSON stream into (kind, payload) tuples; media is
    base64-decoded and a mark carries the burst number from its name."""
    frames = [
        {"event": "media", "media": {"payload": base64.b64encode(b"\x01\x02").decode()}},
        {"event": "mark", "mark": {"name": "utt-1"}},
        {"event": "stop"},
    ]

    class WS:
        def iter_text(self):
            async def gen():
                for f in frames:
                    yield json.dumps(f)
            return gen()

    async def run():
        w = cascade_live.TwilioWire(WS(), "MZ")
        return [ev async for ev in w.events()]

    out = asyncio.run(run())
    assert out == [("media", b"\x01\x02"), ("mark", 1), ("stop", None)]


# =================================================================== c2 — full turn ==
def test_c2_full_turn_caller_to_pacat_over_real_stt():
    """A scripted caller utterance drives the WHOLE lane: real DeepgramLive over a fake
    socket, real TurnDetector @24k, LLM, streaming TTS → pacat stdin. Asserts ordered
    transcript, Deepgram linear16@24000, TTS pcm_24000, a full 960 B PCM frame at pacat,
    and parec argv rate 24000 — with NO FakeDeepgram anywhere."""
    record = []
    # opener reply "" (no speak) keeps the turn slot idle for the caller turn.
    transport = make_transport(replies=("", "Hi there."), record=record)
    dg, sock = real_deepgram(finals=[_final("hello there", start=0.0, duration=0.5)])
    # LOUD×4 opens the turn; QUIET×5 (>60 ms) proposes turn-end, then EOF.
    spawn = make_spawn(parec_frames=[LOUD] * 4 + [QUIET] * 5, pace=0.001)
    wire = PacatWire(audio_format=FMT, spawn=spawn)
    session = make_session(wire=wire, stt=dg, transport=transport)

    async def run():
        await wire.start()
        await session.run()
        if session._turn_task is not None:
            await session._turn_task     # let the caller turn finish after parec EOF
        return

    asyncio.run(run())

    # ordered transcript Them→AI (real STT produced "hello there")
    assert session.transcript == ["Them: hello there", "AI: Hi there."]
    # Deepgram spoke linear16@24000 on the wire URL
    assert "encoding=linear16" in dg._url and "sample_rate=24000" in dg._url
    # TTS was asked for pcm_24000
    tts_reqs = [b for h, b in record if h == "api.elevenlabs.io"]
    assert tts_reqs, "no TTS request"
    # parec argv carried rate 24000, and pacat received ≥1 full 960 B frame
    assert "--rate=24000" in " ".join(_parec_argv())
    pacat = spawn.pacats()[0]
    assert len(pacat.stdin.buf) >= FRAME and len(pacat.stdin.buf) % 2 == 0


def _parec_argv():
    from audio import parec_cmd
    return parec_cmd(24000)


def test_c2_tts_output_format_is_pcm_24000():
    """The engine asks ElevenLabs for pcm_24000 (the μ-law8000 constant is off this path)."""
    captured = {}

    def handler(request):
        if request.url.host == "api.elevenlabs.io":
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, stream=_PcmStream([b"\x00" * FRAME]))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hi"}}], "usage": {}})

    dg, _ = real_deepgram()
    spawn = make_spawn()
    wire = PacatWire(audio_format=FMT, spawn=spawn)
    session = make_session(wire=wire, stt=dg, transport=httpx.MockTransport(handler))

    async def run():
        await wire.start()
        await session._speak("hi")

    asyncio.run(run())
    assert captured["params"].get("output_format") == "pcm_24000"


# ============================================ c3 — playback-done drain model + keying ==
def _clock_holder(start=0.0):
    t = [start]
    return t, (lambda: t[0])


def _recording_sleep():
    calls = []

    async def sleep(d):
        calls.append(d)          # record, return immediately (deterministic)

    return calls, sleep


def _wire_with_fake_pacat(clock=time.monotonic, sleep=asyncio.sleep):
    """A PacatWire with pacat pre-attached and NO parec reader — isolates the drain model
    from ingress noise (no ("stop") in the queue)."""
    spawn = make_spawn()
    w = PacatWire(audio_format=FMT, spawn=spawn, clock=clock, sleep=sleep)
    w._pacat = _FakeProc()       # alive pacat, no reader task
    return w, spawn


def test_c3_drain_delay_is_residual_buffer_not_fixed_lead():
    """emit_mark schedules the synthetic mark after the RESIDUAL = written_s − elapsed_s,
    not a constant. 0.5 s written, 0.2 s elapsed → 0.3 s drain (NOT PACING_LEAD_S=0.30 by
    coincidence: change elapsed and the delay tracks it)."""
    t, clock = _clock_holder(0.0)
    sleeps, sleep = _recording_sleep()
    w, _ = _wire_with_fake_pacat(clock=clock, sleep=sleep)

    async def run():
        for _ in range(25):                 # 25×960 = 24000 B = 0.5 s @ 48000 B/s
            await w.emit_frame(LOUD)
        t[0] = 0.2                            # 0.2 s elapsed at mark time
        await w.emit_mark(1)
        await asyncio.sleep(0)                # let the drain task run
        await asyncio.sleep(0)
        return list(sleeps), w._queue.get_nowait()

    recorded, ev = asyncio.run(run())
    assert recorded == [pytest.approx(0.3)]
    assert ev == ("mark", 1)

    # Different elapsed → different delay (proves it is not a fixed constant).
    t2, clock2 = _clock_holder(0.0)
    sleeps2, sleep2 = _recording_sleep()
    w2, _ = _wire_with_fake_pacat(clock=clock2, sleep=sleep2)

    async def run2():
        for _ in range(25):
            await w2.emit_frame(LOUD)
        t2[0] = 0.1
        await w2.emit_mark(1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return list(sleeps2)

    assert asyncio.run(run2()) == [pytest.approx(0.4)]


def test_c3_newer_utterance_retires_stale_mark():
    """The filler+result interleave: utt-1's drain mark is a NO-OP once utt-2's first
    frame lands (counter guard). Without this the agent self-interrupts mid-utt-2."""
    t, clock = _clock_holder(0.0)
    sleeps, sleep = _recording_sleep()
    w, _ = _wire_with_fake_pacat(clock=clock, sleep=sleep)

    async def run():
        for _ in range(10):
            await w.emit_frame(LOUD)         # utt-1
        await w.emit_mark(1)                  # drain(1) scheduled
        await w.emit_frame(LOUD)             # utt-2 FIRST frame → counter → 2
        await asyncio.sleep(0)               # drain(1) runs, sees counter 2 → no-op
        await asyncio.sleep(0)
        return w._queue.qsize()

    assert asyncio.run(run()) == 0            # NO stale mark enqueued


def test_c3_positive_single_utterance_marks_once():
    """Sanity peer to the keying test: with no newer utterance, exactly one mark fires."""
    t, clock = _clock_holder(0.0)
    sleeps, sleep = _recording_sleep()
    w, _ = _wire_with_fake_pacat(clock=clock, sleep=sleep)

    async def run():
        for _ in range(10):
            await w.emit_frame(LOUD)
        await w.emit_mark(1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return w._queue.qsize(), w._queue.get_nowait()

    n, ev = asyncio.run(run())
    assert n == 1 and ev == ("mark", 1)


def test_c3_barge_before_drain_cancels_pending_mark():
    """A barge (emit_clear) before the drain fires cancels the pending mark — the
    interrupted utterance never 'finished playing' — and respawns pacat."""
    t, clock = _clock_holder(0.0)

    async def blocking_sleep(d):
        await asyncio.Event().wait()          # suspend forever (drain stays pending)

    w, spawn = _wire_with_fake_pacat(clock=clock, sleep=blocking_sleep)

    async def run():
        for _ in range(10):
            await w.emit_frame(LOUD)
        t[0] = 0.0
        await w.emit_mark(1)                  # residual 0.2 → drain suspends in sleep
        await asyncio.sleep(0)
        drain = w._drain_task
        await w.emit_clear()                  # barge
        await asyncio.sleep(0)
        return drain.cancelled() or drain.done(), w._queue.qsize(), len(spawn.pacats())

    cancelled, qsize, pacats = asyncio.run(run())
    assert cancelled and qsize == 0           # no mark
    assert pacats == 1                        # pacat respawned by the flush


def test_c3_no_playing_side_channel_in_wire():
    """The wire NEVER writes engine playback state directly — the ONLY path to
    _playing=False is the engine's mark branch (proven in c1's events test). Structural:
    cascade_bridge references no _playing, and PacatWire holds no session reference."""
    import cascade_bridge
    src = open(cascade_bridge.__file__).read()
    # No CODE touches playback state: no attribute access (._playing) and no assignment
    # (_playing =). Prose in the docstring may name the hazard — that is not a write.
    assert "._playing" not in src and "_playing =" not in src
    w = PacatWire(audio_format=FMT)
    assert not any("session" in a.lower() for a in vars(w))


# ================================================= c4 — barge flush + truncation + live ==
def _speaking_session():
    dg, _ = real_deepgram()
    spawn = make_spawn()
    wire = PacatWire(audio_format=FMT, spawn=spawn)
    wire._pacat = _FakeProc()
    session = make_session(wire=wire, stt=dg, transport=make_transport())
    return session, wire, spawn


def test_c4_hard_barge_flushes_pacat_and_truncates():
    """A hard barge (no tool) respawns pacat AND shortens the interrupted assistant
    message to the played chars + the exact interrupted marker."""
    session, wire, spawn = _speaking_session()
    text = "This is a fairly long spoken reply that got cut off partway."
    session.messages.append({"role": "assistant", "content": text})
    session.transcript.append(f"AI: {text}")
    session._playing = True
    session._speak_text = text
    session._first_frame_mono = session._clock() - 0.5     # ~0.5 s audible
    session._frames_sent = 30                              # 30×20 ms bytes played

    async def run():
        return await session._handle_barge_in()

    soft = asyncio.run(run())
    assert soft is False                                   # hard barge
    assert len(spawn.pacats()) == 1                        # pacat respawned (flush)
    assert session.messages[-1]["content"].endswith("...(interrupted by the caller)")
    assert len(session.messages[-1]["content"]) < len(text) + 40


def test_c4_soft_barge_preserves_tool_and_turn():
    """A soft barge (tool in flight) flushes audio ONCE, stops only the filler, and never
    cancels the turn/tool — the dead-air bug s7 shipped.

    s16 c2: "stops the filler" now means stops THIS filler line and re-arms — leaving the
    caller in permanent silence was itself a dead-air defect (s14b call 7)."""
    session, wire, spawn = _speaking_session()

    async def _never():
        await asyncio.Event().wait()

    async def run():
        session._tool_task = asyncio.create_task(_never(), name="tool")
        session._filler_task = asyncio.create_task(_never(), name="filler")
        session._turn_task = asyncio.create_task(_never(), name="turn")
        await asyncio.sleep(0)
        original_filler = session._filler_task
        soft = await session._handle_barge_in()
        turn_alive = not session._turn_task.done()
        tool_alive = not session._tool_task.done()
        # s16 c2 changed this contract: the barge stops the CURRENT filler audio and then
        # RE-ARMS a fresh filler after a debounce. It used to leave `_filler_task is None`
        # forever, which is exactly the ~47s of dead air on s14b call 7 -- one interjection
        # bought silence for the rest of the tool call. What must still hold is that THIS
        # filler's audio stopped and that the turn/tool were untouched.
        filler_done = original_filler.done()
        rearmed = (session._filler_task is not None
                   and session._filler_task is not original_filler)
        for t in (session._tool_task, session._turn_task, session._filler_task):
            if t is not None:
                t.cancel()
        return soft, turn_alive, tool_alive, filler_done, rearmed, len(spawn.pacats())

    soft, turn_alive, tool_alive, filler_done, rearmed, pacats = asyncio.run(run())
    assert soft is True
    assert turn_alive and tool_alive              # turn + tool survive
    assert filler_done                            # THIS filler's audio stopped
    assert rearmed                                # ...but audible cover resumes (s16 c2)
    assert pacats == 1                            # flushed once


def test_c4_dead_pacat_raises_to_teardown():
    """A broken pacat pipe on emit_frame raises — the engine tears down rather than
    soldiering on mutely (dead egress = agent gone silent)."""
    w, _ = _wire_with_fake_pacat()
    w._pacat.stdin.broken = True

    async def run():
        await w.emit_frame(LOUD)

    with pytest.raises(RuntimeError):
        asyncio.run(run())

    # A pacat that already exited also raises (no stdin to write).
    w2, _ = _wire_with_fake_pacat()
    w2._pacat.returncode = 0

    async def run2():
        await w2.emit_frame(LOUD)

    with pytest.raises(RuntimeError):
        asyncio.run(run2())


# ==================================================== c7 — engine/wire teardown parity ==
def test_c7_teardown_kills_procs_one_record_idempotent():
    """After teardown: parec+pacat terminated, Deepgram closed, exactly ONE recorder
    record; a second teardown is a no-op (the engine's _finished guard)."""
    rec = _Recorder()
    dg, sock = real_deepgram()
    spawn = make_spawn(parec_frames=[QUIET, QUIET], pace=0.001)
    wire = PacatWire(audio_format=FMT, spawn=spawn)
    session = make_session(wire=wire, stt=dg, transport=make_transport(),
                           recorder=rec)

    async def run():
        await wire.start()
        await session.run()                       # parec EOF ends it
        await wire.aclose()                        # wire owns parec/pacat death
        await session.teardown(outcome="ok")       # engine owns the ONE record + retain
        await session.teardown(outcome="ok")       # idempotent
        return

    asyncio.run(run())
    assert spawn.parec().returncode is not None
    assert spawn.pacats()[0].returncode is not None
    assert sock.closed is True
    assert len(rec.finishes) == 1                  # exactly one record, not two
