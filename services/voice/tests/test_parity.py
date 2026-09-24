"""s1 no-profile parity tests, Mode C (c15, c17, c19–c22): with VOICE_AGENT
unset/blank the bridge must be byte-identical to the pre-change d36113c behavior —
module constants, the session.update wire bytes, and the wss URL. Goldens were
captured from the clean d36113c tree (see regen_goldens.py for the exact recipe)."""
import json

import pytest
from fastapi.testclient import TestClient

import parity_env as pe
from voicecore import profiles
import server
from conftest import FakeOpenAIWS
from profile_helpers import SERVICE_DIR

MC_DEFAULT_URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime"


def _assert_no_profile_parity(srv):
    assert pe.capture_session_raw(srv, outbound=False) == \
        pe.load_golden("golden_mc_session_inbound.json")
    assert pe.capture_session_raw(srv, outbound=True) == \
        pe.load_golden("golden_mc_session_outbound.json")
    assert srv._realtime_url() == MC_DEFAULT_URL


# -- c15: no-profile independence from VOICE_CONFIG_DIR -----------------------

def test_missing_config_dir_ok_without_profile(tmp_path):
    """VOICE_AGENT unset + config dir missing/unreadable: module import succeeds,
    no exception, providers.yaml never required, bytes equal the goldens."""
    with pe.env_sandbox({"VOICE_CONFIG_DIR": str(tmp_path / "definitely-missing")}) as srv:
        _assert_no_profile_parity(srv)

    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    unreadable.chmod(0o000)
    try:
        with pe.env_sandbox({"VOICE_CONFIG_DIR": str(unreadable)}) as srv:
            _assert_no_profile_parity(srv)
    finally:
        unreadable.chmod(0o755)


def test_blank_voice_agent_is_no_profile(tmp_path):
    """Blank/whitespace VOICE_AGENT is UNSET — no file access, no profile, no error."""
    for blank in ("", " ", "   ", "\t"):
        with pe.env_sandbox({"VOICE_AGENT": blank,
                             "VOICE_CONFIG_DIR": str(tmp_path / "nope")}) as srv:
            assert profiles.load_active_profile() is None
            _assert_no_profile_parity(srv)


# -- c17: module-constant parity ----------------------------------------------

def test_config_parity_defaults():
    with pe.env_sandbox() as srv:
        assert pe.config_snapshot(srv) == \
            pe.load_golden_json("golden_mc_config_defaults.json")


def test_config_parity_full_env():
    with pe.env_sandbox(pe.FULL_ENV) as srv:
        assert pe.config_snapshot(srv) == \
            pe.load_golden_json("golden_mc_config_fullenv.json")


# -- c19: session.update byte parity ------------------------------------------

def test_session_update_bytes_inbound():
    with pe.env_sandbox() as srv:
        raw = pe.capture_session_raw(srv, outbound=False)
        assert raw == pe.load_golden("golden_mc_session_inbound.json")
        session = json.loads(raw)["session"]
        assert session["tools"] == srv.TOOLS and session["tool_choice"] == "auto"
        assert session["audio"]["input"]["format"] == {"type": "audio/pcmu"}
        assert session["audio"]["output"]["format"] == {"type": "audio/pcmu"}


def test_session_update_bytes_outbound():
    with pe.env_sandbox() as srv:
        raw = pe.capture_session_raw(srv, outbound=True)
        assert raw == pe.load_golden("golden_mc_session_outbound.json")
        session = json.loads(raw)["session"]
        assert session["tools"] == [] and session["tool_choice"] == "none"


# -- c20: wss URL parity -------------------------------------------------------

def test_realtime_url_no_profile():
    with pe.env_sandbox() as srv:
        assert srv._realtime_url() == MC_DEFAULT_URL
    with pe.env_sandbox({"OPENAI_REALTIME_MODEL": "custom-model"}) as srv:
        assert srv._realtime_url() == \
            "wss://api.openai.com/v1/realtime?model=custom-model"


# -- c21: divergent per-bridge defaults preserved ------------------------------

def test_vad_divergence_preserved():
    ours = json.loads(pe.load_golden("golden_mc_session_inbound.json"))
    td = ours["session"]["audio"]["input"]["turn_detection"]
    assert td == {"type": "server_vad", "threshold": 0.5,
                  "prefix_padding_ms": 300, "silence_duration_ms": 500}
    with pe.env_sandbox() as srv:
        assert srv.OPENAI_MODEL == "gpt-realtime"
    # Cross-check against Mode V's golden: the divergence is real, not normalized.
    mv_golden = SERVICE_DIR.parent / "talk-voice-bridge" / "tests" / "fixtures" / \
        "golden_mv_session_inbound.json"
    mv_td = json.loads(mv_golden.read_text())["session"]["audio"]["input"]["turn_detection"]
    assert mv_td["silence_duration_ms"] == 250 and td["silence_duration_ms"] == 500


# -- c22: exactly one session.update construction site ------------------------

def test_single_session_update_builder(monkeypatch):
    product = [p for p in SERVICE_DIR.glob("*.py")]
    assert product, "no product files found"
    count = sum(p.read_text().count('"type": "session.update"') for p in product)
    assert count == 1, "exactly ONE session.update construction site is allowed"

    # Call-site proof: media_stream must route through _send_session_update, and the
    # inbound tools must come from the same TOOLS object as at d36113c.
    assert json.loads(pe.load_golden("golden_mc_session_inbound.json"))["session"]["tools"] \
        == server.TOOLS

    calls = []

    async def spy(openai_ws, system_prompt, *, outbound=False):
        calls.append(outbound)

    fake = FakeOpenAIWS()
    monkeypatch.setattr(server, "_send_session_update", spy)
    monkeypatch.setattr(server.websockets, "connect", lambda *a, **kw: fake)
    monkeypatch.setattr(server, "OPENAI_API_KEY", "test-key")
    tok = server._mint_inbound_token()

    client = TestClient(server.app)
    with client.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "start", "sequenceNumber": "1", "streamSid": "MZparity",
                      "start": {"streamSid": "MZparity", "callSid": "CAx",
                                "customParameters": {"inbound_token": tok}}})
        ws.send_json({"event": "stop", "sequenceNumber": "2", "streamSid": "MZparity"})
    assert calls == [False], "media_stream did not route through _send_session_update"
