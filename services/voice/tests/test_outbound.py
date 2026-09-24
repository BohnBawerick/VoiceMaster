"""Tests for the Mode C (Twilio PSTN) OUTBOUND-calling path.

The security-critical invariant is load-bearing: an outbound (mission) session must be sent to
OpenAI with NO tools, seeded only with the mission brief. If that ever regresses, a callee could
prompt-inject the on-call model into reaching the owner's data. These tests assert the wire-level
session shape directly, plus the endpoint's auth / allow-list / dial plumbing and the report-back.

No real Twilio/OpenAI/Nextcloud: a fake WS records `session.update`; the Twilio SDK Client is
monkeypatched; respx mocks httpx for report-back. Trust-inverse of the inbound path (`tools=TOOLS`).
"""
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import outbound
import server
from outbound import OutboundMission


# -- the mission system prompt (defense-in-depth on top of tools=[]) ---------------------

def test_outbound_prompt_carries_brief_and_containment():
    p = server.build_outbound_prompt("Ask the dentist to move Tue 3pm to Thu 10am.",
                                     target_display="Dr Smith")
    assert "Ask the dentist to move Tue 3pm to Thu 10am." in p   # the mission is present
    assert "Dr Smith" in p
    assert "NO tools" in p and "NO access" in p                  # explicit containment
    assert "Stay strictly on mission" in p


def test_outbound_prompt_omits_soul_and_backend_tool():
    """The sandbox prompt must NOT drag in the owner persona or advertise the backend tool."""
    p = server.build_outbound_prompt("say hi")
    assert "hermes_agent" not in p        # no backend escape hatch mentioned
    assert "YOUR SOUL" not in p           # SOUL is inbound-only (server.build_system_prompt)


def test_outbound_prompt_disclosure_toggle():
    assert "automated assistant" not in server.build_outbound_prompt("x", disclose=False).lower()
    assert "automated assistant" in server.build_outbound_prompt("x", disclose=True).lower()


# -- the sandboxed OpenAI session shape (THE guardrail) ----------------------------------

class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


@pytest.mark.asyncio
async def test_outbound_session_has_no_tools():
    """The core guardrail, asserted on the wire: mission session => tools=[] / tool_choice=none."""
    ws = FakeWS()
    await server._send_session_update(ws, "MISSION PROMPT", outbound=True)
    sess = ws.sent[0]["session"]
    assert sess["tools"] == []
    assert sess["tool_choice"] == "none"
    assert sess["instructions"] == "MISSION PROMPT"
    # caller-side ASR is on so the transcript report-back can capture both halves
    assert sess["audio"]["input"]["transcription"] == {"model": server.TRANSCRIPTION_MODEL}
    assert server.TRANSCRIPTION_MODEL == "gpt-4o-transcribe"   # Idea 3 default


@pytest.mark.asyncio
async def test_inbound_session_keeps_full_tools():
    """Regression guard: the normal inbound path must be untouched (full server.TOOLS, auto)."""
    ws = FakeWS()
    await server._send_session_update(ws, "INBOUND PROMPT", outbound=False)
    sess = ws.sent[0]["session"]
    assert sess["tools"] == server.TOOLS
    assert sess["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_outbound_greeting_opens_the_mission():
    ws = FakeWS()
    await server._send_initial_greeting(ws, outbound=True)
    text = ws.sent[0]["item"]["content"][0]["text"]
    assert "mission" in text.lower() and "answered" in text.lower()


# -- POST /voice/outbound: auth, allow-list, dial plumbing -------------------------------

@pytest.fixture
def client():
    return TestClient(server.app)


def test_outbound_endpoint_rejects_bad_token(client):
    r = client.post("/voice/outbound", headers={"Authorization": "Bearer wrong"},
                    json={"brief": "hi", "to": "+61491570156"})
    assert r.status_code == 401


def test_outbound_endpoint_requires_brief_and_number(client):
    h = {"Authorization": "Bearer test-token"}
    assert client.post("/voice/outbound", headers=h, json={"to": "+61491570156"}).status_code == 400
    assert client.post("/voice/outbound", headers=h, json={"brief": "hi"}).status_code == 400


def test_outbound_endpoint_enforces_allowlist(client):
    """A number outside VOICE_OUTBOUND_ALLOWED_NUMBERS is refused with 403 — no call placed."""
    r = client.post("/voice/outbound", headers={"Authorization": "Bearer test-token"},
                    json={"brief": "hi", "to": "+61400000000"})
    assert r.status_code == 403


def test_outbound_endpoint_places_call(client, monkeypatch):
    """Happy path: allow-listed number → Twilio calls.create(twiml=...) with the call_id Parameter,
    and the mission is stashed for media_stream to arm the sandbox on `start`."""
    captured = {}

    class FakeCall:
        sid = "CAtest123"

    class FakeCalls:
        def create(self, **kw):
            captured.update(kw)
            return FakeCall()

    class FakeClient:
        def __init__(self, *a, **kw):
            self.calls = FakeCalls()

    monkeypatch.setattr(server, "Client", FakeClient)

    r = client.post("/voice/outbound", headers={"Authorization": "Bearer test-token"},
                    json={"brief": "Tell them the order is ready.", "to": "+61491570156",
                          "target_display": "Mum", "report_channel": "telegram",
                          "report_address": "8209633400"})
    assert r.status_code == 200
    body = r.json()
    assert body["placed"] is True and body["call_sid"] == "CAtest123"
    cid = body["call_id"]

    # Twilio was asked to dial from the DID with inline TwiML carrying the call_id parameter.
    assert captured["to"] == "+61491570156"
    assert captured["from_"] == "+61855501234"
    assert f'name="call_id" value="{cid}"' in captured["twiml"]
    assert "wss://voiceh.test/voice/stream" in captured["twiml"]

    # The mission is remembered under that call_id (and only consumable once).
    m = server._take_mission(cid)
    assert m is not None and m.brief == "Tell them the order is ready." and m.to == "+61491570156"
    assert server._take_mission(cid) is None   # single-use


def test_outbound_route_places_exactly_the_builder_request(client, monkeypatch):
    """s5 c7: the route's calls.create kwargs ARE build_outbound_request's return value —
    the dashboard dry-run vendors the same builder, so a preview can't drift from what
    the live route places (and the builder isn't a mapper nothing real uses)."""
    from voicecore import outbound_request

    built = {}
    real_build = outbound_request.build_outbound_request

    def spying_build(**kw):
        out = real_build(**kw)
        built.update(out)
        return out

    monkeypatch.setattr(server.outbound_request, "build_outbound_request", spying_build)

    captured = {}

    class FakeCall:
        sid = "CAbuilder1"

    class FakeCalls:
        def create(self, **kw):
            captured.update(kw)
            return FakeCall()

    class FakeClient:
        def __init__(self, *a, **kw):
            self.calls = FakeCalls()

    monkeypatch.setattr(server, "Client", FakeClient)

    r = client.post("/voice/outbound", headers={"Authorization": "Bearer test-token"},
                    json={"brief": "hi", "to": "+61491570156"})
    assert r.status_code == 200
    assert built, "route did not go through outbound_request.build_outbound_request"
    assert captured == built   # kwargs passed to Twilio == the builder's exact output


def test_builder_refuses_empty_public_host():
    """s5 c7: no public host → an honest error, never a wss://None/... TwiML."""
    from voicecore import outbound_request

    with pytest.raises(outbound_request.OutboundRequestError, match="VOICE_PUBLIC_HOST"):
        outbound_request.build_outbound_request(
            to="+61491570156", from_number="+61855501234", public_host="", call_id="x")


# -- teardown: Twilio `stop` must close the OpenAI socket --------------------------------

from conftest import FakeOpenAIWS  # shared with test_inbound.py  # noqa: E402


def test_twilio_stop_closes_openai_socket(client, monkeypatch):
    """Regression: after Twilio's `stop` event the handler must close the OpenAI socket.
    Bug found live 2026-07-13: the stop-branch broke out without closing it, so teardown
    (and the outbound transcript delivery in the finally) stalled until OpenAI's own
    idle timeout — the transcript arrived minutes-to-never after hangup."""
    fake = FakeOpenAIWS()
    monkeypatch.setattr(server.websockets, "connect", lambda *a, **kw: fake)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")
    tok = server._mint_inbound_token()   # arm as a legit inbound call (see test_inbound.py)

    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZteardown",
                      "start": {"streamSid": "MZteardown", "callSid": "CAteardown",
                                "customParameters": {"inbound_token": tok}}})
        ws.send_json({"event": "stop", "sequenceNumber": "2", "streamSid": "MZteardown"})
    # handler must have torn down because WE closed the socket on `stop` — not because
    # the fake's patience ran out (which is what the pre-fix behaviour degenerates to).
    assert fake.state.name == "CLOSED"
    assert not fake.timed_out

@pytest.mark.asyncio
@respx.mock
async def test_deliver_transcript_talk_posts_to_room(monkeypatch):
    monkeypatch.setattr(outbound, "NEXTCLOUD_BASE_URL", "https://nc.test")
    monkeypatch.setattr(outbound, "NEXTCLOUD_VOICE_APP_PASSWORD", "app-pw")
    route = respx.post("https://nc.test/ocs/v2.php/apps/spreed/api/v1/chat/home1").mock(
        return_value=httpx.Response(200, json={"ocs": {}}))
    m = OutboundMission(brief="x", report_channel="talk", report_address="home1", target_display="Mum")
    await outbound.deliver_transcript(m, ["Them: hi", "AI: hello"])
    assert route.called
    body = route.calls.last.request.content.decode()
    assert "Mum" in body and "Them%3A+hi" in body   # form-encoded transcript


@pytest.mark.asyncio
@respx.mock
async def test_deliver_transcript_telegram_when_token_present(monkeypatch):
    monkeypatch.setattr(outbound, "TELEGRAM_BOT_TOKEN", "BOT123")
    route = respx.post("https://api.telegram.org/botBOT123/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    m = OutboundMission(brief="x", report_channel="telegram", report_address="8209633400")
    await outbound.deliver_transcript(m, ["Them: hi"])
    assert route.called
    assert json.loads(route.calls.last.request.content)["chat_id"] == "8209633400"


@pytest.mark.asyncio
@respx.mock
async def test_deliver_transcript_telegram_falls_back_to_talk_without_token(monkeypatch):
    """Telegram requested but no bot token configured => fall back to the Talk home room."""
    monkeypatch.setattr(outbound, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(outbound, "NEXTCLOUD_BASE_URL", "https://nc.test")
    monkeypatch.setattr(outbound, "NEXTCLOUD_VOICE_APP_PASSWORD", "app-pw")
    monkeypatch.setattr(outbound, "NEXTCLOUD_TALK_HOME_CONVERSATION", "home1")
    talk = respx.post("https://nc.test/ocs/v2.php/apps/spreed/api/v1/chat/home1").mock(
        return_value=httpx.Response(200, json={"ocs": {}}))
    m = OutboundMission(brief="x", report_channel="telegram", report_address="")
    await outbound.deliver_transcript(m, ["Them: hi"])
    assert talk.called


def test_second_outbound_while_one_pending_is_a_clear_409(client, monkeypatch):
    """s7 c7: one outbound call at a time — a second POST while the first mission is
    pending (or its session live) gets 409, and no second calls.create happens."""
    creates = []

    class FakeCall:
        sid = "CAbusy1"

    class FakeCalls:
        def create(self, **kw):
            creates.append(kw)
            return FakeCall()

    class FakeClient:
        def __init__(self, *a, **kw):
            self.calls = FakeCalls()

    monkeypatch.setattr(server, "Client", FakeClient)
    server._MISSIONS.clear()
    server._ACTIVE_OUTBOUND.clear()
    h = {"Authorization": "Bearer test-token"}
    first = client.post("/voice/outbound", headers=h,
                        json={"brief": "call one", "to": "+61491570156"})
    assert first.status_code == 200 and len(creates) == 1
    second = client.post("/voice/outbound", headers=h,
                         json={"brief": "call two", "to": "+61491570156"})
    assert second.status_code == 409
    assert "already in progress" in second.json()["error"]
    assert len(creates) == 1                       # never a second dial
    # teardown clears the gate: once the session/mission is gone, dialing works again
    server._MISSIONS.clear()
    server._ACTIVE_OUTBOUND.clear()
    third = client.post("/voice/outbound", headers=h,
                        json={"brief": "call three", "to": "+61491570156"})
    assert third.status_code == 200 and len(creates) == 2
