"""Ticket 09: POST /api/calls/place. One-shot, no pointer, no dry-run, no allow-list.

Every assertion here is written to go red if the route still did what the old
Test screen did: write active.yaml, require the activated outbound agent, run
a dry-run, or refuse a number that is not on a list.
"""
import copy
import json

import httpx
import yaml
from fastapi.testclient import TestClient

from conftest import RecordingTransport, SentinelTransport

OWNER = "+61491570156"
OTHER = "+61899990000"

AGENT_A = {
    "id": "agent-a",
    "description": "one-shot A",
    "enabled": True,
    "pipeline": "realtime",
    "providers": {"realtime": "openai-gpt-realtime"},
    "number_policy": {"allow": [OWNER]},
}

AGENT_B = {
    "id": "agent-b",
    "description": "one-shot B",
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


def _write_pointer(tmp_path, phone_outbound=None):
    path = tmp_path / "active.yaml"
    path.write_text(yaml.safe_dump({
        "outlets": {
            "phone": {"inbound": None, "outbound": phone_outbound},
            "talk": {"inbound": None, "outbound": None},
        }
    }))
    return path


def _wire(monkeypatch, tmp_path, *docs, phone_outbound=None):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "agents").mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_GATEWAY_TOKEN", "gw-token")
    monkeypatch.setenv("VOICE_MODE_C_URL", "http://127.0.0.1:3336")
    monkeypatch.setenv("VOICE_OWNER_NUMBER", OWNER)
    for doc in docs:
        _write_agent(tmp_path, doc)
    return _write_pointer(tmp_path, phone_outbound)


def place_transport(mode_c_status=200, call_sid="CAplace1"):
    async def handler(request):
        host = request.url.host
        if host == "127.0.0.1":
            assert request.url.path == "/voice/outbound"
            if mode_c_status != 200:
                return httpx.Response(mode_c_status, json={
                    "error": "an outbound call is already in progress"})
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "placed": True, "call_sid": call_sid,
                "call_id": "cid-" + body.get("agent", "x"),
                "agent": body.get("agent"),
            })
        raise AssertionError(f"unexpected host: {host}")

    return RecordingTransport(handler)


def _dials(transport):
    return [q for q in transport.calls if q.url.host == "127.0.0.1"]


def _place(client, agent="agent-a", to=OWNER, mission="Ask about Friday.",
           disclose=False, **extra):
    return client.post("/api/calls/place", json={
        "agent": agent, "to": to, "mission": mission, "disclose": disclose,
        **extra,
    })


def test_place_dials_the_named_agent_and_does_not_touch_the_pointer(
        make_app, monkeypatch, tmp_path):
    pointer = _wire(monkeypatch, tmp_path, _doc(), _doc(AGENT_B),
                    phone_outbound="agent-b")
    before = pointer.read_text()
    transport = place_transport()
    client = TestClient(make_app(transport))
    r = _place(client, agent="agent-a", to=OTHER, mission="Book a table.",
               disclose=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["placed"] is True
    assert body["agent"] == "agent-a"
    assert body["to"] == OTHER
    assert body["mission"] == "Book a table."
    assert body["disclose"] is True
    assert body["call_sid"] == "CAplace1"
    assert "dry_run" not in body and "dry_run_report" not in body
    assert pointer.read_text() == before

    dials = _dials(transport)
    assert len(dials) == 1
    sent = json.loads(dials[0].content)
    assert sent["agent"] == "agent-a"
    assert sent["to"] == OTHER
    assert sent["brief"] == "Book a table."
    assert sent["disclose"] is True
    assert dials[0].headers.get("authorization") == "Bearer gw-token"
    # Dashboard never talks to Twilio.
    assert all(q.url.host != "api.twilio.com" for q in transport.calls)


def test_two_places_with_different_agents_do_not_share_state(
        make_app, monkeypatch, tmp_path):
    """THE dashboard-side shared-mutable-state test.

    Fire A, then fire B. Each POST to the bridge names its own agent. The
    pointer is byte-identical throughout. If the route wrote the pointer
    between fires (the old "activate then fire" path), the file would change
    and this would go red.
    """
    pointer = _wire(monkeypatch, tmp_path, _doc(), _doc(AGENT_B),
                    phone_outbound="agent-b")
    before = pointer.read_text()
    transport = place_transport()
    client = TestClient(make_app(transport))

    a = _place(client, agent="agent-a", mission="Mission A")
    b = _place(client, agent="agent-b", mission="Mission B", to=OTHER)
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
    assert pointer.read_text() == before

    sent = [json.loads(q.content) for q in _dials(transport)]
    assert [s["agent"] for s in sent] == ["agent-a", "agent-b"]
    assert [s["brief"] for s in sent] == ["Mission A", "Mission B"]
    assert sent[0]["to"] == OWNER and sent[1]["to"] == OTHER


def test_place_dials_a_number_not_on_any_allow_list(
        make_app, monkeypatch, tmp_path):
    """agent-a has number_policy.allow = [OWNER] only. OTHER still dials."""
    _wire(monkeypatch, tmp_path, _doc(), phone_outbound="agent-a")
    transport = place_transport()
    client = TestClient(make_app(transport))
    r = _place(client, to="+61 899 990 000")
    assert r.status_code == 200, r.text
    sent = json.loads(_dials(transport)[0].content)
    assert sent["to"] == OTHER
    assert sent["agent"] == "agent-a"


def test_place_normalizes_the_target(make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _doc())
    for tricky in ("+61 491 570 156", "0061491570156", "+61-491-570-156"):
        transport = place_transport()
        client = TestClient(make_app(transport))
        r = _place(client, to=tricky)
        assert r.status_code == 200, f"{tricky!r}: {r.text}"
        assert json.loads(_dials(transport)[0].content)["to"] == OWNER


def test_place_refuses_a_non_number(make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _doc())
    transport = place_transport()
    client = TestClient(make_app(transport))
    r = _place(client, to="+61abc")
    assert r.status_code == 422
    assert not _dials(transport)


def test_place_requires_agent_to_and_mission(make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _doc())
    client = TestClient(make_app(SentinelTransport()))
    assert client.post("/api/calls/place", json={
        "to": OWNER, "mission": "x"}).status_code == 422
    assert client.post("/api/calls/place", json={
        "agent": "agent-a", "mission": "x"}).status_code == 422
    assert client.post("/api/calls/place", json={
        "agent": "agent-a", "to": OWNER}).status_code == 422
    assert client.post("/api/calls/place", json={
        "agent": "agent-a", "to": OWNER, "mission": "   "}).status_code == 422


def test_place_refuses_a_missing_agent(make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _doc())
    client = TestClient(make_app(SentinelTransport()))
    r = _place(client, agent="ghost")
    assert r.status_code == 409
    assert "ghost" in r.text


def test_place_refuses_a_disabled_agent(make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _doc(enabled=False))
    client = TestClient(make_app(SentinelTransport()))
    r = _place(client)
    assert r.status_code == 409
    assert "enabled: false" in r.text


def test_place_does_not_require_the_activated_outbound(
        make_app, monkeypatch, tmp_path):
    """The old fire path 409'd unless the draft WAS the activated outbound.
    One-shot names the agent; the pointer can say something else, or nothing."""
    _wire(monkeypatch, tmp_path, _doc(), _doc(AGENT_B), phone_outbound=None)
    transport = place_transport()
    client = TestClient(make_app(transport))
    r = _place(client, agent="agent-a")
    assert r.status_code == 200, r.text
    assert json.loads(_dials(transport)[0].content)["agent"] == "agent-a"


def test_place_surfaces_a_bridge_refusal(make_app, monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _doc())
    transport = place_transport(mode_c_status=409)
    client = TestClient(make_app(transport))
    r = _place(client)
    assert r.status_code == 409
    assert "already in progress" in r.text


def test_test_call_is_gone(make_app, monkeypatch, tmp_path):
    """The dry-run / Test-screen endpoint is deleted, not aliased."""
    _wire(monkeypatch, tmp_path, _doc())
    client = TestClient(make_app(SentinelTransport()))
    assert client.post("/api/test-call", json={
        "doc": _doc(), "to": OWNER}).status_code == 404
    assert client.post("/api/test-call", json={
        "doc": _doc(), "to": OWNER, "fire": True, "brief": "x"}).status_code == 404


def test_place_takes_a_tools_off_direct_hermes_agent(make_app, monkeypatch, tmp_path):
    """The tools setting is per Agent: Hermes enforces tool_choice, so a direct Agent with
    tools off is a valid outbound call. It used to be refused with 'cannot be sandboxed'
    before the dial. What reaches the bridge is the Agent's name; the bridge sends the
    switch (services/voice/tests/test_tool_toggle.py)."""
    direct = {"id": "agent-direct", "enabled": True, "pipeline": "cascade",
              "providers": {"stt": "deepgram", "llm": "hermes-agent", "tts": "elevenlabs"},
              "knobs": {"voice": "el-voice-1"}, "guardrails": {"on_call_tools": False},
              "number_policy": {"allow": [OWNER]}}
    _wire(monkeypatch, tmp_path, direct)
    transport = place_transport()
    client = TestClient(make_app(transport))
    r = _place(client, agent="agent-direct")
    assert r.status_code == 200, r.text
    assert r.json()["placed"] is True
    (dial,) = _dials(transport)
    assert json.loads(dial.content)["agent"] == "agent-direct"
