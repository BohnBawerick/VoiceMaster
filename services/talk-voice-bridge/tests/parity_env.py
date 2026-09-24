"""Shared parity-capture harness for the Mode V golden fixtures (s1 voice profiles).

This module is the SINGLE definition of the capture environment: the env vars that
config.load() reads, the fixed base prompt, the deterministic full-env matrix, and the
fake-WS recorder that captures the exact bytes ``RealtimeBridge._send_session_update``
puts on the wire. Both ``regen_goldens.py`` (golden capture, run against the clean
d36113c product code) and ``test_parity.py`` (byte-parity assertions against the
current code) import it, so capture and check can never drift apart.

Golden policy (s1 contract, pinned rule 6): goldens MUST be captured from the clean
d36113c tree — see regen_goldens.py for the exact recipe. Goldens regenerated from
post-change code are void.
"""
import asyncio
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Fixed, deterministic base prompt (the real one embeds SOUL.md; parity only needs
# a known byte sequence to prove pass-through and persona composition).
BASE_PROMPT = "You are Robot on a live call. (s1 parity fixture base prompt)"

# Every env var config.load() reads, plus the s1 profile-activation vars. Scrubbed
# before every capture/assertion so host env can't leak into goldens.
CONFIG_ENV_VARS = [
    "NEXTCLOUD_BASE_URL",
    "NEXTCLOUD_TALK_USER",
    "NEXTCLOUD_VOICE_APP_PASSWORD",
    "NEXTCLOUD_VOICE_LOGIN_PASSWORD",
    "OPENAI_API_KEY",
    "OPENAI_VOICE",
    "OPENAI_REALTIME_MODEL",
    "HERMES_GATEWAY_URL",
    "HERMES_GATEWAY_TOKEN",
    "HERMES_TIMEOUT",
    "HERMES_CONFIG_DIR",
    "VOICE_TRANSCRIPTION_MODEL",
    "VOICE_FILLER_DEBOUNCE_MS",
    "VOICE_FILLER_TEXT",
    "HINDSIGHT_URL",
    "HINDSIGHT_BANK",
    "VOICE_RETAIN_ENABLED",
    "AUDIO_RATE",
    "TALK_VOICE_VAD_SILENCE_MS",
    "TALK_VOICE_IDLE_TIMEOUT",
    "TALK_VOICE_APPROVAL_TIMEOUT",
    "PORT",
    "NEXTCLOUD_TALK_HOME_CONVERSATION",
    "TELEGRAM_BOT_TOKEN",
    # s1 profile activation (must be absent on the no-profile parity path)
    "VOICE_AGENT",
    "VOICE_CONFIG_DIR",
]

# c16 full-env matrix: every config env var set to a NON-default, obviously-fake value
# (no real secrets - fixtures must never hold a real credential or key-shaped string).
FULL_ENV = {
    "NEXTCLOUD_BASE_URL": "https://nc.parity.test/",
    "NEXTCLOUD_TALK_USER": "parity-bot",
    "NEXTCLOUD_VOICE_APP_PASSWORD": "fake-app-pw",
    "NEXTCLOUD_VOICE_LOGIN_PASSWORD": "fake-login-pw",
    "OPENAI_API_KEY": "test-openai-key-not-real",
    "OPENAI_VOICE": "echo",
    "OPENAI_REALTIME_MODEL": "gpt-realtime-parity",
    "HERMES_GATEWAY_URL": "http://gateway.parity.test:1234",
    "HERMES_GATEWAY_TOKEN": "fake-gw-tok",
    "HERMES_TIMEOUT": "77",
    "HERMES_CONFIG_DIR": "/tmp/parity-config",
    "VOICE_TRANSCRIPTION_MODEL": "whisper-1",
    "VOICE_FILLER_DEBOUNCE_MS": "2500",
    "VOICE_FILLER_TEXT": "Hold on a moment.",
    "HINDSIGHT_URL": "http://hindsight.parity.test:9999",
    "HINDSIGHT_BANK": "parity-bank",
    "VOICE_RETAIN_ENABLED": "false",
    "AUDIO_RATE": "16000",
    "TALK_VOICE_VAD_SILENCE_MS": "310",
    "TALK_VOICE_IDLE_TIMEOUT": "111",
    "TALK_VOICE_APPROVAL_TIMEOUT": "33",
    "PORT": "4444",
    "NEXTCLOUD_TALK_HOME_CONVERSATION": "parityroomtok",
    "TELEGRAM_BOT_TOKEN": "fake-tg-tok",
}


class RecordingWS:
    """Records the raw JSON strings passed to send() — the exact wire bytes."""

    def __init__(self):
        self.raw = []

    async def send(self, raw):
        self.raw.append(raw)

    async def close(self):
        pass  # teardown compatibility for tests that drive run()


def capture_session_raw(*, outbound: bool) -> str:
    """Build one session.update through RealtimeBridge._send_session_update (current code).

    Returns the raw string the bridge would send — json.dumps with default separators,
    no sort_keys (whatever the product code does; we never re-serialize it).
    """
    import config
    import outbound as outbound_mod
    import realtime_bridge
    from approval import ApprovalStore

    cfg = config.load()
    mission = outbound_mod.OutboundMission(brief="parity mission") if outbound else None
    bridge = realtime_bridge.RealtimeBridge(
        cfg, BASE_PROMPT, ApprovalStore(),
        token_ctx={"token": "parity-token", "caller": "parity"}, mission=mission)
    ws = RecordingWS()
    asyncio.run(bridge._send_session_update(ws))
    assert len(ws.raw) == 1, f"expected exactly one send, got {len(ws.raw)}"
    return ws.raw[0]


def config_snapshot() -> dict:
    """dataclasses.asdict of config.load(), JSON-normalized (all values already scalar)."""
    import dataclasses

    import config

    return dataclasses.asdict(config.load())


def load_golden(name: str) -> str:
    return (FIXTURES / name).read_text()


def load_golden_json(name: str):
    return json.loads(load_golden(name))
