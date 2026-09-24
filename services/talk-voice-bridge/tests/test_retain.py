"""Tests for Idea 4 — retaining call transcripts into Hindsight memory.

Two halves: the inline Hindsight client contract (POST shape / empty-content noop / never
raises), asserted on the wire with respx; and the bridge change that makes INBOUND calls
capture their transcript too (was outbound-only, for the report-back).

No real Hindsight/OpenAI — respx mocks httpx; a fake async-iterable WS drives the receive loop.
"""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
import respx

from voicecore import eventlog
from voicecore import hindsight
import outbound
import realtime_bridge
from approval import ApprovalStore
from config import load
from outbound import OutboundMission

_MEMORIES_URL = "http://hindsight:8888/v1/default/banks/hermes/memories"


# -- fake WS + bridge builder (mirrors tests/test_outbound.py) ---------------------------

class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class IterWS(FakeWS):
    def __init__(self, messages):
        super().__init__()
        self._messages = messages

    def __aiter__(self):
        async def _gen():
            for m in self._messages:
                yield m
        return _gen()


def _bridge(mission):
    return realtime_bridge.RealtimeBridge(
        load(), "PROMPT", ApprovalStore(),
        token_ctx={"token": "t", "caller": ""}, mission=mission)


# -- the inline Hindsight client contract ------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_retain_posts_correct_shape():
    """The verified live contract: items[] with str->str metadata + tags, top-level async:true."""
    route = respx.post(_MEMORIES_URL).mock(return_value=httpx.Response(200, json={}))
    ok = await hindsight.retain(
        "http://hindsight:8888", "hermes",
        content="Them: hi\nAI: hello", document_id="voice-talk-x",
        metadata={"platform": "voice_talk", "direction": "inbound"},
        tags=["voice", "talk", "inbound"])
    assert ok is True
    assert route.called
    body = json.loads(route.calls.last.request.content)
    item = body["items"][0]
    assert item["content"] == "Them: hi\nAI: hello"
    assert item["document_id"] == "voice-talk-x"
    assert item["metadata"] == {"platform": "voice_talk", "direction": "inbound"}
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in item["metadata"].items())
    assert item["tags"] == ["voice", "talk", "inbound"]
    assert body["async"] is True


@pytest.mark.asyncio
@respx.mock
async def test_retain_empty_content_is_noop():
    """Whitespace-only content is dropped before any HTTP call — returns False, posts nothing."""
    ok = await hindsight.retain("http://hindsight:8888", "hermes",
                                content="   ", document_id="x")
    assert ok is False
    assert len(respx.calls) == 0


@pytest.mark.asyncio
@respx.mock
async def test_retain_failure_returns_false_not_raises():
    """A 5xx (or any error) is swallowed: retain never raises into the call path."""
    respx.post(_MEMORIES_URL).mock(return_value=httpx.Response(500))
    ok = await hindsight.retain("http://hindsight:8888", "hermes",
                                content="Them: hi", document_id="x")
    assert ok is False


# -- inbound now captures its transcript (was outbound-only) -----------------------------

@pytest.mark.asyncio
async def test_inbound_now_captures_transcript():
    """Idea 4 dropped the mission guard: an INBOUND session captures both halves too."""
    bridge = _bridge(None)   # inbound

    async def _noop(_delta):
        return None
    bridge._play = _noop     # no real Pulse pipe under test

    ws = IterWS([
        json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "hello robot"}),
        json.dumps({"type": "response.output_audio_transcript.done",
                    "transcript": "hi there"}),
    ])
    await bridge._receive_from_openai(ws)
    assert bridge._transcript == ["Them: hello robot", "AI: hi there"]


# -- s5 (ticket 05): what the Talk Outlet's archive rows say ------------------------------

@pytest.mark.asyncio
async def test_the_talk_bridge_records_its_own_outlet_agent_mission_and_outcome(monkeypatch):
    """One outbound Talk call whose session never opens: the archive still gets a row,
    and every field in it is one this bridge actually knew.

    Failing the session on purpose is how `outcome` gets tested at all: before this ticket
    both Talk and phone hard-coded outcome="ok" on teardown, so a call that blew up was
    archived as one that finished cleanly.
    """
    cfg = replace(load(), openai_api_key="k", hindsight_url="http://hs:8888",
                  hindsight_bank="voice", retain_enabled=True)
    profile = SimpleNamespace(agent_id="hermes-main")
    mission = OutboundMission(brief="Ask the vet about Rufus.", target_display="The vet")
    bridge = realtime_bridge.RealtimeBridge(
        cfg, "PROMPT", ApprovalStore(), token_ctx={"token": "ROOM-1", "caller": "Alex"},
        mission=mission, profile=profile)
    bridge._transcript = ["Them: hi", "AI: hello"]

    def boom(*a, **kw):
        raise RuntimeError("openai is down")
    monkeypatch.setattr(realtime_bridge.websockets, "connect", boom)

    dispatched = []
    monkeypatch.setattr(hindsight, "retain_detached",
                        lambda url, bank, **kw: dispatched.append((url, bank, kw)) or True)
    events = []
    monkeypatch.setattr(eventlog, "append_event", lambda obj, path=None: events.append(obj))
    monkeypatch.setattr(outbound, "deliver_transcript",
                        lambda *a, **kw: asyncio.sleep(0))

    await bridge.run()

    assert len(dispatched) == 1
    _url, bank, kw = dispatched[0]
    assert bank == "voice"
    meta = kw["metadata"]
    assert meta["outlet"] == "talk"          # this bridge IS the Talk Outlet
    assert meta["agent"] == "hermes-main"
    assert meta["mission"] == "Ask the vet about Rufus."
    assert meta["outcome"] == "error"        # it did NOT finish cleanly, and says so
    assert meta["direction"] == "outbound"
    assert float(meta["duration_s"]) >= 0.0
    assert "talk" in kw["tags"] and "hermes-main" in kw["tags"]

    calls = [e for e in events if e.get("type") == "call"]
    assert len(calls) == 1
    assert calls[0]["outlet"] == "talk" and calls[0]["outcome"] == "error"
    assert "openai is down" in (calls[0]["err"] or "")


@pytest.mark.asyncio
async def test_an_inbound_talk_call_with_no_agent_records_neither_agent_nor_mission(monkeypatch):
    """Unknown stays unknown. No "no-profile" stand-in, no empty mission string."""
    cfg = replace(load(), openai_api_key="k", hindsight_url="http://hs:8888",
                  hindsight_bank="voice", retain_enabled=True)
    bridge = realtime_bridge.RealtimeBridge(
        cfg, "PROMPT", ApprovalStore(), token_ctx={"token": "ROOM-2", "caller": "Alex"},
        mission=None, profile=None)
    bridge._transcript = ["Them: hi", "AI: hello"]

    def boom(*a, **kw):
        raise RuntimeError("openai is down")
    monkeypatch.setattr(realtime_bridge.websockets, "connect", boom)

    dispatched = []
    monkeypatch.setattr(hindsight, "retain_detached",
                        lambda url, bank, **kw: dispatched.append((url, bank, kw)) or True)
    monkeypatch.setattr(eventlog, "append_event", lambda obj, path=None: None)

    await bridge.run()

    assert len(dispatched) == 1
    meta = dispatched[0][2]["metadata"]
    assert "agent" not in meta
    assert "mission" not in meta
    assert meta["outlet"] == "talk" and meta["direction"] == "inbound"


@pytest.mark.asyncio
async def test_a_hanging_store_does_not_delay_the_talk_lane_teardown(monkeypatch):
    """The fire-and-forget bar on this Outlet: the store holds the socket, the call ends.

    The retain is really in flight when `run()` returns -- that is the point. What must not
    happen is `run()` waiting for it.
    """
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hang(url, bank, **kwargs):
        entered.set()
        await release.wait()
        return False, "released"
    monkeypatch.setattr(hindsight, "retain_result", hang)

    cfg = replace(load(), openai_api_key="k", hindsight_url="http://hs:8888",
                  hindsight_bank="voice", retain_enabled=True)
    bridge = realtime_bridge.RealtimeBridge(
        cfg, "PROMPT", ApprovalStore(), token_ctx={"token": "ROOM-3", "caller": "Alex"},
        mission=None, profile=None)
    bridge._transcript = ["Them: hi"]

    def boom(*a, **kw):
        raise RuntimeError("openai is down")
    monkeypatch.setattr(realtime_bridge.websockets, "connect", boom)
    events = []
    monkeypatch.setattr(eventlog, "append_event", lambda obj, path=None: events.append(obj))

    await asyncio.wait_for(bridge.run(), timeout=5.0)
    await asyncio.wait_for(entered.wait(), timeout=5.0)

    # The call record was written while the store was still holding the socket.
    assert [e for e in events if e.get("type") == "call"]
    release.set()
