"""c20/c33 effective-config preview: POST /api/agents/preview must return EXACTLY what
each bridge's own builder produces with the draft selected — dict/byte
equality against an independent invocation of the real builder in the bridge's venv
(never a reimplementation, never a test double). The golden nodes pin the full env
snapshot (VOICE_AGENT, active.yaml, VOICE_RETAIN_ENABLED, VOICE_OUTBOUND_ALLOWED_NUMBERS)
identically on both sides, and run both with VOICE_AGENT unset AND set to a DECOY id —
preview-of-X previews X as-if-selected either way (the declared c33 semantics).

These nodes spawn the bridge venvs (services/voice/.venv, services/talk-voice-bridge/
.venv) — offline: the builders construct payloads/URLs without dialing anything.
"""
import copy
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from conftest import SentinelTransport
from voicecore import profiles

APP_DIR = Path(__file__).resolve().parent.parent
SERVICES = APP_DIR.parent
BRIDGE_DIRS = {"mode-c": SERVICES / "voice", "mode-v": SERVICES / "talk-voice-bridge"}

# Probe draft (contract obj. 5 shape): voice marin + non-default model; vad.silence_ms
# deliberately UNSET so the mode-c/mode-v divergent coded defaults (500 vs 250) stay
# visible between the two previews (c20's D4 guard).
PROBE_DOC = {
    "id": "probe-x",
    "description": "preview probe",
    "pipeline": "realtime",
    "providers": {"realtime": "openai-gpt-realtime"},
    "knobs": {"voice": "marin", "model": "gpt-realtime-mini",
              "vad": {"threshold": 0.7, "prefix_padding_ms": 120}},
    "persona": "Preview marker persona sentence.",
    "memory": {"retain": True},
}

DECOY_DOC = {
    "id": "decoy-agent",
    "pipeline": "realtime",
    "providers": {"realtime": "openai-gpt-realtime"},
    "knobs": {"voice": "cedar", "model": "gpt-realtime-decoy"},
}

# Independent invocation of each bridge's REAL builder, exactly as its call-setup path
# threads the snapshot (media_stream / CallSession.start). Run in the bridge's venv with
# VOICE_AGENT genuinely selecting the probe — no staging, no helper reuse.
BUILDER_PROGS = {
    "mode-c": """
import asyncio, json
import server
from voicecore import profiles
snapshot = profiles.load_effective_profile("inbound")
class WS:
    def __init__(self): self.raw = []
    async def send(self, d): self.raw.append(d)
ws = WS()
url = server._realtime_url(profile=snapshot, direction="inbound")
tok = server._CALL_PROFILE.set(snapshot)
try:
    asyncio.run(server._send_session_update(ws, server.build_system_prompt(), outbound=False))
finally:
    server._CALL_PROFILE.reset(tok)
print(json.dumps({"url": url, "session_update": json.loads(ws.raw[0])}))
""",
    "mode-v": """
import asyncio, json
import config, hermes, realtime_bridge
from voicecore import profiles
from approval import ApprovalStore
snapshot = profiles.load_effective_profile("inbound")
cfg = config.overlay_profile(config.load_base(), snapshot)
class WS:
    def __init__(self): self.raw = []
    async def send(self, d): self.raw.append(d)
ws = WS()
bridge = realtime_bridge.RealtimeBridge(
    cfg, hermes.build_system_prompt(cfg.config_dir, trust="owner"), ApprovalStore(),
    token_ctx={"token": "t", "caller": ""}, mission=None, profile=snapshot)
asyncio.run(bridge._send_session_update(ws))
print(json.dumps({"url": realtime_bridge.realtime_url(cfg),
                  "session_update": json.loads(ws.raw[0])}))
""",
}


def _avoid_minute_boundary():
    """Both prompt builders embed 'Current date/time: … %H:%M'. Keep the endpoint call
    and the independent builder run inside one wall-clock minute."""
    if datetime.now().second > 40:
        time.sleep(61 - datetime.now().second)


def _minute() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M")


def _sample_both(take_preview, take_builder):
    """Take the two samples and prove they were taken inside ONE wall-clock minute.

    The pair is a TestClient call plus a subprocess launch, and 40s of headroom is
    not always enough on a loaded machine: the run straddles a minute boundary and
    the two otherwise byte-identical payloads differ by one digit inside
    'Current date/time'. Sleeping until just after a boundary on every case would
    add minutes to the suite, so take the samples, check whether the minute rolled
    underneath them, and take them again if it did. Never compare across a
    boundary, and never paper over a real diff by retrying a failed assertion -
    the retry is driven by the clock, not by the comparison.
    """
    for _ in range(3):
        _avoid_minute_boundary()
        started = _minute()
        preview = take_preview()
        builder = take_builder()
        if _minute() == started:
            return preview, builder
    raise AssertionError(
        "could not sample the endpoint and the builder inside one wall-clock "
        "minute after 3 attempts - the builder subprocess is taking >60s")


def _run_builder(bridge: str, env: dict) -> dict:
    service_dir = BRIDGE_DIRS[bridge]
    proc = subprocess.run(
        [str(service_dir / ".venv" / "bin" / "python"), "-c", BUILDER_PROGS[bridge]],
        cwd=str(service_dir), env=env, capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, f"{bridge} builder program failed: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout)


@pytest.fixture
def preview_env(make_app, monkeypatch, tmp_path):
    """Tmp config dir with the probe + a decoy agent, active.yaml pointing at the DECOY
    for both directions, retain/allow env pinned — the c20 golden env snapshot."""
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "probe-x.yaml").write_text(yaml.safe_dump(PROBE_DOC, sort_keys=False))
    (agents / "decoy-agent.yaml").write_text(yaml.safe_dump(DECOY_DOC, sort_keys=False))
    (tmp_path / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {o: {"inbound": "decoy-agent", "outbound": "decoy-agent"}
                     for o in profiles.OUTLETS}}))
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("VOICE_RETAIN_ENABLED", "false")
    monkeypatch.setenv("VOICE_OUTBOUND_ALLOWED_NUMBERS", "+15550001111")
    return TestClient(make_app(SentinelTransport()))


@pytest.mark.parametrize("bridge", ["mode-c", "mode-v"])
@pytest.mark.parametrize("voice_agent_env", [None, "decoy-agent"],
                         ids=["env_unset", "env_set_to_decoy"])
def test_preview_equals_bridge_builder(preview_env, monkeypatch, bridge,
                                       voice_agent_env):
    """Byte equality endpoint-vs-real-builder, with and without VOICE_AGENT set (c33:
    preview-of-X stays X-as-if-selected even while env selects the decoy)."""
    client = preview_env
    if voice_agent_env is not None:
        monkeypatch.setenv("VOICE_AGENT", voice_agent_env)

    # Independent side: the bridge's own builder with probe-x GENUINELY selected.
    builder_env = dict(os.environ)
    builder_env["VOICE_AGENT"] = "probe-x"
    r, expected = _sample_both(
        lambda: client.post("/api/agents/preview",
                            json={"bridge": bridge, "doc": PROBE_DOC}),
        lambda: _run_builder(bridge, builder_env))
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["url"] == expected["url"]
    assert (json.dumps(body["session_update"], sort_keys=True)
            == json.dumps(expected["session_update"], sort_keys=True))
    assert body["bridge"] == bridge and body["as_if_selected"] is True
    assert body["direction"] == "inbound" and body["agent_id"] == "probe-x"
    # Probe knobs land through the real precedence chain.
    session = body["session_update"]["session"]
    assert session["audio"]["output"]["voice"] == "marin"
    assert body["url"].endswith("?model=gpt-realtime-mini")
    assert session["audio"]["input"]["turn_detection"]["threshold"] == 0.7
    assert session["instructions"].endswith("Preview marker persona sentence.")
    # Retain composed from the same env snapshot: env false, profile true => true/profile.
    assert body["effective"] == {"retain": True, "retain_source": "profile"}


def test_preview_divergent_defaults_visible(preview_env):
    """D4 guard: a mode-v preview must NOT reuse mode-c defaults — the coded
    divergences (audio format, vad silence default) show between the two flavors."""
    client = preview_env
    payloads = {}
    for bridge in ("mode-c", "mode-v"):
        r = client.post("/api/agents/preview",
                        json={"bridge": bridge, "doc": PROBE_DOC})
        assert r.status_code == 200, r.text
        payloads[bridge] = r.json()["session_update"]["session"]
    c, v = payloads["mode-c"], payloads["mode-v"]
    assert c["audio"]["input"]["format"] == {"type": "audio/pcmu"}
    assert v["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert c["audio"]["input"]["turn_detection"]["silence_duration_ms"] == 500
    assert v["audio"]["input"]["turn_detection"]["silence_duration_ms"] == 250


def test_preview_env_fallback_retain(preview_env, monkeypatch):
    """Retain absent on the draft => the env value governs (source 'env')."""
    client = preview_env
    doc = copy.deepcopy(PROBE_DOC)
    del doc["memory"]
    r = client.post("/api/agents/preview", json={"bridge": "mode-c", "doc": doc})
    assert r.status_code == 200, r.text
    assert r.json()["effective"] == {"retain": False, "retain_source": "env"}


# -- request validation (no subprocess spawned on any of these) ----------------

def test_preview_rejects_unknown_bridge(preview_env):
    r = preview_env.post("/api/agents/preview",
                         json={"bridge": "mode-x", "doc": PROBE_DOC})
    assert r.status_code == 422
    assert "mode-x" in r.text and "mode-c" in r.text


def test_preview_rejects_invalid_doc_via_validate_profile(preview_env):
    bad = copy.deepcopy(PROBE_DOC)
    bad["knobs"]["vad"]["threshold"] = 7
    r = preview_env.post("/api/agents/preview", json={"bridge": "mode-c", "doc": bad})
    assert r.status_code == 422
    assert any("knobs.vad.threshold" in e for e in r.json()["detail"])


def test_preview_rejects_bad_id(preview_env):
    bad = copy.deepcopy(PROBE_DOC)
    bad["id"] = "../escape"
    r = preview_env.post("/api/agents/preview", json={"bridge": "mode-c", "doc": bad})
    assert r.status_code == 422


def test_preview_refuses_disabled_draft(preview_env):
    """Activation refusals surface honestly (the REAL _activate message, not a 500)."""
    bad = copy.deepcopy(PROBE_DOC)
    bad["enabled"] = False
    r = preview_env.post("/api/agents/preview", json={"bridge": "mode-c", "doc": bad})
    assert r.status_code == 422
    assert "enabled: false" in r.json()["detail"][0]
