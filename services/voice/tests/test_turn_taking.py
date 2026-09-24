"""Ticket 21: the direct lane's turn-taking, pinned against the failures reproduced from
the owner's real calls (2026-09-23).

Each test here was sabotage-checked: the fix it binds was removed and the test went red
(the sabotage is named in each docstring). The scripted scenarios run on a Twilio
simulator that behaves like Twilio where it matters: agent audio plays at 20 ms a frame
from when it arrives, a mark is echoed only when playout reaches it (plus a network
lag), and ``clear`` drops the queued audio and echoes every pending mark at once. The
old suite's fake echoed marks from a fixed script, which is why none of this was caught.
"""
import asyncio
import audioop
import base64
import json
import time

import httpx
import pytest

from voicecore import cascade_live
from voicecore.cascade_live import CascadeLiveSession
from voicecore.turn_detect import TurnDetector
from test_cascade_live import ENV, FakeDeepgram, FakeRecorder
from test_direct_lane_engine import CONFIG
from test_hermes_voice import DONE, USAGE, _frame
from tts_fake import http_tts_connect

FRAME = 160
FRAME_S = 0.02


def _mulaw(amplitude: int) -> bytes:
    return audioop.lin2ulaw(amplitude.to_bytes(2, "little", signed=True) * FRAME, 2)


LOUD = _mulaw(9000)
QUIET = _mulaw(30)
AGENT = b"\xff"                   # agent audio byte (μ-law silence: its level is irrelevant)


class TwilioSim:
    """The phone side of a Twilio media stream, in real time.

    ``caller`` is a list of (seconds, frame) segments the caller sends, 20 ms a frame,
    followed by ``stop``. Agent media is "played" from when it arrives; ``played`` holds
    (start, end) per agent frame, cut short by a ``clear``."""

    def __init__(self, caller, *, lag=0.3):
        self.caller = caller
        self.lag = lag
        self.sent: list = []
        self.played: list = []          # [start, end] per agent frame
        self._playout_end = 0.0
        self._inbound: asyncio.Queue = asyncio.Queue()
        self._marks: list = []          # (handle, name)
        self.t0 = time.monotonic()      # reset when the call starts

    def now(self):
        return time.monotonic() - self.t0

    async def send_json(self, obj):
        now = self.now()
        self.sent.append((now, obj))
        kind = obj.get("event")
        if kind == "media":
            start = max(now, self._playout_end)
            self._playout_end = start + FRAME_S
            self.played.append([start, self._playout_end])
        elif kind == "mark":
            name = obj["mark"]["name"]
            at = max(now, self._playout_end) + self.lag
            loop = asyncio.get_running_loop()
            handle = loop.call_later(at - now, self._echo, name)
            self._marks.append((handle, name))
        elif kind == "clear":
            for span in self.played:
                if span[1] > now:
                    span[1] = max(span[0], now)
            self._playout_end = now
            for handle, name in self._marks:
                if not handle.cancelled():
                    handle.cancel()
                    self._echo(name)
            self._marks = []

    def _echo(self, name):
        self._inbound.put_nowait({"event": "mark", "mark": {"name": name}})

    def iter_text(self):
        async def gen():
            feeder = asyncio.create_task(self._feed())
            try:
                while True:
                    msg = await self._inbound.get()
                    yield json.dumps(msg)
                    if msg.get("event") == "stop":
                        return
            finally:
                feeder.cancel()
        return gen()

    async def _feed(self):
        t = 0.0
        for seconds, frame in self.caller:
            for _ in range(int(round(seconds / FRAME_S))):
                t += FRAME_S
                await asyncio.sleep(max(0.0, t - self.now()))
                self._inbound.put_nowait({"event": "media", "media": {
                    "payload": base64.b64encode(frame).decode()}})
        self._inbound.put_nowait({"event": "stop"})

    def clears(self):
        return [t for t, o in self.sent if o.get("event") == "clear"]

    def audible_after(self, t):
        """Seconds of agent audio the caller heard after ``t``."""
        return sum(max(0.0, end - max(start, t)) for start, end in self.played)

    def audible_between(self, a, b):
        return sum(max(0.0, min(end, b) - max(start, a)) for start, end in self.played)


class TimedStream(httpx.AsyncByteStream):
    """SSE frames, each sent at its own offset (seconds) from the request."""

    def __init__(self, timed, log=None):
        self.timed, self.log = timed, log

    async def __aiter__(self):
        t0 = time.monotonic()
        try:
            for at, chunk in self.timed:
                await asyncio.sleep(max(0.0, at - (time.monotonic() - t0)))
                yield chunk
        finally:
            if self.log is not None:
                self.log.append(("closed", time.monotonic()))

    async def aclose(self):
        pass


def _progress() -> bytes:
    return b'event: hermes.tool.progress\ndata: {"tool": "gmail"}\n\n'


def world(turns, *, tts_seconds_per_char=1 / 14.0):
    """Hermes (a list of timed frame lists, one per request) and ElevenLabs (audio at
    14 chars/s, returned at once) behind one transport. ``log`` records what Hermes was
    sent, what was spoken, and when each Hermes stream was closed."""
    log = {"hermes": [], "spoken": [], "closed": []}
    pending = list(turns)

    def handler(request):
        if request.url.host == "hermes.test":
            log["hermes"].append(json.loads(request.content))
            timed = pending.pop(0) if pending else [(0.0, _frame(finish="stop")), (0.0, DONE)]
            return httpx.Response(200, stream=TimedStream(timed, log["closed"]),
                                  headers={"Content-Type": "text/event-stream"})
        if request.url.host == "api.elevenlabs.io":
            text = json.loads(request.content)["text"]
            log["spoken"].append(text)
            n = int(len(text) * tts_seconds_per_char / FRAME_S)
            return httpx.Response(200, content=AGENT * (FRAME * n))
        raise AssertionError(request.url.host)
    return httpx.MockTransport(handler), log


def session_for(transport, ws, *, stt=None, direction="inbound"):
    conversation = cascade_live.hermes_conversation_for(
        CONFIG, call_id="MZturns", token="t", transport=transport)
    return CascadeLiveSession(
        twilio_ws=ws, stream_sid="MZturns", config=CONFIG, profile=None,
        recorder=FakeRecorder(), env=ENV, stt=stt or FakeDeepgram(),
        detector=TurnDetector(), transport=transport,
        tts_connect=http_tts_connect(transport), hindsight_url="",
        hermes_conversation=conversation, direction=direction)


def reply(*timed_sentences):
    """[(at_s, text), ...] -> timed SSE frames ending cleanly after the last one."""
    frames = [(at, _frame(text)) for at, text in timed_sentences]
    last = timed_sentences[-1][0]
    return frames + [(last, _frame(finish="stop", usage=USAGE)), (last, DONE)]


async def listen_only(session, ws, seconds):
    """Drive the media stream WITHOUT the opener turn, so a test starts mid-call."""
    async def consume():
        await session._stt.start()
        async for kind, payload in session._wire.events():
            if kind == "media":
                await session._on_media(payload)
            elif kind == "mark":
                session._on_mark(payload)
    ws.t0 = time.monotonic()
    task = asyncio.create_task(consume())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    await session.teardown()


async def run_call(session, ws, seconds):
    async def drive():
        await session.run()
    task = asyncio.create_task(drive())
    await asyncio.sleep(seconds)
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    await session.teardown()


# ---------------------------------------------------------------------------------------
# 1. A stale mark must not switch barge-in off for the rest of the reply.
# ---------------------------------------------------------------------------------------

def test_a_caller_talking_over_the_second_burst_stops_it_within_a_quarter_second():
    """Report 2.3 mechanism 1, scenario s1. The opener speaks one sentence, pauses long
    enough that its burst ends and its mark goes out, then speaks a long second
    sentence. Twilio echoes the first mark AFTER the second burst has started. The
    caller cuts in about 1 s into the second burst.

    Sabotage: make ``_on_mark`` clear ``_playing`` for any mark (the deployed code):
    the echo of mark 1 lands mid-burst-2, barge-in is off, no ``clear`` is sent and the
    agent talks on to the end of the reply (about 4 s)."""
    long = ("That is the first thing, and now here is a much longer second sentence "
            "that keeps going for quite a while so you can cut in.")
    transport, log = world([reply((0.0, "Hi, glad you called, go on. "), (2.3, long))])
    # Sentence 1 plays ~0.05-1.95 s; its burst ends ~2.2 s and mark 1 is echoed ~2.55 s,
    # after burst 2 started (~2.35 s). The caller talks at 3.0-3.6 s.
    ws = TwilioSim([(3.0, QUIET), (0.6, LOUD), (2.0, QUIET)], lag=0.35)
    session = session_for(transport, ws)
    asyncio.run(run_call(session, ws, 5.4))

    marks = [(t, o["mark"]["name"]) for t, o in ws.sent if o.get("event") == "mark"]
    assert marks and marks[0][1] == "utt-1" and marks[0][0] < 2.35, marks
    burst2 = min(t for t, o in ws.sent if o.get("event") == "media" and t > marks[0][0])
    assert burst2 < marks[0][0] + ws.lag, "burst 2 must start before mark 1 is echoed"
    onset = 3.0
    clears = [t for t in ws.clears() if t > onset]
    assert clears, "no clear was sent when the caller talked over the second burst"
    assert clears[0] - onset < 0.4, clears[0] - onset
    assert ws.audible_after(onset) < 0.45, ws.audible_after(onset)


def test_only_the_mark_of_the_burst_now_playing_ends_playback():
    """The same guard, unit-sized. Sabotage as above: red."""
    session = session_for(world([])[0], TwilioSim([]))
    session._mark_seq, session._playing, session._burst_open = 2, True, True
    session._on_mark(1)                       # the echo of an earlier burst
    assert session._playing
    session._burst_open = False
    session._on_mark(2)
    assert not session._playing
    session._playing = True
    session._on_mark(None)                    # a mark this engine never sent
    assert session._playing


# ---------------------------------------------------------------------------------------
# 2. Barge-in in every detector state while the agent is audible.
# ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("state", ["speech", "pending"])
def test_a_caller_already_talking_when_the_agent_starts_can_still_stop_it(state):
    """Report 2.3 mechanism 2: the caller started before the agent (into a filler, or a
    reply that began over them), so the detector is not idle. Sabotage: only barge from
    idle (the deployed state machine) - no barge_in event, red."""
    det = TurnDetector()
    for _ in range(4):
        det.feed(LOUD, agent_playing=False)
    if state == "pending":
        for _ in range(31):
            det.feed(QUIET, agent_playing=False)
    assert det.state == state
    events = [e for _ in range(8) for e in det.feed(LOUD, agent_playing=True).events]
    assert "barge_in" in events


def test_echo_level_audio_does_not_barge_from_speech_either():
    det = TurnDetector()
    for _ in range(4):
        det.feed(LOUD, agent_playing=False)
    medium = _mulaw(400)                     # over the base threshold, under the raised one
    events = [e for _ in range(20) for e in det.feed(medium, agent_playing=True).events]
    assert "barge_in" not in events


# ---------------------------------------------------------------------------------------
# 3. Nothing starts while the caller is mid-utterance.
# ---------------------------------------------------------------------------------------

def test_the_answer_waits_for_a_caller_who_is_still_talking():
    """Report 2.3 mechanism 3 / scenario s2: the answer is ready while the caller is
    still talking, so it waits until they stop instead of starting over them.

    Sabotage: make ``_hold_for_caller`` return at once - the answer starts at 0.3 s,
    over the caller, red."""
    transport, log = world([reply((0.3, "Sure, I can help you with that one."))])
    ws = TwilioSim([(0.2, QUIET), (1.2, LOUD), (2.5, QUIET)])
    session = session_for(transport, ws)
    asyncio.run(run_call(session, ws, 3.6))
    assert ws.audible_between(0.2, 1.4) == 0.0, "the agent spoke over the caller"
    assert ws.audible_after(1.4) > 0.5, "the answer never came"


# ---------------------------------------------------------------------------------------
# 4. The filler speaks only on a real wait, and a filler cut short is cleared.
# ---------------------------------------------------------------------------------------

def test_a_turn_that_answers_before_the_wait_gets_no_filler(monkeypatch):
    """Report 2.3 mechanism 5: Hermes usually answers in 2.5-3.7 s and the filler fired
    at 2 s, so nearly every turn began "One sec, let me-". Scaled: the wait is 0.5 s and
    the answer comes at 0.3 s. Sabotage: start the filler on the old fixed debounce
    (0.1 s here) - "One sec" is spoken, red."""
    monkeypatch.setattr(cascade_live, "HERMES_FILLER_WAIT_S", 0.5)
    transport, log = world([reply((0.3, "It is half past three in the afternoon."))])
    session = session_for(transport, TwilioSim([]))
    session._filler_debounce_s = 0.1
    session.messages.append({"role": "user", "content": "what time is it"})
    asyncio.run(session._agent_turn())
    assert log["spoken"] == ["It is half past three in the afternoon."]


def test_tool_progress_is_a_real_wait_and_gets_the_filler_at_once(monkeypatch):
    """A turn that starts a tool is a real wait: the filler starts on the progress
    frame, long before HERMES_FILLER_WAIT_S. Sabotage: ignore ``tool_progress`` - no
    filler before the answer, red."""
    monkeypatch.setattr(cascade_live, "HERMES_FILLER_WAIT_S", 5.0)
    turn = [(0.05, _progress())] + reply((1.2, "Your latest email is from GitHub."))
    transport, log = world([turn])
    session = session_for(transport, TwilioSim([]))
    session.messages.append({"role": "user", "content": "check my email"})
    asyncio.run(session._agent_turn())
    assert log["spoken"] == ["One sec, let me check that.",
                             "Your latest email is from GitHub."]


def test_a_filler_cut_by_the_answer_is_cleared_off_the_wire(monkeypatch):
    """"One sec, let me- Take your time": the audio already handed to Twilio played on
    into the answer. Sabotage: drop the ``_clear_wire`` in ``_cancel_filler`` - no clear
    between the filler and the answer, red."""
    monkeypatch.setattr(cascade_live, "HERMES_FILLER_WAIT_S", 0.05)
    monkeypatch.setattr(cascade_live, "HERMES_OPENER_FILLER", "One moment " * 12)
    transport, log = world([reply((0.9, "Hello, how can I help?"))])
    ws = TwilioSim([(2.5, QUIET)])
    session = session_for(transport, ws)
    asyncio.run(run_call(session, ws, 2.6))
    assert log["spoken"][0].startswith("One moment")
    assert log["spoken"][1] == "Hello, how can I help?"
    assert [t for t in ws.clears() if 0.85 < t < 1.6], ws.clears()


# ---------------------------------------------------------------------------------------
# 5. A resumed utterance is merged into the turn in flight, not answered a turn late.
# ---------------------------------------------------------------------------------------

class ScriptedSTT(FakeDeepgram):
    """Utterances in order, with Scribe's own verdict on each."""

    def turn_verdict(self, text):
        from voicecore import elevenlabs_live
        return elevenlabs_live.turn_verdict(text)


def test_the_caller_resuming_before_the_answer_is_merged_into_one_turn():
    """Report 2.3 mechanism 4: "I just" became its own Hermes turn and was answered
    after the caller had already said the rest. Here the caller pauses, Hermes starts on
    the fragment, the caller resumes, and the fragment turn is withdrawn: its stream is
    closed and ONE turn answers everything, told why.

    Sabotage: make ``_mergeable`` return False - two Hermes turns are answered, the
    first one to "I just", red. Also red before the fix to ``_note_hermes_interruption``:
    a withdrawn answer nobody heard was recorded as a barge-in."""
    transport, log = world([
        # The fragment's answer is ready while the caller is talking again: it is handed
        # to TTS and held, never heard, then withdrawn.
        reply((0.5, "What were you going to say there?")),
        reply((0.3, "Yes, I can hear you fine.")),
    ])
    stt = ScriptedSTT(["I just", "can you hear me?"])
    ws = TwilioSim([(0.3, QUIET), (0.5, LOUD), (0.7, QUIET), (0.6, LOUD), (2.4, QUIET)])
    session = session_for(transport, ws, stt=stt)
    asyncio.run(listen_only(session, ws, 4.3))

    assert log["spoken"][-1] == "Yes, I can hear you fine."
    assert ws.audible_between(0.0, 2.7) == 0.0, "the withdrawn answer was heard"
    assert len(log["hermes"]) == 2
    first, second = (b["messages"][-1]["content"] for b in log["hermes"])
    assert first == "I just"
    # Nothing of the withdrawn answer was heard, so Hermes is told about the merge only,
    # and the words it answers are the caller's (the replay caught this sending Hermes an
    # "(interrupted)" assistant note instead).
    assert second == ("(You were cut off before answering: the caller had not finished. "
                      "Everything they said:)\nI just can you hear me?")
    assert not [m for m in session.messages
                if "What were you going" in (m.get("content") or "")]
    assert log["closed"], "the withdrawn turn's stream was never closed"
    assert [line for line in session.transcript if line.startswith("Them:")] == [
        "Them: I just can you hear me?"]


def test_a_turn_that_started_a_tool_is_never_withdrawn():
    """Cancelling a turn that has acted could leave an action half done: after tool
    progress the new words queue as the next turn instead. Sabotage: drop the
    tool-progress condition from ``_mergeable`` - the email turn is cancelled, red."""
    transport, log = world([
        [(0.1, _progress())] + reply((1.4, "You have two new emails.")),
        reply((0.2, "Sure, anything else?")),
    ])
    stt = ScriptedSTT(["check my email", "thanks"])
    ws = TwilioSim([(0.3, QUIET), (0.5, LOUD), (0.7, QUIET), (0.4, LOUD), (4.6, QUIET)])
    session = session_for(transport, ws, stt=stt)
    asyncio.run(listen_only(session, ws, 6.0))
    assert "You have two new emails." in log["spoken"]
    assert [b["messages"][-1]["content"] for b in log["hermes"]] == [
        "check my email", "thanks"]


# ---------------------------------------------------------------------------------------
# 6. The provider's end-of-turn signal: a trailing-off utterance re-arms the window.
# ---------------------------------------------------------------------------------------

def test_scribe_marking_speech_as_unfinished_keeps_listening():
    """Scribe wrote "I just..." for the owner's mid-thought pause. The engine re-arms
    and answers the whole utterance once it ends. Sabotage: ignore ``turn_verdict`` -
    "I just..." is answered on its own, red."""
    transport, log = world([reply((0.1, "Yes, loud and clear."))])
    stt = ScriptedSTT(["I just...", "can you hear me?"])
    ws = TwilioSim([(0.2, QUIET), (0.5, LOUD), (0.7, QUIET), (0.5, LOUD), (1.6, QUIET)])
    session = session_for(transport, ws, stt=stt)
    asyncio.run(listen_only(session, ws, 3.4))
    assert [b["messages"][-1]["content"] for b in log["hermes"]] == [
        "I just... can you hear me?"]
    assert session.transcript[0] == "Them: I just... can you hear me?"


# ---------------------------------------------------------------------------------------
# 7. One TTS stream per reply, opened while Hermes thinks.
# ---------------------------------------------------------------------------------------

def test_a_whole_reply_is_one_tts_stream_opened_before_the_first_sentence():
    """Report 2.3 mechanism 6: one HTTP request per sentence left ~0.6 s gaps that
    invited the caller in. Sabotage: speak each sentence with its own ``_speak`` - three
    sockets, red."""
    sockets: list = []
    transport, log = world([reply((0.4, "First sentence of the reply here. "),
                                  (0.5, "Second sentence right after it. "),
                                  (0.6, "And a third to finish."))])
    conversation = cascade_live.hermes_conversation_for(
        CONFIG, call_id="MZone", token="t", transport=transport)
    session = CascadeLiveSession(
        twilio_ws=TwilioSim([]), stream_sid="MZone", config=CONFIG, profile=None,
        recorder=FakeRecorder(), env=ENV, stt=FakeDeepgram(), transport=transport,
        tts_connect=http_tts_connect(transport, sockets), hindsight_url="",
        hermes_conversation=conversation, direction="inbound")
    session.messages.append({"role": "user", "content": "tell me three things"})
    session._wire._ws.t0 = time.monotonic()
    opened: list = []
    real_open = session._open_tts

    async def spy_open():
        stream = await real_open()
        opened.append(time.monotonic() - session._wire._ws.t0)
        return stream
    session._open_tts = spy_open
    asyncio.run(session._agent_turn())
    assert len(sockets) == 1
    assert log["spoken"] == ["First sentence of the reply here.",
                             "Second sentence right after it.", "And a third to finish."]
    assert opened and opened[0] < 0.4, "the socket was not opened while Hermes thought"


def test_a_barge_clears_the_wire_before_the_cut_turn_finishes_closing():
    """The cut turn closes its TTS websocket and its Hermes stream, and a websocket close
    is a network handshake. The caller must stop hearing the agent before that, not
    after. Sabotage: send ``clear`` after awaiting the cancelled turn - the clear waits
    out the slow close, red."""
    import tts_fake
    transport, log = world([reply((0.0, "Here is a long answer that goes on and on for "
                                         "a good while so there is time to cut in."))])

    class SlowClose(tts_fake.HttpBackedTTSSocket):
        async def close(self):
            await asyncio.sleep(0.5)
            await super().close()

    async def connect(url, additional_headers=None):
        return SlowClose(transport, url)

    ws = TwilioSim([])
    conversation = cascade_live.hermes_conversation_for(
        CONFIG, call_id="MZslow", token="t", transport=transport)
    session = CascadeLiveSession(
        twilio_ws=ws, stream_sid="MZslow", config=CONFIG, profile=None,
        recorder=FakeRecorder(), env=ENV, stt=FakeDeepgram(), transport=transport,
        tts_connect=connect, hindsight_url="", hermes_conversation=conversation,
        direction="inbound")
    session.messages.append({"role": "user", "content": "tell me everything"})

    async def run():
        session._turn_task = asyncio.create_task(session._agent_turn())
        while not [o for _, o in ws.sent if o.get("event") == "media"]:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.8)
        started = ws.now()
        await session._handle_barge_in()
        return started
    started = asyncio.run(run())
    assert ws.clears() and ws.clears()[0] - started < 0.1, (started, ws.clears())
    heard = [m["content"] for m in session.messages if m["role"] == "assistant"]
    assert heard and heard[0].startswith("Here is") and "interrupted" in heard[0]


# ---------------------------------------------------------------------------------------
# 8. Review of PR 32: B1 + S1 - a word said in the gap, then a barge over the next burst.
# ---------------------------------------------------------------------------------------

class ScribeOverRule:
    """The real Scribe wire rules, played locally: a commit over less than 0.3 s of
    uncommitted audio is `commit_throttled` and CLOSES the session (measured live); a
    commit that holds loud caller audio is answered with the next scripted transcript."""

    def __init__(self, transcripts):
        self.transcripts = list(transcripts)
        self.sent = []
        self.throttled = False
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._audio = b""

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if self.throttled:
            raise ConnectionError("closed")
        self._audio += base64.b64decode(msg["audio_base_64"])
        if not msg["commit"]:
            return
        audio, self._audio = self._audio, b""
        if len(audio) < 2400:
            self.throttled = True
            self._inbox.put_nowait(json.dumps({"message_type": "commit_throttled",
                                               "error": "under 0.3s"}))
            self._inbox.put_nowait(None)
            return
        loud = sum(1 for i in range(0, len(audio) - 159, 160)
                   if audioop.rms(audioop.ulaw2lin(audio[i:i + 160], 2), 2) > 2000)
        text = self.transcripts.pop(0) if loud >= 5 and self.transcripts else ""
        self._inbox.put_nowait(json.dumps({"message_type": "committed_transcript",
                                           "text": text}))

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        while True:
            item = await self._inbox.get()
            if item is None:
                return
            yield item

    async def close(self):
        self._inbox.put_nowait(None)


def test_a_word_in_the_gap_then_a_barge_is_one_utterance_and_scribe_stays_open():
    """The reviewer's scenario. The caller says "Wait." in the pause between two agent
    sentences (queued: the opener is still in flight), then talks over the second one.

    Before: the barge from `pending` reset the STT, dropping "Wait."; draining the queued
    turn mid-sentence ended the caller's turn early and let the agent start over them;
    and the drain committed 20 ms of audio, which closes a Scribe session, so the rest
    of the call was deaf. Now "Wait." and "No, stop." are one utterance, the agent does
    not start over the caller, and the session stays open for the next question.

    Sabotage: reset the STT on a barge from speech/pending (S1) - "Wait." is lost, red;
    keep the queued drain (S1) - a turn starts on half the utterance and a merge has to
    withdraw it, red. The padding (B1) is
    bound by test_elevenlabs_live's rule-enforcing socket."""
    from voicecore import elevenlabs_live
    long = ("Here is a much longer second sentence that keeps on going for a good long "
            "while so the caller has time to talk over it.")
    transport, log = world([
        reply((0.0, "Hi, glad you called, go on. "), (3.6, long)),
        reply((0.3, "Okay, stopping there.")),
        reply((0.3, "Yes, I am still here.")),
    ])
    sock = ScribeOverRule(["Wait. No, stop.", "Hello, are you there?"])

    async def connect(url, additional_headers=None):
        return sock
    stt = elevenlabs_live.ScribeLive("k", connect=connect)
    # "Wait." 2.4-2.8 s in the gap; "No, stop." over the second sentence, 4.4-5.6 s;
    # "Hello, are you there?" over the answer, 7.1-7.7 s.
    ws = TwilioSim([(2.4, QUIET), (0.4, LOUD), (1.6, QUIET), (1.2, LOUD), (1.5, QUIET),
                    (0.6, LOUD), (2.0, QUIET)], lag=0.1)
    session = session_for(transport, ws, stt=stt)
    asyncio.run(run_call(session, ws, 10.0))

    assert not sock.throttled, "a commit short enough to close the Scribe session"
    inputs = [b["messages"][-1]["content"] for b in log["hermes"]]
    # Exactly three Hermes turns - the opener, the whole interruption, the question -
    # and no withdrawn one: draining the queued turn mid-sentence used to start a turn
    # on half an utterance that a merge then had to take back.
    assert len(inputs) == 3, inputs
    assert not [i for i in inputs if "cut off before answering" in i], inputs
    assert inputs[1].endswith("Wait. No, stop."), inputs
    assert inputs[2].endswith("Hello, are you there?"), inputs
    # After the barge at ~4.55 s the agent stays quiet until the caller has finished.
    assert ws.audible_between(4.75, 6.2) == 0.0


# ---------------------------------------------------------------------------------------
# 9. Review S2: an STT that is lost is reported, and the call knows it went deaf.
# ---------------------------------------------------------------------------------------

def test_a_lost_stt_session_is_reported_to_the_event_log_and_marks_the_call():
    from voicecore import elevenlabs_live
    reported: list = []

    class Rec(FakeRecorder):
        def record_stt(self, **kw):
            reported.append(kw)

    sockets: list = []

    async def connect(url, additional_headers=None):
        sock = ScribeOverRule([])
        sockets.append(sock)
        return sock
    stt = elevenlabs_live.ScribeLive("k", connect=connect)
    transport, _ = world([reply((0.0, "Hello there, how can I help today?"))])
    ws = TwilioSim([(1.5, QUIET)])
    conversation = cascade_live.hermes_conversation_for(
        CONFIG, call_id="MZlost", token="t", transport=transport)
    session = CascadeLiveSession(
        twilio_ws=ws, stream_sid="MZlost", config=CONFIG, profile=None, recorder=Rec(),
        env=ENV, stt=stt, transport=transport, tts_connect=http_tts_connect(transport),
        hindsight_url="", hermes_conversation=conversation, direction="inbound")

    async def run():
        task = asyncio.create_task(session.run())
        await asyncio.sleep(0.2)
        sockets[0]._inbox.put_nowait(None)          # the server ends the session
        await asyncio.sleep(0.2)
        sockets[1]._inbox.put_nowait(None)          # and the reconnected one
        await asyncio.sleep(0.2)
        task.cancel()
        await session.teardown()
    asyncio.run(run())
    assert [r["event"] for r in reported] == ["reconnected", "lost"]
    assert reported[0]["provider"] == "deepgram"     # CONFIG's stt stage names it
    assert session.stt_lost


# ---------------------------------------------------------------------------------------
# 10. Review S5: Hermes can speak through Deepgram Aura too.
# ---------------------------------------------------------------------------------------

def test_hermes_speaks_through_aura_sentence_by_sentence():
    aura_config = dict(CONFIG, tts={"provider": "deepgram-aura",
                                    "secret_env": "DEEPGRAM_API_KEY",
                                    "voice": "aura-2-thalia-en", "speed": None})
    spoken: list = []

    def handler(request):
        if request.url.host == "hermes.test":
            return httpx.Response(200, stream=TimedStream(
                reply((0.0, "The first sentence of the answer is here. "),
                      (0.1, "And the second one follows it."))),
                headers={"Content-Type": "text/event-stream"})
        if request.url.host == "api.deepgram.com":
            spoken.append((request.url.params.get("model"),
                           json.loads(request.content)["text"]))
            return httpx.Response(200, stream=TimedStream([(0.0, AGENT * (FRAME * 10))]))
        raise AssertionError(request.url.host)
    transport = httpx.MockTransport(handler)
    conversation = cascade_live.hermes_conversation_for(
        aura_config, call_id="MZaura", token="t", transport=transport)
    ws = TwilioSim([])
    session = CascadeLiveSession(
        twilio_ws=ws, stream_sid="MZaura", config=aura_config, profile=None,
        recorder=FakeRecorder(), env=ENV, stt=FakeDeepgram(), transport=transport,
        hindsight_url="", hermes_conversation=conversation, direction="inbound")
    session.messages.append({"role": "user", "content": "tell me two things"})
    asyncio.run(session._agent_turn())
    assert spoken == [("aura-2-thalia-en", "The first sentence of the answer is here."),
                      ("aura-2-thalia-en", "And the second one follows it.")]
    assert [o for _, o in ws.sent if o.get("event") == "media"]
