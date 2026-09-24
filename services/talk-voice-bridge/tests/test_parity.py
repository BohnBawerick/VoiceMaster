"""s1 no-profile parity tests, Mode V (c15–c22): with VOICE_AGENT unset/blank the
bridge must be byte-identical to the pre-change d36113c behavior — Config, the
session.update wire bytes, and the wss URL. Goldens were captured from the clean
d36113c tree (see regen_goldens.py for the exact recipe)."""
import asyncio
import importlib
import json
import os

import pytest

import config
import parity_env as pe
from voicecore import profiles
import realtime_bridge
from profile_helpers import SERVICE_DIR, build_payload_and_url

MV_DEFAULT_URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime-2"


@pytest.fixture
def clean_env(monkeypatch):
    for var in pe.CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _assert_no_profile_parity():
    raw, url = build_payload_and_url()
    assert raw == pe.load_golden("golden_mv_session_inbound.json")
    assert url == MV_DEFAULT_URL
    raw_out, _ = build_payload_and_url(outbound=True)
    assert raw_out == pe.load_golden("golden_mv_session_outbound.json")


# -- c15: no-profile independence from VOICE_CONFIG_DIR -----------------------

def test_missing_config_dir_ok_without_profile(clean_env, monkeypatch, tmp_path):
    """VOICE_AGENT unset + config dir missing/unreadable: imports fine, no exception,
    providers.yaml never required, bytes equal the goldens."""
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path / "definitely-missing"))
    importlib.reload(profiles)
    importlib.reload(config)
    importlib.reload(realtime_bridge)
    assert config.load() is not None  # no exception of any kind
    _assert_no_profile_parity()

    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    unreadable.chmod(0o000)
    try:
        monkeypatch.setenv("VOICE_CONFIG_DIR", str(unreadable))
        _assert_no_profile_parity()
    finally:
        unreadable.chmod(0o755)


def test_blank_voice_agent_is_no_profile(clean_env, monkeypatch, tmp_path):
    """Blank/whitespace VOICE_AGENT is UNSET — no file access, no profile, no error."""
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path / "nope"))
    for blank in ("", " ", "   ", "\t"):
        monkeypatch.setenv("VOICE_AGENT", blank)
        assert profiles.load_active_profile() is None
        _assert_no_profile_parity()


# -- c16: Config dataclass parity ---------------------------------------------

def test_config_parity_defaults(clean_env):
    assert pe.config_snapshot() == pe.load_golden_json("golden_mv_config_defaults.json")


def test_config_parity_full_env(clean_env, monkeypatch):
    for key, value in pe.FULL_ENV.items():
        monkeypatch.setenv(key, value)
    assert pe.config_snapshot() == pe.load_golden_json("golden_mv_config_fullenv.json")


# -- c18: session.update byte parity ------------------------------------------

def test_session_update_bytes_inbound(clean_env):
    assert pe.capture_session_raw(outbound=False) == \
        pe.load_golden("golden_mv_session_inbound.json")


def test_session_update_bytes_outbound(clean_env):
    raw = pe.capture_session_raw(outbound=True)
    assert raw == pe.load_golden("golden_mv_session_outbound.json")
    session = json.loads(raw)["session"]
    assert session["tools"] == [] and session["tool_choice"] == "none"


# -- c20: wss URL parity -------------------------------------------------------

def test_realtime_url_no_profile(clean_env, monkeypatch):
    assert realtime_bridge.realtime_url(config.load()) == MV_DEFAULT_URL
    monkeypatch.setenv("OPENAI_REALTIME_MODEL", "custom-model")
    assert realtime_bridge.realtime_url(config.load()) == \
        "wss://api.openai.com/v1/realtime?model=custom-model"


# -- c21: divergent per-bridge defaults preserved ------------------------------

def test_vad_divergence_preserved(clean_env):
    ours = json.loads(pe.load_golden("golden_mv_session_inbound.json"))
    td = ours["session"]["audio"]["input"]["turn_detection"]
    assert td == {"type": "server_vad", "threshold": 0.5,
                  "prefix_padding_ms": 300, "silence_duration_ms": 250}
    assert config.load().openai_model == "gpt-realtime-2"
    # Cross-check against Mode C's golden: the divergence is real, not normalized.
    mc_golden = SERVICE_DIR.parent / "voice" / "tests" / "fixtures" / \
        "golden_mc_session_inbound.json"
    mc_td = json.loads(mc_golden.read_text())["session"]["audio"]["input"]["turn_detection"]
    assert mc_td["silence_duration_ms"] == 500 and td["silence_duration_ms"] == 250


# -- c22: exactly one session.update construction site ------------------------

def test_single_session_update_builder(clean_env, monkeypatch, tmp_path):
    product = [p for p in SERVICE_DIR.glob("*.py")]
    assert product, "no product files found"
    count = sum(p.read_text().count('"type": "session.update"') for p in product)
    assert count == 1, "exactly ONE session.update construction site is allowed"

    # Call-site proof: run() must route through _send_session_update.
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("VOICE_EVENTLOG_PATH", str(tmp_path / "events.jsonl"))
    calls = []

    async def spy(self, ws):
        calls.append("builder")
        raise RuntimeError("stop right after the builder — proves the call site")

    class FakeConnect:
        async def __aenter__(self):
            return pe.RecordingWS()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(realtime_bridge.RealtimeBridge, "_send_session_update", spy)
    monkeypatch.setattr(realtime_bridge.websockets, "connect",
                        lambda *a, **kw: FakeConnect())

    from approval import ApprovalStore
    bridge = realtime_bridge.RealtimeBridge(
        config.load(), pe.BASE_PROMPT, ApprovalStore(),
        token_ctx={"token": "t", "caller": "c"})
    asyncio.run(bridge.run())
    assert calls == ["builder"], "run() did not route through _send_session_update"
