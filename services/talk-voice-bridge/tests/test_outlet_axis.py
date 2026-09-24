"""s16 tests, Mode V: the outlet axis on the TALK call path.

The shared resolution matrix lives ONCE in the phone suite
(``voice/tests/test_outlet_axis.py`` - both suites import the same voicecore
module; duplicating the matrix is the twin-copy smell VC17 removed). What lives
HERE is what only this bridge can prove: CallSession.start and the /call/outbound
gate resolve the TALK outlet, even when the file assigns a different agent to the
phone outlet, and the deleted flat shape no longer answers a Talk call.
"""
import json

import pytest
import yaml
from fastapi.testclient import TestClient

import config
import outbound as outbound_mod
from voicecore import profiles
import realtime_bridge
import server
import session as session_mod
from approval import ApprovalStore
from outbound import OutboundMission
from session import CallSession

from profile_helpers import profile_doc, write_config_dir

PHONE_MODEL = "gpt-realtime-probe-phone"
TALK_MODEL = "gpt-realtime-probe-talk"


def _agent(aid, voice, model, **overrides):
    return profile_doc(id=aid, knobs={"voice": voice, "model": model}, **overrides)


def _write_outlet_shape(d, *, phone=None, talk=None):
    doc = {"outlets": {}}
    if phone is not None:
        doc["outlets"]["phone"] = phone
    if talk is not None:
        doc["outlets"]["talk"] = talk
    (d / "active.yaml").write_text(yaml.safe_dump(doc))


def _write_flat_shape(d, inbound=None, outbound=None):
    """The shape the old Agents screen used to write: a direction and no Outlet.
    Deleted with s17; kept here only so the test can prove it lands nowhere."""
    (d / "active.yaml").write_text(yaml.safe_dump(
        {"inbound": inbound, "outbound": outbound}))


def _point_env(monkeypatch, d, voice_agent=None):
    for var in ("VOICE_AGENT", "VOICE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(d))
    if voice_agent is not None:
        monkeypatch.setenv("VOICE_AGENT", voice_agent)


class FakeBrowser:
    async def join_call(self, token):
        return None

    async def start_call(self, token):
        return None

    async def leave_call(self):
        return None


class FakeRealtimeWS:
    """Async CM + async-iterable: yields no events, records sends."""

    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        return None

    def __aiter__(self):
        async def _gen():
            if False:  # pragma: no cover - no events needed for these assertions
                yield None
        return _gen()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def _drive(monkeypatch, *, mission=None):
    """One full Mode V call setup through CallSession.start."""
    urls = []
    fake = FakeRealtimeWS()

    def connect(url, **kw):
        urls.append(url)
        return fake

    monkeypatch.setattr(realtime_bridge.websockets, "connect", connect)
    monkeypatch.setattr(realtime_bridge, "parec_cmd", lambda rate: ["true"])
    monkeypatch.setattr(realtime_bridge, "pacat_cmd", lambda rate: ["cat"])

    async def _no_deliver(cfg, mission_, lines):
        return None

    monkeypatch.setattr(outbound_mod, "deliver_transcript", _no_deliver)
    sess = CallSession(config.load_base(), FakeBrowser(), ApprovalStore())
    ok = await sess.start("tok-s16", "owner", "owner", "Owner", mission=mission)
    if ok and sess._run_task is not None:
        await sess._run_task
        await sess._reconcile_task
    return urls, fake, ok


def _session_update(fake):
    ups = [m for m in fake.sent if m.get("type") == "session.update"]
    assert len(ups) == 1, f"expected exactly one session.update, got {len(ups)}"
    return ups[0]["session"]


# -- the Talk call path resolves the TALK outlet ------------------------------

@pytest.mark.asyncio
async def test_talk_inbound_call_uses_the_talk_outlet_agent(monkeypatch, tmp_path):
    """The wire-level pin: with DIFFERENT agents on the two outlets, a Talk inbound
    call must run the TALK outlet's agent - the phone assignment must not leak in."""
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL),
                                    _agent("talk-agent", "cedar", TALK_MODEL)])
    _write_outlet_shape(d,
                        phone={"inbound": "phone-agent", "outbound": "phone-agent"},
                        talk={"inbound": "talk-agent", "outbound": "talk-agent"})
    _point_env(monkeypatch, d)
    urls, fake, ok = await _drive(monkeypatch)
    assert ok
    assert urls == [f"wss://api.openai.com/v1/realtime?model={TALK_MODEL}"]
    assert _session_update(fake)["audio"]["output"]["voice"] == "cedar"


@pytest.mark.asyncio
async def test_talk_outbound_call_uses_the_talk_outlet_agent(monkeypatch, tmp_path):
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL),
                                    _agent("talk-agent", "cedar", TALK_MODEL)])
    _write_outlet_shape(d,
                        phone={"inbound": "phone-agent", "outbound": "phone-agent"},
                        talk={"inbound": "talk-agent", "outbound": "talk-agent"})
    _point_env(monkeypatch, d)
    urls, fake, ok = await _drive(monkeypatch,
                                  mission=OutboundMission(brief="b"))
    assert ok
    assert urls == [f"wss://api.openai.com/v1/realtime?model={TALK_MODEL}"]
    assert _session_update(fake)["audio"]["output"]["voice"] == "cedar"


@pytest.mark.asyncio
async def test_a_flat_file_does_not_answer_a_talk_call(monkeypatch, tmp_path):
    """The refusal on the REAL Talk call path (s17): a top-level direction names
    no Outlet, so the file the old screen used to write answers nothing. The Talk
    refusal happens in session start, before any bridge exists."""
    d = write_config_dir(tmp_path, [_agent("t-agent", "marin", TALK_MODEL)])
    _write_flat_shape(d, inbound="t-agent", outbound="t-agent")
    _point_env(monkeypatch, d)
    urls, fake, ok = await _drive(monkeypatch)
    assert not ok
    assert urls == []


@pytest.mark.asyncio
async def test_broken_talk_slot_refuses_the_talk_call(monkeypatch, tmp_path):
    """The loud bar on the live path: a broken TALK slot refuses the call - the slot
    fails loud, never silently falls back to the phone outlet's agent."""
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL)])
    _write_outlet_shape(d,
                        phone={"inbound": "phone-agent"},
                        talk={"inbound": "ghost-agent"})
    _point_env(monkeypatch, d)
    urls, fake, ok = await _drive(monkeypatch)
    assert ok is False and urls == [] and fake.sent == []


# -- the pre-dial gate reads the TALK outlet too ------------------------------

def test_outbound_gate_applies_the_talk_outlet_profile(monkeypatch, tmp_path):
    """/call/outbound's talk_policy gate must grade the TALK outlet's profile: the
    talk agent's deny-all gate refuses the dial even though the phone outlet's agent
    carries no gate."""
    d = write_config_dir(
        tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL),
                   _agent("talk-agent", "cedar", TALK_MODEL,
                          talk_policy={"allow": []})])
    _write_outlet_shape(d,
                        phone={"outbound": "phone-agent"},
                        talk={"outbound": "talk-agent"})
    _point_env(monkeypatch, d)
    client = TestClient(server.app, headers={
        "Authorization": f"Bearer {server._cfg.hermes_gateway_token}"})
    r = client.post("/call/outbound", json={"brief": "hi", "token": "anyroom"})
    assert r.status_code == 403
    assert "talk_policy" in r.json()["error"]


def test_readiness_reports_the_talk_outlet_agent(monkeypatch, tmp_path):
    """GET /readiness/cascade reports on the TALK outlet's activated outbound profile
    - never the phone outlet's."""
    d = write_config_dir(tmp_path, [_agent("phone-agent", "marin", PHONE_MODEL),
                                    _agent("talk-agent", "cedar", TALK_MODEL)])
    _write_outlet_shape(d,
                        phone={"outbound": "phone-agent"},
                        talk={"outbound": "talk-agent"})
    _point_env(monkeypatch, d)
    client = TestClient(server.app, headers={
        "Authorization": f"Bearer {server._cfg.hermes_gateway_token}"})
    r = client.get("/readiness/cascade")
    assert r.status_code == 200
    body = r.json()
    # Not cascade, so not ready - but the reported agent must be the TALK one.
    assert body["agent"] == "talk-agent"
    assert "not cascade" in body["error"]
