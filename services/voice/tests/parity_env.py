"""Shared parity-capture harness for the Mode C golden fixtures (s1 voice profiles).

Single definition of the capture environment: every env var server.py reads at import,
the fixed base prompt, the deterministic full-env matrix, an env sandbox that reloads
the module under a controlled environment (Mode C config is module-level constants, so
changing env REQUIRES a reload), and the fake-WS recorder capturing the exact bytes
``_send_session_update`` puts on the wire. Both ``regen_goldens.py`` (golden capture,
run against the clean d36113c product code) and ``test_parity.py`` import it, so the
capture and the check can never drift apart.

Golden policy (s1 contract, pinned rule 6): goldens MUST be captured from the clean
d36113c tree — see regen_goldens.py for the exact recipe.
"""
import asyncio
import contextlib
import importlib
import json
import os
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Fixed, deterministic base prompt (build_system_prompt() embeds datetime.now, so the
# harness passes its own prompt — exactly what the c19 verify command prescribes).
BASE_PROMPT = "You are Robot on a live call. (s1 parity fixture base prompt)"

# Every env var server.py reads at module level, plus the s1 profile-activation vars.
CONFIG_ENV_VARS = [
    "OPENAI_API_KEY",
    "OPENAI_VOICE",
    "OPENAI_REALTIME_MODEL",
    "VOICE_TRANSCRIPTION_MODEL",
    "VOICE_FILLER_TEXT",
    "VOICE_FILLER_DEBOUNCE_MS",
    "HINDSIGHT_URL",
    "HINDSIGHT_BANK",
    "VOICE_RETAIN_ENABLED",
    "HERMES_CONFIG_DIR",
    "PORT",
    "HERMES_GATEWAY_URL",
    "HERMES_GATEWAY_TOKEN",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_SIGNING_TOKENS",
    "VOICE_INBOUND_ALLOWED_CALLERS",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_FROM_NUMBER",
    "VOICE_OUTBOUND_ALLOWED_NUMBERS",
    "VOICE_OUTBOUND_AI_DISCLOSURE",
    "VOICE_PUBLIC_HOST",
    # s1 profile activation (must be absent on the no-profile parity path)
    "VOICE_AGENT",
    "VOICE_CONFIG_DIR",
]

# c17 full-env matrix: every config env var set to a NON-default, obviously-fake value.
FULL_ENV = {
    "OPENAI_API_KEY": "test-openai-key-not-real",
    "OPENAI_VOICE": "echo",
    "OPENAI_REALTIME_MODEL": "gpt-realtime-parity",
    "VOICE_TRANSCRIPTION_MODEL": "whisper-1",
    "VOICE_FILLER_TEXT": "Hold on a moment.",
    "VOICE_FILLER_DEBOUNCE_MS": "2500",
    "HINDSIGHT_URL": "http://hindsight.parity.test:9999",
    "HINDSIGHT_BANK": "parity-bank",
    "VOICE_RETAIN_ENABLED": "false",
    "HERMES_CONFIG_DIR": "/tmp/parity-config",
    "PORT": "4444",
    "HERMES_GATEWAY_URL": "http://gateway.parity.test:1234",
    "HERMES_GATEWAY_TOKEN": "fake-gw-tok",
    "TWILIO_AUTH_TOKEN": "fake-twilio-auth",
    "TWILIO_SIGNING_TOKENS": "fake-sign-a,fake-sign-b",
    "VOICE_INBOUND_ALLOWED_CALLERS": "+15550001111,+15550002222",
    "TWILIO_ACCOUNT_SID": "ACfake000",
    "TWILIO_FROM_NUMBER": "+15550009999",
    "VOICE_OUTBOUND_ALLOWED_NUMBERS": "+15550003333",
    "VOICE_OUTBOUND_AI_DISCLOSURE": "true",
    "VOICE_PUBLIC_HOST": "parity.test",
}

# The module constants the s1 refactor touches (the c17 parity surface).
CONFIG_CONSTANTS = [
    "VOICE", "OPENAI_MODEL", "TRANSCRIPTION_MODEL", "FILLER_TEXT", "FILLER_DEBOUNCE_S",
    "HINDSIGHT_URL", "HINDSIGHT_BANK", "RETAIN_ENABLED", "CONFIG_DIR", "PORT",
    "HERMES_GATEWAY_URL", "HERMES_GATEWAY_TOKEN", "TWILIO_AUTH_TOKEN",
    "TWILIO_SIGNING_TOKENS", "ALLOWED_CALLERS", "TWILIO_ACCOUNT_SID",
    "TWILIO_FROM_NUMBER", "ALLOWED_OUTBOUND", "AI_DISCLOSURE", "PUBLIC_HOST",
]


class RecordingWS:
    """Records the raw JSON strings passed to send() — the exact wire bytes."""

    def __init__(self):
        self.raw = []

    async def send(self, raw):
        self.raw.append(raw)

    async def close(self):
        pass  # teardown compatibility for tests that drive the stream


def reload_server():
    import server

    return importlib.reload(server)


@contextlib.contextmanager
def env_sandbox(overrides: dict | None = None):
    """Reload server.py under a controlled env; restore env AND module state after.

    Scrubs every CONFIG_ENV_VARS entry, applies ``overrides``, reloads, yields the
    reloaded module. On exit the original environment is restored and the module is
    reloaded again so later tests see the same server module state as before.
    """
    saved = {k: os.environ.get(k) for k in CONFIG_ENV_VARS}
    for k in CONFIG_ENV_VARS:
        os.environ.pop(k, None)
    if overrides:
        os.environ.update(overrides)
    try:
        yield reload_server()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reload_server()


def capture_session_raw(server_mod, *, outbound: bool) -> str:
    """Build one session.update through _send_session_update (current code)."""
    ws = RecordingWS()
    asyncio.run(server_mod._send_session_update(ws, BASE_PROMPT, outbound=outbound))
    assert len(ws.raw) == 1, f"expected exactly one send, got {len(ws.raw)}"
    return ws.raw[0]


def config_snapshot(server_mod) -> dict:
    """JSON-normalized snapshot of the c17 constant surface (Path→str, sets→sorted lists)."""
    out = {}
    for name in CONFIG_CONSTANTS:
        v = getattr(server_mod, name)
        if isinstance(v, Path):
            v = str(v)
        elif isinstance(v, frozenset):
            v = sorted(v)
        elif isinstance(v, tuple):
            v = list(v)
        out[name] = v
    return out


def load_golden(name: str) -> str:
    return (FIXTURES / name).read_text()


def load_golden_json(name: str):
    return json.loads(load_golden(name))
