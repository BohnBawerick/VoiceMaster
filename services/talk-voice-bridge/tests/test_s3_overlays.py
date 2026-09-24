"""s3 tests, Mode V: active.yaml pointer (c12/c13/c15/c31), retain overlay at the
consumer (c16), on_call_tools on real session.update payloads (c17/c32), the
number_policy pre-dial gate (c18), and single-load TOCTOU (c19).

The call-setup path under test is the REAL one: CallSession.start resolves the
profile once, overlays the Config and threads the snapshot into RealtimeBridge;
run() dials the (monkeypatched) websocket with the profile URL and sends the real
session.update. parec/pacat are swapped for inert commands; nothing touches the
network (websockets + HTTP delivery are patched; retain is a spy).
"""
import asyncio
import json

import pytest
import yaml
from fastapi.testclient import TestClient

import config
from voicecore import hindsight
import outbound as outbound_mod
from voicecore import profiles
import realtime_bridge
import server
import session as session_mod
from approval import ApprovalStore
from outbound import OutboundMission
from session import CallSession

import parity_env as pe
from profile_helpers import profile_doc, write_config_dir

PROBE_MODEL = "gpt-realtime-probe-s3"


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


def _scrub(monkeypatch):
    for var in pe.CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


def _point_env(monkeypatch, d, voice_agent=None):
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
    """Async CM + async-iterable: yields scripted events once, records sends."""

    def __init__(self, events=()):
        self._events = [json.dumps(e) for e in events]
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        return None

    def __aiter__(self):
        async def _gen():
            for m in self._events:
                yield m
        return _gen()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def _drive(monkeypatch, *, mission=None, events=()):
    """One full Mode V call setup through CallSession.start; returns (urls, fake, ok)."""
    urls = []
    fake = FakeRealtimeWS(events)

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
    ok = await sess.start("tok-s3", "owner", "owner", "Owner", mission=mission)
    if ok and sess._run_task is not None:
        await asyncio.wait_for(sess._run_task, 10)
        await asyncio.sleep(0)  # let the done-callback reconcile task spawn
        if sess._reconcile_task is not None:
            await asyncio.wait_for(sess._reconcile_task, 10)
    return urls, fake, ok


def _session_update(fake):
    ups = [m for m in fake.sent if m.get("type") == "session.update"]
    assert len(ups) == 1, f"expected exactly one session.update, got {len(ups)}"
    return ups[0]["session"]


# -- c12: pointer selects the profile per direction (env unset) ----------------

@pytest.mark.asyncio
async def test_active_pointer_selects_profile_inbound(monkeypatch, tmp_path):
    _scrub(monkeypatch)
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, inbound="t-agent")
    _point_env(monkeypatch, d)
    urls, fake, ok = await _drive(monkeypatch)
    assert ok
    assert urls == [f"wss://api.openai.com/v1/realtime?model={PROBE_MODEL}"]
    assert _session_update(fake)["audio"]["output"]["voice"] == "marin"


@pytest.mark.asyncio
async def test_active_pointer_selects_profile_outbound(monkeypatch, tmp_path):
    _scrub(monkeypatch)
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, outbound="t-agent")
    _point_env(monkeypatch, d)
    urls, fake, ok = await _drive(monkeypatch, mission=OutboundMission(brief="b"))
    assert ok
    assert urls == [f"wss://api.openai.com/v1/realtime?model={PROBE_MODEL}"]
    sess = _session_update(fake)
    assert sess["audio"]["output"]["voice"] == "marin"
    assert sess["tools"] == [] and sess["tool_choice"] == "none"  # sandbox intact


@pytest.mark.asyncio
async def test_null_direction_is_no_profile(monkeypatch, tmp_path):
    """Pointer set for OUTBOUND only: the INBOUND setup is byte-identical no-profile."""
    _scrub(monkeypatch)
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, inbound=None, outbound="t-agent")
    _point_env(monkeypatch, d)
    urls, fake, ok = await _drive(monkeypatch)
    assert ok
    assert urls == ["wss://api.openai.com/v1/realtime?model=gpt-realtime-2"]
    golden = pe.load_golden_json("golden_mv_session_inbound.json")["session"]
    got = _session_update(fake)
    assert got["audio"] == golden["audio"]
    assert got["tools"] == golden["tools"]


# -- c13: env always wins over the pointer -------------------------------------

@pytest.mark.asyncio
async def test_env_voice_agent_beats_pointer(monkeypatch, tmp_path):
    _scrub(monkeypatch)
    a = _probe_doc(id="agent-a")
    b = profile_doc(id="agent-b", knobs={"voice": "cedar", "model": "gpt-b-model"})
    d = write_config_dir(tmp_path, [a, b])
    _pointer(d, inbound="agent-b", outbound="agent-b")
    _point_env(monkeypatch, d, voice_agent="agent-a")
    urls, fake, ok = await _drive(monkeypatch)
    assert ok
    assert urls == [f"wss://api.openai.com/v1/realtime?model={PROBE_MODEL}"]  # a's
    assert _session_update(fake)["audio"]["output"]["voice"] == "marin"       # a's


@pytest.mark.asyncio
async def test_blank_env_falls_through_to_pointer(monkeypatch, tmp_path):
    _scrub(monkeypatch)
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, inbound="t-agent")
    _point_env(monkeypatch, d)
    monkeypatch.setenv("VOICE_AGENT", "   ")
    urls, fake, ok = await _drive(monkeypatch)
    assert ok
    assert urls == [f"wss://api.openai.com/v1/realtime?model={PROBE_MODEL}"]
    assert _session_update(fake)["audio"]["output"]["voice"] == "marin"


# -- c15: pointer to missing/disabled agent fails loud -------------------------

@pytest.mark.asyncio
async def test_pointer_missing_agent_hard_fails(monkeypatch, tmp_path):
    _scrub(monkeypatch)
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, inbound="ghost-agent")
    _point_env(monkeypatch, d)
    with pytest.raises(profiles.ProfileError) as exc:
        profiles.load_effective_profile("inbound")
    assert "ghost-agent" in str(exc.value)
    urls, fake, ok = await _drive(monkeypatch)
    assert ok is False and urls == [] and fake.sent == []   # call refused, no dial


@pytest.mark.asyncio
async def test_pointer_disabled_agent_hard_fails(monkeypatch, tmp_path):
    _scrub(monkeypatch)
    d = write_config_dir(tmp_path, [_probe_doc(enabled=False)])
    _pointer(d, outbound="t-agent")
    _point_env(monkeypatch, d)
    with pytest.raises(profiles.ProfileError) as exc:
        profiles.load_effective_profile("outbound")
    assert "t-agent" in str(exc.value) and "enabled" in str(exc.value)
    urls, fake, ok = await _drive(monkeypatch, mission=OutboundMission(brief="b"))
    assert ok is False and urls == [] and fake.sent == []


# -- c31: malformed/partial active.yaml, per direction -------------------------

@pytest.mark.asyncio
async def test_partial_active_yaml_per_direction(monkeypatch, tmp_path):
    _scrub(monkeypatch)
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, raw="outlets:\n  talk:\n    inbound: t-agent\nsurprise: ignored\n")
    _point_env(monkeypatch, d)
    assert profiles.load_effective_profile("outbound") is None
    urls, fake, ok = await _drive(monkeypatch, mission=OutboundMission(brief="b"))
    assert ok and urls == ["wss://api.openai.com/v1/realtime?model=gpt-realtime-2"]

    _pointer(d, raw="outlets:\n  phone:\n    inbound: {bad: value}\n"
                    "    outbound: t-agent\n")
    with pytest.raises(profiles.ProfileError) as exc:
        profiles.load_effective_profile("inbound")              # bad value: loud
    assert "outlets.phone.inbound" in str(exc.value)
    p = profiles.load_effective_profile("outbound")             # other side unaffected
    assert p is not None and p.agent_id == "t-agent"


# -- c16: memory.retain overlay, asserted at the retain consumer ---------------

RETAIN_MATRIX = [
    ("profile-true-env-false", True, "false", True),
    ("profile-true-env-true", True, "true", True),
    ("profile-false-env-true", False, "true", False),
    ("profile-false-env-false", False, "false", False),
    ("absent-env-true", None, "true", True),
    ("absent-env-false", None, "false", False),
    ("no-profile-env-true", "none", "true", True),
    ("no-profile-env-false", "none", "false", False),
]

_TRANSCRIPT_EVENT = {"type": "conversation.item.input_audio_transcription.completed",
                     "transcript": "hello there"}


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["inbound", "outbound"])
@pytest.mark.parametrize("case,profile_retain,env_value,fires",
                         RETAIN_MATRIX, ids=[c[0] for c in RETAIN_MATRIX])
async def test_retain_overlay_matrix(monkeypatch, tmp_path, case, profile_retain,
                                     env_value, fires, direction):
    """hindsight.retain_detached (the consumer in run()'s teardown) fires or refrains
    per profile>env precedence — never asserted via config properties."""
    _scrub(monkeypatch)
    monkeypatch.setenv("VOICE_RETAIN_ENABLED", env_value)
    calls = []
    monkeypatch.setattr(hindsight, "retain_detached",
                        lambda url, bank, **kw: calls.append(kw) or True)
    if profile_retain == "none":
        monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path / "absent"))
    else:
        doc = _probe_doc()
        if profile_retain is not None:
            doc["memory"] = {"retain": profile_retain}
        d = write_config_dir(tmp_path, [doc])
        _pointer(d, **{direction: "t-agent"})
        _point_env(monkeypatch, d)
    mission = OutboundMission(brief="b") if direction == "outbound" else None
    _, fake, ok = await _drive(monkeypatch, mission=mission,
                               events=[_TRANSCRIPT_EVENT])
    assert ok
    assert (len(calls) == 1) is fires, f"{case}/{direction}: retain consumer mismatch"


# -- c17: on_call_tools — five cases on actual session.update payloads ---------

def _capture(monkeypatch, tmp_path, *, outbound, doc=None, name="vcfg"):
    """Real per-call composition (load once -> overlay -> bridge) -> raw payload."""
    if doc is not None:
        d = write_config_dir(tmp_path, [doc], name=name)
        _point_env(monkeypatch, d, voice_agent=doc["id"])
    mission = OutboundMission(brief="parity mission") if outbound else None
    profile = profiles.load_effective_profile("outbound" if outbound else "inbound")
    cfg = config.overlay_profile(config.load_base(), profile)
    bridge = realtime_bridge.RealtimeBridge(
        cfg, pe.BASE_PROMPT, ApprovalStore(),
        token_ctx={"token": "t", "caller": "c"}, mission=mission, profile=profile)
    ws = pe.RecordingWS()
    asyncio.run(bridge._send_session_update(ws))
    return ws.raw[0]


@pytest.mark.parametrize("case", ["true", "false", "absent", "no_profile",
                                  "inbound_ignored"])
def test_on_call_tools_five_cases(monkeypatch, tmp_path, case):
    _scrub(monkeypatch)
    if case == "true":
        doc = profile_doc(guardrails={"on_call_tools": True})
        out = json.loads(_capture(monkeypatch, tmp_path, outbound=True, doc=doc,
                                  name="vcfg-out"))
        inn = json.loads(_capture(monkeypatch, tmp_path, outbound=False, doc=doc,
                                  name="vcfg-in"))
        # s11a c3: on_call_tools OPENS tools on outbound, but the outbound export is
        # hermes_agent ONLY — the inbound-guest request_owner_approval tool is dropped.
        # Inbound keeps the full set.
        out_names = [t["name"] for t in out["session"]["tools"]]
        inn_names = [t["name"] for t in inn["session"]["tools"]]
        assert out_names == ["hermes_agent"]
        assert "request_owner_approval" not in out_names
        assert "hermes_agent" in inn_names and "request_owner_approval" in inn_names
        assert out["session"]["tool_choice"] == inn["session"]["tool_choice"] == "auto"
    elif case in ("false", "absent"):
        guard = {"guardrails": {"on_call_tools": False}} if case == "false" else {}
        doc = profile_doc(**guard)
        raw = _capture(monkeypatch, tmp_path, outbound=True, doc=doc)
        assert raw == pe.load_golden("golden_mv_session_outbound.json")
    elif case == "no_profile":
        raw = _capture(monkeypatch, tmp_path, outbound=True)
        assert raw == pe.load_golden("golden_mv_session_outbound.json")
    else:
        raws = []
        for i, guard in enumerate(({"on_call_tools": True},
                                   {"on_call_tools": False}, None)):
            doc = profile_doc() if guard is None else profile_doc(guardrails=guard)
            raws.append(_capture(monkeypatch, tmp_path, outbound=False, doc=doc,
                                 name=f"vcfg-{i}"))
        assert raws[0] == raws[1] == raws[2]


def test_on_call_tools_and_deny_all_compose(monkeypatch, tmp_path):
    """c32: on_call_tools true + talk_policy.allow [] on ONE profile: tools open on the
    session while the pre-dial gate denies every candidate. (s11b-1: the Talk gate now
    reads talk_policy.allow, not number_policy.allow.)"""
    _scrub(monkeypatch)
    doc = profile_doc(guardrails={"on_call_tools": True}, talk_policy={"allow": []})
    out = json.loads(_capture(monkeypatch, tmp_path, outbound=True, doc=doc,
                              name="vcfg-out"))
    inn = json.loads(_capture(monkeypatch, tmp_path, outbound=False, doc=doc,
                              name="vcfg-in"))
    # s11a c3: outbound opens tools (hermes_agent only) while the gate still denies all.
    assert [t["name"] for t in out["session"]["tools"]] == ["hermes_agent"]
    assert inn["session"]["tools"] != []
    p = profiles.load_effective_profile("outbound")
    assert p.talk_allow_list() == []                # the gate input: deny-all
    client = TestClient(server.app, headers={
        "Authorization": f"Bearer {server._cfg.hermes_gateway_token}"})
    r = client.post("/call/outbound", json={"brief": "hi", "target": "anyone"})
    assert r.status_code == 403


# -- c18 (s11b-1): talk_policy.allow REPLACES the allow-any Talk posture; the E.164
# number_policy.allow no longer gates a Talk dial. (Full matrix: test_s11b1_gate.py.)

@pytest.mark.parametrize("case", ["permit", "refuse", "deny_all", "absent",
                                  "number_policy_inert"])
def test_talk_policy_replaces_posture(monkeypatch, tmp_path, case):
    _scrub(monkeypatch)

    class OkSession:
        async def start(self, *a, **kw):
            return True

    monkeypatch.setattr(server, "_session", OkSession())
    if case == "number_policy_inert":
        doc = profile_doc(number_policy={"allow": ["+15550002222"]})
    elif case == "absent":
        doc = profile_doc()
    else:
        allow = [] if case == "deny_all" else ["roomtok"]
        doc = profile_doc(talk_policy={"allow": allow})
    d = write_config_dir(tmp_path, [doc])
    _point_env(monkeypatch, d, voice_agent="t-agent")
    client = TestClient(server.app, headers={
        "Authorization": f"Bearer {server._cfg.hermes_gateway_token}"})

    def dial(target):
        return client.post("/call/outbound",
                           json={"brief": "hi", "token": target}).status_code

    if case == "permit":
        assert dial("roomtok") == 200
    elif case == "refuse":
        assert dial("otherroom") == 403             # replace, never a union
    elif case == "deny_all":
        assert dial("roomtok") == 403 and dial("otherroom") == 403
    elif case == "number_policy_inert":
        assert dial("anyroom") == 200               # number_policy does NOT gate Talk
    else:
        assert dial("anyroom") == 200               # absent field: posture unchanged


# -- c19: single load per call setup; freshness on the NEXT call ---------------

@pytest.mark.asyncio
async def test_single_profile_load_per_call_setup(monkeypatch, tmp_path):
    _scrub(monkeypatch)
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, inbound="t-agent")
    _point_env(monkeypatch, d)
    loads = []
    real = profiles.load_effective_profile

    def counting(direction, outlet=None, env=None):
        loads.append((direction, outlet))
        return real(direction, outlet, env)

    monkeypatch.setattr(session_mod.profiles, "load_effective_profile", counting)
    urls, fake, ok = await _drive(monkeypatch)
    # s16: exactly one load, and it resolves the TALK outlet - the axis is not just
    # threaded, it is the bridge's own outlet.
    assert ok and loads == [("inbound", profiles.OUTLET_TALK)], \
        f"expected exactly one talk-outlet load, got {loads}"
    assert urls == [f"wss://api.openai.com/v1/realtime?model={PROBE_MODEL}"]
    assert _session_update(fake)["audio"]["output"]["voice"] == "marin"


@pytest.mark.asyncio
async def test_profile_swap_mid_setup_is_coherent(monkeypatch, tmp_path):
    _scrub(monkeypatch)
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, inbound="t-agent")
    _point_env(monkeypatch, d)
    real = profiles.load_effective_profile

    def load_then_swap(direction, outlet=None, env=None):
        snapshot = real(direction, outlet, env)
        (d / "agents" / "agent0.yaml").write_text(yaml.safe_dump(
            profile_doc(knobs={"voice": "cedar", "model": "gpt-swapped"})))
        return snapshot

    monkeypatch.setattr(session_mod.profiles, "load_effective_profile", load_then_swap)
    urls, fake, ok = await _drive(monkeypatch)
    assert ok
    assert urls == [f"wss://api.openai.com/v1/realtime?model={PROBE_MODEL}"]  # pre-swap
    assert _session_update(fake)["audio"]["output"]["voice"] == "marin"       # pre-swap


@pytest.mark.asyncio
async def test_next_setup_observes_new_profile(monkeypatch, tmp_path):
    _scrub(monkeypatch)
    d = write_config_dir(tmp_path, [_probe_doc()])
    _pointer(d, inbound="t-agent")
    _point_env(monkeypatch, d)
    _, fake1, ok1 = await _drive(monkeypatch)
    assert ok1 and _session_update(fake1)["audio"]["output"]["voice"] == "marin"

    (d / "agents" / "agent0.yaml").write_text(yaml.safe_dump(
        profile_doc(knobs={"voice": "verse", "model": "gpt-next"})))
    urls, fake2, ok2 = await _drive(monkeypatch)
    assert ok2 and urls == ["wss://api.openai.com/v1/realtime?model=gpt-next"]
    assert _session_update(fake2)["audio"]["output"]["voice"] == "verse"
