"""Ticket 21: the ElevenLabs clients the direct lane hears and speaks through.

The wire shapes here were checked against the live API on 2026-09-23 (Scribe realtime on
the owner's recorded caller leg, stream-input TTS with two short sentences), not only
against the docs. The ``turn_verdict`` cases are Scribe's own output for that call.
"""
import asyncio
import base64
import json
import urllib.parse

import pytest

from voicecore import elevenlabs_live
from voicecore.elevenlabs_live import ScribeLive, TTSStream, turn_verdict


class FakeSocket:
    """A websocket: records what was sent, replays scripted replies. ``on_send`` maps a
    sent message to the replies it should cause."""

    def __init__(self, on_send=None):
        self.sent: list = []
        self.closed = False
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._on_send = on_send or (lambda msg: [])

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        for reply in self._on_send(msg):
            self._inbox.put_nowait(json.dumps(reply))

    def push(self, reply):
        self._inbox.put_nowait(json.dumps(reply))

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        while True:
            item = await self._inbox.get()
            if item is None:
                return
            yield item

    async def close(self):
        self.closed = True
        self._inbox.put_nowait(None)


def connector(sock, seen=None):
    async def connect(url, additional_headers=None):
        if seen is not None:
            seen.append((url, additional_headers))
        return sock
    return connect


# --------------------------------------------------------------------- verdict --

@pytest.mark.parametrize("text, verdict", [
    ("Um...", "incomplete"),
    ("I just...", "incomplete"),
    ("Can you...", "incomplete"),
    ("Well, okay. Uh, listen, uh, are you waiting for-", "incomplete"),
    ("Hmm, that is good.", "complete"),
    ("Can you hear me?", "complete"),
    ("No, you stupid bitch. That 14-day free trial.", "complete"),
    ("How do you know where to check my resume? They said so. Sure.", "complete"),
    ("so I was thinking,", "incomplete"),
    ("", None),
])
def test_scribe_marks_speech_that_trailed_off(text, verdict):
    assert turn_verdict(text) == verdict


# ---------------------------------------------------------------------- Scribe --

def test_the_scribe_url_asks_for_the_wire_format_and_a_manual_commit():
    seen: list = []
    sock = FakeSocket()
    stt = ScribeLive("xi-secret", language="en", keyterms=["Hermes", "Alex"],
                     connect=connector(sock, seen))

    async def run():
        await stt.start()
        await stt.close()
    asyncio.run(run())
    url, headers = seen[0]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert url.startswith("wss://api.elevenlabs.io/v1/speech-to-text/realtime?")
    assert query["model_id"] == ["scribe_v2_realtime"]
    assert query["audio_format"] == ["ulaw_8000"]
    assert query["commit_strategy"] == ["manual"]
    assert query["language_code"] == ["en"]
    assert query["keyterms"] == ["Hermes", "Alex"]
    assert headers == {"xi-api-key": "xi-secret"}
    assert "xi-secret" not in url


def test_talk_audio_is_sent_as_pcm_24000():
    seen: list = []
    stt = ScribeLive("k", wire_format="pcm_24k", connect=connector(FakeSocket(), seen))
    asyncio.run(stt.start())
    assert "audio_format=pcm_24000" in seen[0][0]


def test_twenty_ms_frames_are_batched_into_100_ms_chunks():
    sock = FakeSocket()
    stt = ScribeLive("k", connect=connector(sock))

    async def run():
        await stt.start()
        for _ in range(12):
            await stt.feed(b"\x7f" * 160)
        await stt.close()
    asyncio.run(run())
    chunks = [m for m in sock.sent if m["message_type"] == "input_audio_chunk"]
    assert len(chunks) == 2
    assert all(len(base64.b64decode(m["audio_base_64"])) == 800 for m in chunks)
    assert all(m["commit"] is False and m["sample_rate"] == 8000 for m in chunks)


def test_take_utterance_commits_the_tail_and_returns_the_committed_text():
    def on_send(msg):
        return ([{"message_type": "committed_transcript", "text": "Can you hear me?"}]
                if msg["commit"] else [])
    sock = FakeSocket(on_send)
    stt = ScribeLive("k", connect=connector(sock))

    async def run():
        await stt.start()
        await stt.feed(b"\x7f" * 480)                     # 60 ms: stays buffered
        text = await stt.take_utterance()
        await stt.close()
        return text
    assert asyncio.run(run()) == "Can you hear me?"
    commit = sock.sent[-1]
    assert commit["commit"] is True
    audio = base64.b64decode(commit["audio_base_64"])
    assert audio[:480] == b"\x7f" * 480                   # the tail rides along...
    assert len(audio) == 2800 and set(audio[480:]) == {0xFF}   # ...padded to 0.35 s


def test_a_reset_drops_the_transcript_of_the_audio_before_it():
    """A barge-in: the speech before it was echo or a cut reply. Its segment is
    committed and thrown away, and the next utterance starts clean."""
    replies = iter(["stale words", "the real question?"])

    def on_send(msg):
        return ([{"message_type": "committed_transcript", "text": next(replies)}]
                if msg["commit"] else [])
    sock = FakeSocket(on_send)
    stt = ScribeLive("k", connect=connector(sock))

    async def run():
        await stt.start()
        await stt.feed(b"\x7f" * 800)
        stt.reset()
        await stt.feed(b"\x7f" * 800)
        text = await stt.take_utterance()
        await stt.close()
        return text
    assert asyncio.run(run()) == "the real question?"


def test_a_reset_with_nothing_sent_drops_nothing():
    """Dropping "the next commit" when there was nothing to commit would drop the
    caller's real utterance instead."""
    def on_send(msg):
        return ([{"message_type": "committed_transcript", "text": "hello?"}]
                if msg["commit"] else [])
    sock = FakeSocket(on_send)
    stt = ScribeLive("k", connect=connector(sock))

    async def run():
        await stt.start()
        stt.reset()
        await stt.feed(b"\x7f" * 800)
        text = await stt.take_utterance()
        await stt.close()
        return text
    assert asyncio.run(run()) == "hello?"


def test_an_unanswered_commit_returns_what_there_is_inside_the_wait():
    sock = FakeSocket()
    stt = ScribeLive("k", connect=connector(sock))

    async def run():
        await stt.start()
        await stt.feed(b"\x7f" * 800)
        text = await stt.take_utterance(finalize_wait_s=0.05)
        await stt.close()
        return text
    assert asyncio.run(run()) == ""


def test_silence_keeps_the_socket_alive_while_the_echo_gate_withholds_audio():
    sock = FakeSocket()
    stt = ScribeLive("k", connect=connector(sock), keepalive_s=0.05)

    async def run():
        await stt.start()
        await asyncio.sleep(0.2)
        await stt.close()
    asyncio.run(run())
    silence = [m for m in sock.sent if not m["commit"]]
    assert silence
    assert set(base64.b64decode(silence[0]["audio_base_64"])) == {0xFF}   # μ-law silence


def test_an_error_message_is_logged_never_raised(caplog):
    sock = FakeSocket()
    stt = ScribeLive("k", connect=connector(sock))

    async def run():
        await stt.start()
        sock.push({"message_type": "quota_exceeded", "error": "You have exceeded"})
        await asyncio.sleep(0.05)
        await stt.close()
    asyncio.run(run())
    assert "quota_exceeded" in caplog.text


# ------------------------------------------------------------------------- TTS --

def _tts_socket():
    def on_send(msg):
        text = msg.get("text")
        if msg.get("flush"):
            return [{"audio": base64.b64encode(b"\xff" * 320).decode()}]
        if text == "":
            return [{"isFinal": True}]
        return []
    return FakeSocket(on_send)


def test_one_reply_is_one_socket_and_every_sentence_is_flushed():
    seen: list = []
    sock = _tts_socket()
    tts = TTSStream(api_key="xi-secret", voice="voice-1", output_format="ulaw_8000",
                    speed=1.1, connect=connector(sock, seen))

    async def run():
        await tts.open()
        await tts.send("First sentence.")
        await tts.send("Second sentence.")
        await tts.finish()
        return [chunk async for chunk in tts.audio()]
    chunks = asyncio.run(run())
    url, headers = seen[0]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert url.startswith("wss://api.elevenlabs.io/v1/text-to-speech/voice-1/stream-input?")
    assert query["model_id"] == ["eleven_flash_v2_5"]
    assert query["output_format"] == ["ulaw_8000"]
    assert headers == {"xi-api-key": "xi-secret"}
    assert sock.sent[0] == {"text": " ", "voice_settings": {"speed": 1.1}}
    assert sock.sent[1:] == [{"text": "First sentence. ", "flush": True},
                             {"text": "Second sentence. ", "flush": True},
                             {"text": ""}]
    assert chunks == [b"\xff" * 320, b"\xff" * 320]


def test_an_error_from_elevenlabs_fails_the_reply_loudly():
    sock = FakeSocket(lambda msg: [{"message": "voice not found", "error": "not_found"}]
                      if msg.get("flush") else [])
    tts = TTSStream(api_key="k", voice="v", connect=connector(sock))

    async def run():
        await tts.open()
        await tts.send("Hello.")
        return [chunk async for chunk in tts.audio()]
    with pytest.raises(RuntimeError, match="voice not found"):
        asyncio.run(run())


def test_a_socket_that_goes_quiet_after_text_was_sent_is_a_stall_not_a_hang(monkeypatch):
    """Without this a dead socket held the turn forever: no audio, no apology."""
    monkeypatch.setattr(elevenlabs_live, "TTS_STALL_S", 0.2)
    monkeypatch.setattr(elevenlabs_live, "STALL_POLL_S", 0.05)
    tts = TTSStream(api_key="k", voice="v", connect=connector(FakeSocket()))

    async def run():
        await tts.open()
        await tts.send("Hello.")
        return [chunk async for chunk in tts.audio()]
    with pytest.raises(RuntimeError, match="sent nothing"):
        asyncio.run(asyncio.wait_for(run(), 3))


def test_waiting_on_hermes_mid_reply_is_not_a_stall(monkeypatch):
    """Nothing outstanding (all sent text was answered): the reply may be waiting on a
    Hermes tool call, which has its own budget. Only text with no reply is a stall."""
    monkeypatch.setattr(elevenlabs_live, "TTS_STALL_S", 0.1)
    monkeypatch.setattr(elevenlabs_live, "STALL_POLL_S", 0.02)
    sock = _tts_socket()
    tts = TTSStream(api_key="k", voice="v", connect=connector(sock))

    async def run():
        await tts.open()
        await tts.send("Let me check.")

        async def later():
            await asyncio.sleep(0.4)                 # a long Hermes pause
            await tts.send("Found it.")
            await tts.finish()
        task = asyncio.create_task(later())
        out = [chunk async for chunk in tts.audio()]
        await task
        return out
    assert len(asyncio.run(run())) == 2


# ------------------------------------------------------------ construction --

def _registry():
    from voicecore import profiles
    return profiles.load_registry()


def _direct_doc(stt):
    return {"id": "a", "pipeline": "cascade", "hermes_profile": "default",
            "providers": {"stt": stt, "llm": "hermes-agent", "tts": "elevenlabs"}}


def test_scribe_is_a_live_stt_and_defaults_to_english():
    from voicecore import cascade_config
    config = cascade_config.build_cascade_config(_direct_doc("elevenlabs-scribe"),
                                                 _registry(), {})
    assert config["stt"]["wired_live"]
    assert config["stt"]["model"] == "scribe_v2_realtime"
    assert config["stt"]["language"] == "en"
    assert config["stt"]["secret_env"] == "ELEVENLABS_API_KEY"
    assert config["tts"]["model"] == "eleven_flash_v2_5"


def test_deepgram_stays_selectable_and_now_defaults_to_english_too():
    """`multi` heard "fourteen six" as "Diez dos" on the owner's call."""
    from voicecore import cascade_config
    config = cascade_config.build_cascade_config(_direct_doc("deepgram"), _registry(), {})
    assert config["stt"]["wired_live"] and config["stt"]["language"] == "en"


def test_one_construction_picks_the_client_by_provider_and_wire_format():
    from voicecore import cascade_config, cascade_live, deepgram_live
    env = {"ELEVENLABS_API_KEY": "xi", "DEEPGRAM_API_KEY": "dg"}
    scribe = cascade_live.open_stt(
        cascade_config.build_cascade_config(_direct_doc("elevenlabs-scribe"), _registry(),
                                            env), env, cascade_config.PCM_24K)
    assert isinstance(scribe, ScribeLive)
    assert scribe._api_key == "xi" and "audio_format=pcm_24000" in scribe._url
    deepgram = cascade_live.open_stt(
        cascade_config.build_cascade_config(_direct_doc("deepgram"), _registry(), env),
        env, cascade_config.MULAW_8K)
    assert isinstance(deepgram, deepgram_live.DeepgramLive)
    assert "encoding=mulaw" in deepgram._url and "language=en" in deepgram._url


def test_a_deepgram_model_left_on_an_agent_is_never_sent_to_scribe():
    from voicecore import cascade_config
    doc = dict(_direct_doc("elevenlabs-scribe"), knobs={"transcription_model": "nova-3"})
    config = cascade_config.build_cascade_config(doc, _registry(), {})
    assert config["stt"]["model"] == "scribe_v2_realtime"


# ------------------------------------------------- Scribe's commit rule (B1) --

class ScribeRuleSocket(FakeSocket):
    """Scribe as measured live on 2026-09-23: a commit over less than 0.3 s of
    uncommitted audio gets `commit_throttled` and the session is CLOSED."""

    MIN_BYTES = 2400                                  # 0.3 s of ulaw_8000

    def __init__(self, text="heard"):
        super().__init__()
        self.uncommitted = 0
        self.throttled = False
        self._text = text

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if self.throttled:
            raise ConnectionError("socket closed")
        self.uncommitted += len(base64.b64decode(msg["audio_base_64"]))
        if msg["commit"]:
            if self.uncommitted < self.MIN_BYTES:
                self.throttled = True
                self.push({"message_type": "commit_throttled", "error": "only 0.2s"})
                self._inbox.put_nowait(None)
            else:
                self.push({"message_type": "committed_transcript", "text": self._text})
            self.uncommitted = 0


def test_no_commit_is_ever_short_enough_for_scribe_to_close_the_session():
    """B1 (review of PR 32): a commit over 20 ms of audio closed the session and the
    call went deaf. Every commit path - a take after a short tail, the commit a reset
    sends, a take straight after another - is padded to 0.35 s. Sabotage: drop the
    padding in ``ScribeLive._send`` - the first short commit is throttled, red."""
    sock = ScribeRuleSocket()
    stt = ScribeLive("k", connect=connector(sock))

    async def run():
        await stt.start()
        await stt.feed(b"\x7f" * 160)                  # 20 ms, then the turn ends
        first = await stt.take_utterance()
        await stt.feed(b"\x7f" * 800)
        stt.reset()                                     # a barge: commits what was sent
        await stt.feed(b"\x7f" * 160)
        second = await stt.take_utterance()
        await stt.feed(b"\x7f" * 160)
        third = await stt.take_utterance()
        await stt.close()
        return first, second, third
    assert asyncio.run(run()) == ("heard", "heard", "heard")
    assert not sock.throttled


# ------------------------------------------------ sessions that end (S2) --

class DroppingSocket(FakeSocket):
    """A session the server ends (a quota, a time limit) after ``replies`` messages."""

    def __init__(self, error="session_time_limit_exceeded"):
        super().__init__()
        self.error = error

    def end(self):
        self.push({"message_type": self.error, "error": "ended by the server"})
        self._inbox.put_nowait(None)


def _dropping_connector(sockets, *, fail_after=None):
    async def connect(url, additional_headers=None):
        if fail_after is not None and len(sockets) >= fail_after:
            raise ConnectionError("refused")
        sock = DroppingSocket()
        sockets.append(sock)
        return sock
    return connect


def test_a_scribe_session_the_server_ends_is_reconnected_once_and_reported():
    """S2: the socket closing used to leave the call deaf with a traceback per 100 ms
    chunk. Sabotage: make ``_recover`` give up at once - no second socket, red."""
    sockets: list = []
    events: list = []
    stt = ScribeLive("k", connect=_dropping_connector(sockets))
    stt.on_event = events.append

    async def run():
        await stt.start()
        sockets[0].end()
        await asyncio.sleep(0.05)
        await stt.feed(b"\x7f" * 800)                   # the new session hears this
        await stt.close()
    asyncio.run(run())
    assert len(sockets) == 2
    assert [m for m in sockets[1].sent if not m["commit"]], "audio after the reconnect"
    assert events == [{"event": "reconnected", "reason": "session_time_limit_exceeded"}]
    assert not stt.lost


def test_a_scribe_session_lost_twice_is_marked_lost_and_stops_sending(caplog):
    sockets: list = []
    events: list = []
    stt = ScribeLive("k", connect=_dropping_connector(sockets))
    stt.on_event = events.append

    async def run():
        await stt.start()
        sockets[0].end()
        await asyncio.sleep(0.05)
        sockets[1].end()
        await asyncio.sleep(0.05)
        for _ in range(20):
            await stt.feed(b"\x7f" * 800)
        return await stt.take_utterance(finalize_wait_s=5)
    assert asyncio.run(asyncio.wait_for(run(), 2)) == ""   # no 5 s wait on a dead session
    assert stt.lost
    assert [e["event"] for e in events] == ["reconnected", "lost"]
    assert len(sockets) == 2 and not [m for m in sockets[1].sent if m.get("commit")]


def test_a_failed_reconnect_is_lost_at_once():
    sockets: list = []
    events: list = []
    stt = ScribeLive("k", connect=_dropping_connector(sockets, fail_after=1))
    stt.on_event = events.append

    async def run():
        await stt.start()
        sockets[0].end()
        await asyncio.sleep(0.05)
    asyncio.run(run())
    assert stt.lost and events[-1]["event"] == "lost"
    assert "reconnect failed" in events[-1]["reason"]


def test_a_deepgram_stream_that_ends_is_reconnected_once_then_lost():
    """S2 for the other ears: the same rule, and the new stream restarts its clock."""
    from voicecore.deepgram_live import DeepgramLive
    sockets: list = []
    events: list = []

    async def connect():
        sock = FakeSocket()
        sockets.append(sock)
        return sock

    dg = DeepgramLive("k", connect=connect)
    dg.on_event = events.append

    async def run():
        await dg.start()
        await dg.feed(b"\x7f" * 8000)
        sockets[0]._inbox.put_nowait(None)              # the server ends the stream
        await asyncio.sleep(0.05)
        assert dg._audio_sent_s == 0.0                  # a new stream, a new clock
        sockets[1]._inbox.put_nowait(None)
        await asyncio.sleep(0.05)
        await dg.close()
    asyncio.run(run())
    assert len(sockets) == 2
    assert [e["event"] for e in events] == ["reconnected", "lost"]
    assert dg.lost


# ------------------------------------------------ language Scribe takes (S3) --

@pytest.mark.parametrize("language, sent", [
    ("multi", None), ("auto", None), ("english", None), ("", None), (None, None),
    ("en", "en"), ("EN", "en"), ("en-AU", "en"), ("es_MX", "es"), ("yue", "yue"),
])
def test_scribe_is_only_ever_sent_a_language_code_it_accepts(language, sent):
    """S3: Scribe refuses the whole session on 'multi' or 'english' (checked live), and
    an Agent can carry Deepgram's 'multi'. Sabotage: pass the knob through unchanged -
    'multi' reaches the URL, red."""
    from voicecore import cascade_config
    doc = dict(_direct_doc("elevenlabs-scribe"), knobs={"language": language})
    config = cascade_config.build_cascade_config(doc, _registry(), {})
    expected = sent if language else "en"              # blank inherits the registry's en
    assert config["stt"]["language"] == expected
    url = elevenlabs_live.stt_url(language=config["stt"]["language"])
    assert ("language_code=" + expected in url) if expected else "language_code" not in url


def test_deepgram_keeps_its_own_language_vocabulary():
    from voicecore import cascade_config
    doc = dict(_direct_doc("deepgram"), knobs={"language": "multi"})
    assert cascade_config.build_cascade_config(doc, _registry(), {})["stt"]["language"] == "multi"
