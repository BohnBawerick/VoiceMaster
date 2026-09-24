"""s3 tests, Mode C: active.yaml pointer (c12/c13/c15/c31), retain overlay at the
consumer (c16), on_call_tools on real session.update payloads (c17/c32), the
number_policy pre-dial gate (c18/c32), and single-load TOCTOU (c19).

Everything drives the REAL product paths: media_stream over a TestClient websocket
(URL captured from the monkeypatched websockets.connect, payload from the fake WS),
the /voice/outbound endpoint for the pre-dial gate, and profiles.load_effective_profile
for activation. No golden is regenerated; no-profile bytes stay pinned by test_parity.
"""
import asyncio
import json

import pytest
import yaml
from fastapi.testclient import TestClient

import parity_env as pe
from voicecore import profiles
import server
from conftest import FakeOpenAIWS
from outbound import OutboundMission
from profile_helpers import build_payload_and_url, profile_doc, write_config_dir
from test_bargein import ScriptedOpenAIWS

PROBE_MODEL = "gpt-realtime-probe-s3"  # non-default on purpose (pinned probe profile)


def _probe_doc(**overrides):
    doc = profile_doc(knobs={"voice": "marin", "model": PROBE_MODEL})
    doc.update(overrides)
    return doc


def _pointer(d, inbound=None, outbound=None, raw=None):
    """Assign both Outlets the same way. These tests predate the Outlet axis and
    are about direction resolution, not per-Outlet routing, so they say "both"
    explicitly - the shape that used to say it implicitly is gone (s17)."""
    text = raw if raw is not None else yaml.safe_dump(
        {"outlets": {o: {"inbound": inbound, "outbound": outbound}
                     for o in profiles.OUTLETS}})
    (d / "active.yaml").write_text(text)


def _point_env(monkeypatch, d, voice_agent=None):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(d))
    if voice_agent is None:
        monkeypatch.delenv("VOICE_AGENT", raising=False)
    else:
        monkeypatch.setenv("VOICE_AGENT", voice_agent)


@pytest.fixture
def client():
    return TestClient(server.app)


def _drive(client, monkeypatch, fake, *, outbound=False, to="+15550002222"):
    """Run one full media_stream call setup; returns the wss URLs dialed."""
    urls = []

    def connect(url, *a, **kw):
        urls.append(url)
        return fake

    monkeypatch.setattr(server.websockets, "connect", connect)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(server.eventlog, "append_event", lambda *a, **k: None)
    if outbound:
        async def _no_deliver(mission, transcript):
            return None
        monkeypatch.setattr(server, "deliver_transcript", _no_deliver)
        server._remember_mission("cid-s3", OutboundMission(brief="b", to=to))
        params = {"call_id": "cid-s3"}
    else:
        params = {"inbound_token": server._mint_inbound_token()}
    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZs3",
                      "start": {"streamSid": "MZs3", "callSid": "CAx",
                                "customParameters": params}})
        ws.send_json({"event": "stop", "sequenceNumber": "2", "streamSid": "MZs3"})
    return urls


def _session_update(fake):
    ups = [m for m in fake.sent if m.get("type") == "session.update"]
    assert len(ups) == 1, f"expected exactly one session.update, got {len(ups)}"
    return ups[0]["session"]


# -- c12: pointer selects the profile per direction (env unset) ----------------

def test_active_pointer_selects_profile_inbound(client, monkeypatch, tmp_path):
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, inbound="t-agent")
    _point_env(monkeypatch, d)
    fake = FakeOpenAIWS()
    urls = _drive(client, monkeypatch, fake)
    assert urls == [f"wss://api.openai.com/v1/realtime?model={PROBE_MODEL}"]
    sess = _session_update(fake)
    assert sess["audio"]["output"]["voice"] == "marin"


def test_active_pointer_selects_profile_outbound(client, monkeypatch, tmp_path):
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, outbound="t-agent")
    _point_env(monkeypatch, d)
    fake = FakeOpenAIWS()
    urls = _drive(client, monkeypatch, fake, outbound=True)
    assert urls == [f"wss://api.openai.com/v1/realtime?model={PROBE_MODEL}"]
    sess = _session_update(fake)
    assert sess["audio"]["output"]["voice"] == "marin"
    assert sess["tools"] == [] and sess["tool_choice"] == "none"  # sandbox intact


def test_null_direction_is_no_profile(client, monkeypatch, tmp_path):
    """Pointer names an agent for OUTBOUND only: the INBOUND setup is byte-identical
    no-profile (golden bytes) — no auto-default, no bleed between directions."""
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, inbound=None, outbound="t-agent")
    _point_env(monkeypatch, d)
    fake = FakeOpenAIWS()
    urls = _drive(client, monkeypatch, fake)
    assert urls == ["wss://api.openai.com/v1/realtime?model=gpt-realtime"]
    raw_inbound_golden = json.loads(pe.load_golden("golden_mc_session_inbound.json"))
    got = [m for m in fake.sent if m.get("type") == "session.update"][0]
    assert got["session"]["audio"]["output"]["voice"] \
        == raw_inbound_golden["session"]["audio"]["output"]["voice"]
    assert got["session"]["audio"] == raw_inbound_golden["session"]["audio"]
    assert got["session"]["tools"] == raw_inbound_golden["session"]["tools"]


# -- c13: env always wins over the pointer -------------------------------------

def test_env_voice_agent_beats_pointer(client, monkeypatch, tmp_path):
    a = _probe_doc(id="agent-a")
    b = profile_doc(id="agent-b", knobs={"voice": "cedar", "model": "gpt-b-model"})
    d = write_config_dir(tmp_path, [a, b])
    _pointer(d, inbound="agent-b", outbound="agent-b")
    _point_env(monkeypatch, d, voice_agent="agent-a")
    fake = FakeOpenAIWS()
    urls = _drive(client, monkeypatch, fake)
    assert urls == [f"wss://api.openai.com/v1/realtime?model={PROBE_MODEL}"]  # a's model
    assert _session_update(fake)["audio"]["output"]["voice"] == "marin"       # a's voice


def test_blank_env_falls_through_to_pointer(client, monkeypatch, tmp_path):
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, inbound="t-agent")
    _point_env(monkeypatch, d)
    monkeypatch.setenv("VOICE_AGENT", "   ")   # whitespace-only == unset
    fake = FakeOpenAIWS()
    urls = _drive(client, monkeypatch, fake)
    assert urls == [f"wss://api.openai.com/v1/realtime?model={PROBE_MODEL}"]
    assert _session_update(fake)["audio"]["output"]["voice"] == "marin"


# -- c15: pointer to missing/disabled agent fails loud -------------------------

def test_pointer_missing_agent_hard_fails(client, monkeypatch, tmp_path):
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, inbound="ghost-agent")
    _point_env(monkeypatch, d)
    with pytest.raises(profiles.ProfileError) as exc:
        profiles.load_effective_profile("inbound")
    assert "ghost-agent" in str(exc.value)
    # And the bridge REFUSES the call — no OpenAI dial, no session, no env fallback.
    fake = FakeOpenAIWS()
    urls = _drive(client, monkeypatch, fake)
    assert urls == [] and fake.sent == []


def test_pointer_disabled_agent_hard_fails(client, monkeypatch, tmp_path):
    d = write_config_dir(tmp_path, [_probe_doc(enabled=False)])
    _pointer(d, outbound="t-agent")
    _point_env(monkeypatch, d)
    with pytest.raises(profiles.ProfileError) as exc:
        profiles.load_effective_profile("outbound")
    msg = str(exc.value)
    assert "t-agent" in msg and "enabled" in msg
    fake = FakeOpenAIWS()
    urls = _drive(client, monkeypatch, fake, outbound=True)
    assert urls == [] and fake.sent == []


# -- c31: malformed/partial active.yaml, per direction -------------------------

def test_partial_active_yaml_per_direction(client, monkeypatch, tmp_path):
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, raw="outlets:\n  phone:\n    inbound: t-agent\nsurprise: ignored\n")
    _point_env(monkeypatch, d)
    assert profiles.load_effective_profile("outbound") is None    # missing key => null
    p = profiles.load_effective_profile("inbound")
    assert p is not None and p.agent_id == "t-agent"

    _pointer(d, raw="outlets:\n  phone:\n    inbound: [not, a, string]\n"
                    "    outbound: t-agent\n")
    with pytest.raises(profiles.ProfileError) as exc:
        profiles.load_effective_profile("inbound")                # bad value: loud
    assert "outlets.phone.inbound" in str(exc.value)
    p = profiles.load_effective_profile("outbound")               # other side unaffected
    assert p is not None and p.agent_id == "t-agent"


# -- c16: memory.retain overlay, asserted at the retain consumer ---------------

RETAIN_MATRIX = [
    # (case, profile_retain: True/False/None(absent)/"none"(no profile), env, fires)
    ("profile-true-env-false", True, False, True),
    ("profile-true-env-true", True, True, True),
    ("profile-false-env-true", False, True, False),
    ("profile-false-env-false", False, False, False),
    ("absent-env-true", None, True, True),
    ("absent-env-false", None, False, False),
    ("no-profile-env-true", "none", True, True),
    ("no-profile-env-false", "none", False, False),
]


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
@pytest.mark.parametrize("case,profile_retain,env_on,fires",
                         RETAIN_MATRIX, ids=[c[0] for c in RETAIN_MATRIX])
def test_retain_overlay_matrix(client, monkeypatch, tmp_path, case, profile_retain,
                               env_on, fires, direction):
    """The retain step (hindsight.retain_detached at teardown) fires or refrains per
    profile>env precedence — asserted at the CONSUMER call, not on config properties."""
    calls = []
    monkeypatch.setattr(server.hindsight, "retain_detached",
                        lambda url, bank, **kw: calls.append(kw) or True)
    # Mode C env config is a module constant; patching it is this suite's env knob.
    monkeypatch.setattr(server, "RETAIN_ENABLED", env_on)

    if profile_retain == "none":
        monkeypatch.delenv("VOICE_AGENT", raising=False)
        monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path / "absent"))
    else:
        doc = _probe_doc()
        if profile_retain is not None:
            doc["memory"] = {"retain": profile_retain}
        d = write_config_dir(tmp_path, [doc])
        _pointer(d, **{direction: "t-agent"})
        _point_env(monkeypatch, d)

    fake = ScriptedOpenAIWS([
        {"type": "conversation.item.input_audio_transcription.completed",
         "transcript": "hello there"},
    ])
    _drive(client, monkeypatch, fake, outbound=direction == "outbound")
    assert (len(calls) == 1) is fires, f"{case}/{direction}: retain consumer mismatch"


# -- c17: on_call_tools — five cases on actual session.update payloads ---------

def _payload(env, *, outbound):
    raw, _ = build_payload_and_url(env, outbound=outbound)
    return json.loads(raw)["session"]


@pytest.mark.parametrize("case", ["true", "false", "absent", "no_profile",
                                  "inbound_ignored"])
def test_on_call_tools_five_cases(tmp_path, case):
    golden_out = json.loads(pe.load_golden("golden_mc_session_outbound.json"))["session"]

    if case == "true":
        d = write_config_dir(tmp_path, [profile_doc(guardrails={"on_call_tools": True})])
        env = {"VOICE_CONFIG_DIR": str(d), "VOICE_AGENT": "t-agent"}
        out, inn = _payload(env, outbound=True), _payload(env, outbound=False)
        assert out["tools"] == inn["tools"] and out["tools"] != []
        assert out["tool_choice"] == inn["tool_choice"]
    elif case in ("false", "absent"):
        guard = {"guardrails": {"on_call_tools": False}} if case == "false" else {}
        d = write_config_dir(tmp_path, [profile_doc(**guard)])
        env = {"VOICE_CONFIG_DIR": str(d), "VOICE_AGENT": "t-agent"}
        raw, _ = build_payload_and_url(env, outbound=True)
        assert raw == pe.load_golden("golden_mc_session_outbound.json")  # today's cut
    elif case == "no_profile":
        raw, _ = build_payload_and_url(None, outbound=True)
        assert raw == pe.load_golden("golden_mc_session_outbound.json")
        assert golden_out["tools"] == [] and golden_out["tool_choice"] == "none"
    else:  # inbound_ignored: the inbound payload never reads the field
        raws = []
        for guard in ({"on_call_tools": True}, {"on_call_tools": False}, None):
            doc = profile_doc() if guard is None else profile_doc(guardrails=guard)
            d = write_config_dir(tmp_path, [doc], name=f"vcfg-{len(raws)}")
            raw, _ = build_payload_and_url(
                {"VOICE_CONFIG_DIR": str(d), "VOICE_AGENT": "t-agent"}, outbound=False)
            raws.append(raw)
        assert raws[0] == raws[1] == raws[2]


# -- c18: number_policy.allow REPLACES env at the pre-dial gate ----------------

class _FakeCall:
    sid = "CAs3"


class _FakeClient:
    def __init__(self, *a, **kw):
        self.calls = self

    def create(self, **kw):
        return _FakeCall()


@pytest.mark.parametrize("case", ["permit", "refuse", "deny_all", "env_fallback"])
def test_number_policy_replaces_env(client, monkeypatch, tmp_path, case):
    monkeypatch.setattr(server, "Client", _FakeClient)
    monkeypatch.setattr(server, "ALLOWED_OUTBOUND", frozenset({"+15550001111"}))
    # A successful dial in an earlier param queues a mission; without this the
    # env_fallback case 409s on the busy guard (pre-existing isolation leak).
    server._MISSIONS.clear()
    server._ACTIVE_OUTBOUND.clear()

    if case != "env_fallback":
        doc = profile_doc(number_policy={
            "allow": [] if case == "deny_all" else ["+15550002222"]})
        d = write_config_dir(tmp_path, [doc])
        _point_env(monkeypatch, d, voice_agent="t-agent")
    else:
        doc = profile_doc()  # no number_policy field => env governs unchanged
        d = write_config_dir(tmp_path, [doc])
        _point_env(monkeypatch, d, voice_agent="t-agent")

    def dial(to):
        return client.post("/voice/outbound",
                           headers={"Authorization": "Bearer test-token"},
                           json={"brief": "hi", "to": to}).status_code

    if case == "permit":
        assert dial("+15550002222") == 200
    elif case == "refuse":
        # env-allowed but NOT profile-allowed: a union would pass — replace refuses.
        assert dial("+15550001111") == 403
    elif case == "deny_all":
        assert dial("+15550001111") == 403 and dial("+15550002222") == 403
    else:
        assert dial("+15550001111") == 200 and dial("+15550002222") == 403


# -- c32: guardrails compose — tools open while the gate denies all ------------

def test_on_call_tools_and_deny_all_compose(client, monkeypatch, tmp_path):
    doc = profile_doc(guardrails={"on_call_tools": True}, number_policy={"allow": []})
    d = write_config_dir(tmp_path, [doc])
    _pointer(d, outbound="t-agent")
    _point_env(monkeypatch, d)
    # The pre-dial gate denies EVERY candidate…
    monkeypatch.setattr(server, "Client", _FakeClient)
    monkeypatch.setattr(server, "ALLOWED_OUTBOUND", frozenset({"+15550001111"}))
    for n in ("+15550001111", "+15550002222"):
        r = client.post("/voice/outbound", headers={"Authorization": "Bearer test-token"},
                        json={"brief": "hi", "to": n})
        assert r.status_code == 403
    # …while the SAME profile's outbound session carries the inbound-equivalent tools.
    env = {"VOICE_CONFIG_DIR": str(d), "VOICE_AGENT": "t-agent"}
    out, inn = _payload(env, outbound=True), _payload(env, outbound=False)
    assert out["tools"] == inn["tools"] != []
    assert out["tool_choice"] == inn["tool_choice"]


# -- c19: single load per call setup; freshness on the NEXT call ---------------

def test_single_profile_load_per_call_setup(client, monkeypatch, tmp_path):
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, inbound="t-agent")
    _point_env(monkeypatch, d)
    loads = []
    real = profiles.load_effective_profile

    def counting(direction, outlet=None, env=None):
        loads.append((direction, outlet))
        return real(direction, outlet, env)

    monkeypatch.setattr(server.profiles, "load_effective_profile", counting)
    fake = FakeOpenAIWS()
    urls = _drive(client, monkeypatch, fake)
    # s16: exactly one load, and it resolves the PHONE outlet - the axis is not just
    # threaded, it is the bridge's own outlet.
    assert loads == [("inbound", profiles.OUTLET_PHONE)], \
        f"expected exactly one phone-outlet load, got {loads}"
    assert urls == [f"wss://api.openai.com/v1/realtime?model={PROBE_MODEL}"]
    assert _session_update(fake)["audio"]["output"]["voice"] == "marin"


def test_profile_swap_mid_setup_is_coherent(client, monkeypatch, tmp_path):
    """The profile file is swapped immediately after the single load: URL and payload
    must BOTH reflect the pre-swap snapshot (a re-read anywhere would mix models)."""
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, inbound="t-agent")
    _point_env(monkeypatch, d)
    real = profiles.load_effective_profile

    def load_then_swap(direction, outlet=None, env=None):
        snapshot = real(direction, outlet, env)
        (d / "agents" / "agent0.yaml").write_text(yaml.safe_dump(
            profile_doc(knobs={"voice": "cedar", "model": "gpt-swapped"})))
        return snapshot

    monkeypatch.setattr(server.profiles, "load_effective_profile", load_then_swap)
    fake = FakeOpenAIWS()
    urls = _drive(client, monkeypatch, fake)
    assert urls == [f"wss://api.openai.com/v1/realtime?model={PROBE_MODEL}"]
    assert _session_update(fake)["audio"]["output"]["voice"] == "marin"


def test_next_setup_observes_new_profile(client, monkeypatch, tmp_path):
    """No forever-cache: an edit between two sequential setups lands on the second."""
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, inbound="t-agent")
    _point_env(monkeypatch, d)
    fake1 = FakeOpenAIWS()
    _drive(client, monkeypatch, fake1)
    assert _session_update(fake1)["audio"]["output"]["voice"] == "marin"

    (d / "agents" / "agent0.yaml").write_text(yaml.safe_dump(
        profile_doc(knobs={"voice": "verse", "model": "gpt-next"})))
    fake2 = FakeOpenAIWS()
    urls = _drive(client, monkeypatch, fake2)
    assert urls == ["wss://api.openai.com/v1/realtime?model=gpt-next"]
    assert _session_update(fake2)["audio"]["output"]["voice"] == "verse"
