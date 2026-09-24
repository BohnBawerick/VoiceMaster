"""Tests for the OUTBOUND-calling path — the sandbox guardrail is the load-bearing thing here.

The security-critical invariant: an outbound (mission) session must be sent to OpenAI with
NO tools and NO caller-reachable backend, seeded only with the mission brief. If that ever
regresses, a callee could prompt-inject the on-call model into reaching the owner's data.
These tests assert the wire-level session shape directly, plus the OCS/report-back plumbing.

No real Playwright/OpenAI/Nextcloud — a fake WS records sends; respx mocks httpx.
"""
import asyncio
import json

import httpx
import pytest
import respx

import hermes
import outbound
import realtime_bridge
import session as session_mod
from approval import ApprovalStore
from config import load
from outbound import OutboundMission


# -- the mission system prompt (defense-in-depth on top of tools=[]) ---------------------

def test_outbound_prompt_carries_brief_and_containment():
    p = outbound.build_outbound_prompt("Ask the dentist to move Tue 3pm to Thu 10am.",
                                       target_display="Dr Smith")
    assert "Ask the dentist to move Tue 3pm to Thu 10am." in p   # the mission is present
    assert "Dr Smith" in p
    assert "NO tools" in p and "NO access" in p                  # explicit containment
    assert "Stay strictly on mission" in p


def test_outbound_prompt_omits_soul_and_backend_tool():
    """The sandbox prompt must NOT drag in the owner persona or advertise the backend tool."""
    p = outbound.build_outbound_prompt("say hi")
    assert "hermes_agent" not in p        # no backend escape hatch mentioned
    assert "== YOUR SOUL ==" not in p     # SOUL is inbound-only (see hermes.build_system_prompt)


# -- the sandboxed OpenAI session shape --------------------------------------------------

class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _bridge(mission):
    return realtime_bridge.RealtimeBridge(
        load(), "MISSION PROMPT", ApprovalStore(),
        token_ctx={"token": "t", "caller": ""}, mission=mission)


@pytest.mark.asyncio
async def test_outbound_session_has_no_tools():
    """The core guardrail, asserted on the wire: mission session => tools=[] / tool_choice=none."""
    bridge = _bridge(OutboundMission(brief="call and say hi"))
    ws = FakeWS()
    await bridge._send_session_update(ws)
    sess = ws.sent[0]["session"]
    assert sess["tools"] == []
    assert sess["tool_choice"] == "none"
    assert sess["instructions"] == "MISSION PROMPT"
    # caller-side ASR is on so the transcript report-back can capture both halves
    assert sess["audio"]["input"]["transcription"] == {"model": bridge._cfg.transcription_model}
    assert bridge._cfg.transcription_model == "gpt-4o-transcribe"   # Idea 3 default


@pytest.mark.asyncio
async def test_inbound_session_keeps_full_tools():
    """Regression guard: inbound keeps full hermes.TOOLS. Idea 4: input transcription is now ON
    for inbound too (was outbound-only), so inbound calls can be retained to memory."""
    bridge = _bridge(None)
    ws = FakeWS()
    await bridge._send_session_update(ws)
    sess = ws.sent[0]["session"]
    assert sess["tools"] == hermes.TOOLS
    assert sess["tool_choice"] == "auto"
    assert sess["audio"]["input"]["transcription"] == {"model": bridge._cfg.transcription_model}


@pytest.mark.asyncio
async def test_outbound_greeting_opens_the_mission():
    bridge = _bridge(OutboundMission(brief="say hi"))
    ws = FakeWS()
    await bridge._send_initial_greeting(ws)
    text = ws.sent[0]["item"]["content"][0]["text"]
    assert "mission" in text.lower() and "answered" in text.lower()


# -- transcript capture (outbound only) --------------------------------------------------

class IterWS(FakeWS):
    def __init__(self, messages):
        super().__init__()
        self._messages = messages

    def __aiter__(self):
        async def _gen():
            for m in self._messages:
                yield m
        return _gen()


@pytest.mark.asyncio
async def test_transcript_captures_both_sides():
    bridge = _bridge(OutboundMission(brief="x"))
    ws = IterWS([
        json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "Hello, who's this?"}),
        json.dumps({"type": "response.output_audio_transcript.done",
                    "transcript": "Hi, I'm calling on behalf of Alex."}),
    ])
    await bridge._receive_from_openai(ws)
    assert bridge._transcript == [
        "Them: Hello, who's this?",
        "AI: Hi, I'm calling on behalf of Alex.",
    ]


@pytest.mark.asyncio
async def test_inbound_captures_transcript():
    """Idea 4: inbound now captures its transcript too (was outbound-only) — it feeds the
    Hindsight memory retain on teardown."""
    bridge = _bridge(None)   # inbound
    ws = IterWS([json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                             "transcript": "remember this"})])
    await bridge._receive_from_openai(ws)
    assert bridge._transcript == ["Them: remember this"]


# -- OCS room resolution + report-back ---------------------------------------------------

def _cfg_nc():
    import os
    os.environ["NEXTCLOUD_BASE_URL"] = "https://nc.test"
    os.environ["NEXTCLOUD_VOICE_APP_PASSWORD"] = "app-pw"
    os.environ["NEXTCLOUD_TALK_HOME_CONVERSATION"] = "home1"
    return load()


@pytest.mark.asyncio
@respx.mock
async def test_resolve_room_returns_token():
    respx.post("https://nc.test/ocs/v2.php/apps/spreed/api/v4/room").mock(
        return_value=httpx.Response(200, json={"ocs": {"data": {"token": "room9"}}}))
    token = await outbound.resolve_room(_cfg_nc(), "Alex")
    assert token == "room9"


@pytest.mark.asyncio
@respx.mock
async def test_deliver_transcript_talk_posts_to_room():
    route = respx.post("https://nc.test/ocs/v2.php/apps/spreed/api/v1/chat/home1").mock(
        return_value=httpx.Response(200, json={"ocs": {}}))
    m = OutboundMission(brief="x", report_channel="talk", report_address="home1",
                        target_display="Mum")
    await outbound.deliver_transcript(_cfg_nc(), m, ["Them: hi", "AI: hello"])
    assert route.called
    body = route.calls.last.request.content.decode()
    assert "Mum" in body and "Them%3A+hi" in body   # form-encoded transcript


@pytest.mark.asyncio
@respx.mock
async def test_deliver_transcript_telegram_when_token_present():
    cfg = _cfg_nc()
    object.__setattr__(cfg, "telegram_bot_token", "BOT123")   # frozen dataclass
    route = respx.post("https://api.telegram.org/botBOT123/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    m = OutboundMission(brief="x", report_channel="telegram", report_address="8209633400")
    await outbound.deliver_transcript(cfg, m, ["Them: hi"])
    assert route.called
    assert json.loads(route.calls.last.request.content)["chat_id"] == "8209633400"


@pytest.mark.asyncio
@respx.mock
async def test_deliver_transcript_telegram_falls_back_to_talk_without_token():
    """Telegram requested but no bot token configured => fall back to the Talk home room."""
    talk = respx.post("https://nc.test/ocs/v2.php/apps/spreed/api/v1/chat/home1").mock(
        return_value=httpx.Response(200, json={"ocs": {}}))
    m = OutboundMission(brief="x", report_channel="telegram", report_address="home1")
    await outbound.deliver_transcript(_cfg_nc(), m, ["Them: hi"])
    assert talk.called


# -- session routing: outbound => start_call + mission reaches the bridge -----------------

class DualBrowser:
    def __init__(self):
        self.started, self.joined, self.left = [], [], 0

    async def start_call(self, token):
        self.started.append(token)

    async def join_call(self, token):
        self.joined.append(token)

    async def leave_call(self):
        self.left += 1


class CapturingBridge:
    last_kwargs: dict = {}

    def __init__(self, *a, **kw):
        CapturingBridge.last_kwargs = kw
        self._ended = asyncio.Event()

    async def run(self):
        await self._ended.wait()

    async def stop(self):
        self._ended.set()


@pytest.mark.asyncio
async def test_outbound_start_uses_start_call_and_passes_mission(monkeypatch):
    monkeypatch.setattr(session_mod, "RealtimeBridge", CapturingBridge)
    browser = DualBrowser()
    cs = session_mod.CallSession(load(), browser, ApprovalStore())
    m = OutboundMission(brief="call and say hi")

    assert await cs.start("room1", "outbound", "", "", mission=m) is True
    assert browser.started == ["room1"] and browser.joined == []     # rang, did not answer
    assert CapturingBridge.last_kwargs.get("mission") is m           # mission threaded through

    await cs.stop("room1")
    assert cs.busy is False
