"""Ticket 10 API: POST /api/missions/expand and /api/missions/dictate.

The Agent writes. The result is returned for review. A failure body has no
``mission`` key — that is the server half of "leaves the typed text intact".
Neither route can reach the phone bridge.
"""
import copy
import json

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from conftest import RecordingTransport, SentinelTransport

OWNER = "+61491570156"

AGENT_A = {
    "id": "agent-a",
    "description": "one-shot A",
    "enabled": True,
    "pipeline": "realtime",
    "providers": {"realtime": "openai-gpt-realtime"},
    "hermes_profile": "scout",
}

AGENT_DEFAULT = {
    "id": "agent-default",
    "description": "default profile",
    "enabled": True,
    "pipeline": "realtime",
    "providers": {"realtime": "openai-gpt-realtime"},
}


def _doc(base=AGENT_A, **overrides):
    d = copy.deepcopy(base)
    d.update(overrides)
    return d


def _write_agent(tmp_path, doc):
    (tmp_path / "agents").mkdir(exist_ok=True)
    (tmp_path / "agents" / f"{doc['id']}.yaml").write_text(yaml.safe_dump(doc))


def _wire(monkeypatch, tmp_path, *docs):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "agents").mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_GATEWAY_TOKEN", "gw-token")
    monkeypatch.setenv("HERMES_GATEWAY_URL", "http://hermes.test:18789")
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS",
                       "scout=http://sprint.test:18790")
    monkeypatch.setenv("VOICE_MODE_C_URL", "http://bridge.test:3336")
    for doc in docs:
        _write_agent(tmp_path, doc)
    (tmp_path / "active.yaml").write_text(yaml.safe_dump({
        "outlets": {
            "phone": {"inbound": None, "outbound": None},
            "talk": {"inbound": None, "outbound": None},
        }
    }))


def _gateway_ok(text="Call the dentist and move Thursday to Friday."):
    async def handler(request):
        path = request.url.path
        if path == "/v1/audio/transcriptions":
            return httpx.Response(200, json={"text": "move thursday to friday"})
        if path == "/v1/chat/completions":
            body = json.loads(request.content)
            assert body.get("tool_choice") == "none"
            assert "audio" not in body
            return httpx.Response(200, json={
                "choices": [{"message": {"content": text}}]})
        raise AssertionError(f"unexpected path: {path}")
    return RecordingTransport(handler)


def test_expand_goes_to_that_agents_gateway_not_another(
        make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _doc(), _doc(AGENT_DEFAULT))
    transport = _gateway_ok()
    client = TestClient(make_app(transport))
    r = client.post("/api/missions/expand", json={
        "agent": "agent-a", "prompt": "move thursday",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mission"] == "Call the dentist and move Thursday to Friday."
    assert body["agent"] == "agent-a"
    assert body["hermes_profile"] == "scout"
    assert len(transport.calls) == 1
    req = transport.calls[0]
    assert req.url.host == "sprint.test"
    assert req.url.path == "/v1/chat/completions"
    assert req.headers.get("authorization") == "Bearer gw-token"
    sent = json.loads(req.content)
    assert "move thursday" in sent["messages"][0]["content"]
    assert sent["tool_choice"] == "none"


def test_expand_of_default_profile_hits_the_default_gateway(
        make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _doc(AGENT_DEFAULT))
    transport = _gateway_ok("A Mission from default.")
    client = TestClient(make_app(transport))
    r = client.post("/api/missions/expand", json={
        "agent": "agent-default", "prompt": "say hello",
    })
    assert r.status_code == 200, r.text
    assert transport.calls[0].url.host == "hermes.test"


def test_unknown_hermes_profile_does_not_borrow_another_backend(
        make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _doc(hermes_profile="nobody-configured"))
    client = TestClient(make_app(SentinelTransport()))
    r = client.post("/api/missions/expand", json={
        "agent": "agent-a", "prompt": "x",
    })
    assert r.status_code == 409
    assert "nobody-configured" in r.text
    assert "mission" not in r.json()


def _failure_bodies():
    async def unreachable(request):
        raise httpx.ConnectError("refused")

    async def slow(request):
        raise httpx.ReadTimeout("slow")

    async def nothing(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    async def error(request):
        return httpx.Response(503, json={"error": "nope"})

    return [
        (unreachable, "unreachable"),
        (slow, "slow"),
        (nothing, "nothing"),
        (error, "error"),
    ]


@pytest.mark.parametrize("handler,_why", _failure_bodies(),
                         ids=["unreachable", "slow", "nothing", "error"])
def test_expand_failure_has_no_mission_key(make_app, monkeypatch, tmp_path,
                                           handler, _why):
    """THE server half of 'leaves the typed text intact'.

    A client that did ``if ('mission' in body) setMission(body.mission)``
    cannot clear the form from any of these. Sabotage: return
    ``{"mission": ""}`` on failure and this goes red.
    """
    _wire(monkeypatch, tmp_path, _doc())
    monkeypatch.setenv("VOICE_MISSION_TIMEOUT_S", "0.05")
    client = TestClient(make_app(RecordingTransport(handler)))
    r = client.post("/api/missions/expand", json={
        "agent": "agent-a", "prompt": "keep this typed line",
    })
    assert r.status_code == 502, r.text
    body = r.json()
    assert "mission" not in body, body
    assert body.get("detail")


def test_expand_does_not_dial(make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _doc())

    async def handler(request):
        if request.url.host == "bridge.test":
            raise AssertionError("assist reached the phone bridge")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "A Mission."}}]})

    transport = RecordingTransport(handler)
    client = TestClient(make_app(transport))
    r = client.post("/api/missions/expand", json={
        "agent": "agent-a", "prompt": "x",
    })
    assert r.status_code == 200, r.text
    assert all(q.url.path != "/voice/outbound" for q in transport.calls)
    assert all(q.url.host != "bridge.test" for q in transport.calls)


def test_dictate_goes_to_that_agents_gateway(make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _doc())
    transport = _gateway_ok()
    client = TestClient(make_app(transport))
    r = client.post("/api/missions/dictate", data={"agent": "agent-a"},
                    files={"audio": ("clip.webm", b"x" * 400, "audio/webm")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mission"].startswith("Call the dentist")
    assert body["hermes_profile"] == "scout"
    hosts = {q.url.host for q in transport.calls}
    assert hosts == {"sprint.test"}
    paths = {q.url.path for q in transport.calls}
    assert paths <= {"/v1/audio/transcriptions", "/v1/chat/completions"}
    assert "/voice/outbound" not in paths
    assert "/v1/audio/speech" not in paths


@pytest.mark.parametrize("handler,_why", _failure_bodies(),
                         ids=["unreachable", "slow", "nothing", "error"])
def test_dictate_failure_has_no_mission_key(make_app, monkeypatch, tmp_path,
                                            handler, _why):
    _wire(monkeypatch, tmp_path, _doc())
    monkeypatch.setenv("VOICE_MISSION_TIMEOUT_S", "0.05")
    client = TestClient(make_app(RecordingTransport(handler)))
    r = client.post("/api/missions/dictate", data={"agent": "agent-a"},
                    files={"audio": ("clip.webm", b"x" * 400, "audio/webm")})
    assert r.status_code == 502, r.text
    assert "mission" not in r.json()


def test_dictate_silence_does_not_hit_the_gateway(
        make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _doc())
    client = TestClient(make_app(SentinelTransport()))
    r = client.post("/api/missions/dictate", data={"agent": "agent-a"},
                    files={"audio": ("clip.webm", b"", "audio/webm")})
    assert r.status_code == 502
    assert "silent" in r.text.lower() or "short" in r.text.lower()
    assert "mission" not in r.json()


def test_dictate_does_not_dial(make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _doc())

    async def handler(request):
        if request.url.host == "bridge.test" or request.url.path == "/voice/outbound":
            raise AssertionError("dictate reached the phone path")
        if request.url.path == "/v1/audio/transcriptions":
            return httpx.Response(200, json={"text": "hello"})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "Say hello and hang up."}}]})

    transport = RecordingTransport(handler)
    client = TestClient(make_app(transport))
    r = client.post("/api/missions/dictate", data={"agent": "agent-a"},
                    files={"audio": ("clip.webm", b"x" * 400, "audio/webm")})
    assert r.status_code == 200, r.text
    assert all(q.url.path != "/voice/outbound" for q in transport.calls)


def test_assist_requires_an_agent_that_can_run(make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _doc(enabled=False))
    client = TestClient(make_app(SentinelTransport()))
    r = client.post("/api/missions/expand", json={
        "agent": "agent-a", "prompt": "x",
    })
    assert r.status_code == 409
    assert "mission" not in r.json()


def test_place_still_uses_the_mission_the_operator_sends(
        make_app, monkeypatch, tmp_path):
    """Expand does not stash a Mission the next place would fire unseen."""
    _wire(monkeypatch, tmp_path, _doc())

    async def handler(request):
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "EXPANDED UNSEEN"}}]})
        if request.url.path == "/voice/outbound":
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "placed": True, "call_sid": "CA1",
                "call_id": "cid", "agent": body.get("agent")})
        raise AssertionError(request.url)

    client = TestClient(make_app(RecordingTransport(handler)))
    exp = client.post("/api/missions/expand", json={
        "agent": "agent-a", "prompt": "short",
    })
    assert exp.status_code == 200
    assert exp.json()["mission"] == "EXPANDED UNSEEN"
    placed = client.post("/api/calls/place", json={
        "agent": "agent-a", "to": OWNER,
        "mission": "the operator edited this", "disclose": False,
    })
    assert placed.status_code == 200, placed.text
    assert placed.json()["mission"] == "the operator edited this"


@pytest.mark.parametrize("tools", [True, False], ids=["tools-on", "tools-off"])
def test_mission_authoring_is_tools_off_for_a_direct_agent_whatever_its_setting(
        make_app, monkeypatch, tmp_path, tools):
    """A direct Hermes Agent with tools off used to be refused here (its outbound load
    said 'cannot be sandboxed'). It writes a Mission like any other Agent, and the turn is
    tool_choice none through the listen-only client whichever way its setting is."""
    direct = _doc(id="agent-direct", pipeline="cascade",
                  providers={"stt": "deepgram", "llm": "hermes-agent", "tts": "elevenlabs"},
                  guardrails={"on_call_tools": tools})
    _wire(monkeypatch, tmp_path, direct)
    transport = _gateway_ok()
    client = TestClient(make_app(transport))
    r = client.post("/api/missions/expand", json={"agent": "agent-direct", "prompt": "x"})
    assert r.status_code == 200, r.text
    d = client.post("/api/missions/dictate", data={"agent": "agent-direct"},
                    files={"audio": ("clip.webm", b"x" * 400, "audio/webm")})
    assert d.status_code == 200, d.text
    chats = [json.loads(q.content) for q in transport.calls
             if q.url.path == "/v1/chat/completions"]
    assert len(chats) >= 1 and all(c["tool_choice"] == "none" for c in chats)
