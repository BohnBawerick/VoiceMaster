"""s7 c2 units — Deepgram streaming client: Twilio-format feed, assembly semantics,
keepalive during silence, barge-in discard."""
import asyncio
import base64
import json

import pytest

from voicecore import deepgram_live
from voicecore.deepgram_live import DeepgramLive


class FakeWS:
    """Records sends; yields queued inbound messages like a websockets connection."""

    def __init__(self):
        self.sent: list = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True
        await self._queue.put(None)

    def push(self, obj: dict):
        self._queue.put_nowait(json.dumps(obj))

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self._queue.get()
        if msg is None:
            raise StopAsyncIteration
        return msg


def _result(text, *, final=True, start=0.0, duration=1.0):
    return {"type": "Results", "is_final": final, "start": start, "duration": duration,
            "channel": {"alternatives": [{"transcript": text}]}}


def _session(**kw):
    ws = FakeWS()

    async def connect():
        return ws
    dg = DeepgramLive("test-key", connect=connect, **kw)
    return dg, ws


async def _spin():
    await asyncio.sleep(0.01)   # let the recv loop drain the queue


def test_twilio_mulaw_payload_reaches_deepgram_verbatim():
    """The unit starts from a REAL Twilio media payload (base64 μ-law) — the raw
    bytes must hit the socket unchanged, and the stream cursor advances."""
    async def run():
        dg, ws = _session()
        await dg.start()
        payload = base64.b64encode(b"\xff\x7f" * 80).decode()     # 160B = 20ms frame
        await dg.feed(base64.b64decode(payload))
        await dg.close()
        return ws.sent, dg._audio_sent_s
    sent, cursor = asyncio.run(run())
    assert sent[0] == b"\xff\x7f" * 80
    assert cursor == pytest.approx(0.02)


def test_interims_never_stored_finals_join_into_the_utterance():
    async def run():
        dg, ws = _session()
        await dg.start()
        ws.push(_result("hello wor", final=False))
        ws.push(_result("hello world,", start=0.0))
        ws.push(_result("how are you?", start=1.0))
        await _spin()
        text = await dg.take_utterance(finalize_wait_s=0.01)
        empty_after = await dg.take_utterance(finalize_wait_s=0.01)
        await dg.close()
        return text, empty_after
    text, empty_after = asyncio.run(run())
    assert text == "hello world, how are you?"
    assert empty_after == ""                     # segments cleared per utterance


def test_take_utterance_sends_finalize_and_waits_for_the_flushed_final():
    async def run():
        dg, ws = _session()
        await dg.start()
        ws.push(_result("first half", start=0.0))
        await _spin()

        async def flush_later():
            await asyncio.sleep(0.05)
            assert any(json.loads(m).get("type") == "Finalize"
                       for m in ws.sent if isinstance(m, str))
            ws.push(_result("second half", start=1.0))
        flusher = asyncio.create_task(flush_later())
        text = await dg.take_utterance(finalize_wait_s=2.0)
        await flusher
        await dg.close()
        return text
    assert asyncio.run(run()) == "first half second half"


def test_reset_discards_pending_and_inflight_finals_for_old_audio():
    async def run():
        dg, ws = _session()
        await dg.start()
        await dg.feed(b"\x00" * 8000)                # 1s of audio fed
        ws.push(_result("stale one", start=0.0, duration=0.4))
        await _spin()
        dg.reset()                                   # barge-in — cursor at 1.0s
        ws.push(_result("stale two", start=0.2, duration=0.5))   # ends 0.7 ≤ 1.0 → drop
        await dg.feed(b"\x00" * 8000)                # new utterance audio
        ws.push(_result("fresh words", start=1.2, duration=0.6))  # ends 1.8 > 1.0 → keep
        await _spin()
        text = await dg.take_utterance(finalize_wait_s=0.01)
        await dg.close()
        return text
    assert asyncio.run(run()) == "fresh words"


def test_keepalive_flows_while_no_audio_is_fed():
    async def run():
        dg, ws = _session(keepalive_s=0.04)
        await dg.start()
        await asyncio.sleep(0.15)                    # caller silent (echo gate)
        await dg.close()
        return [m for m in ws.sent
                if isinstance(m, str) and json.loads(m).get("type") == "KeepAlive"]
    assert len(asyncio.run(run())) >= 1


def test_close_sends_closestream_and_stops_the_tasks():
    async def run():
        dg, ws = _session()
        await dg.start()
        await dg.close()
        await asyncio.sleep(0.01)
        return ws, dg
    ws, dg = asyncio.run(run())
    assert any(isinstance(m, str) and json.loads(m).get("type") == "CloseStream"
               for m in ws.sent)
    assert ws.closed
    assert dg._recv_task.cancelled() or dg._recv_task.done()


def test_listen_url_carries_the_stream_contract():
    url = deepgram_live.listen_url("nova-3", "multi")
    assert url.startswith("wss://api.deepgram.com/v1/listen?")
    for fragment in ("encoding=mulaw", "sample_rate=8000", "model=nova-3",
                     "language=multi", "interim_results=true"):
        assert fragment in url


def test_listen_url_appends_one_keyterm_param_per_term_verbatim():
    """s8 c4: each keyterm becomes its OWN repeatable ``keyterm`` query param,
    URL-encoded but otherwise verbatim; empty/whitespace terms are dropped."""
    import urllib.parse
    url = deepgram_live.listen_url("nova-3", None,
                                   keyterms=["Hermes", "Robot Sam", "  ", ""])
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["keyterm"] == ["Hermes", "Robot Sam"]     # order kept, blanks dropped
    assert "language" not in q                            # none passed


def test_listen_url_no_keyterm_param_when_absent():
    url = deepgram_live.listen_url("nova-3")
    assert "keyterm=" not in url


def test_deepgram_live_threads_keyterms_into_its_url():
    dg = DeepgramLive("k", model="nova-3", keyterms=["Hermes"])
    assert "keyterm=Hermes" in dg._url
