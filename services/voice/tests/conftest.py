"""Test bootstrap for the Mode C voice service.

Mirrors services/talk-voice-bridge/tests/conftest.py (flat-module imports), plus the env the
outbound endpoint reads at import time — server.py resolves ALLOWED_OUTBOUND / HERMES_GATEWAY_TOKEN /
Twilio creds as module-level constants, so they must be set BEFORE ``import server``.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Deterministic config for the endpoint/guardrail tests (read by server.py at import).
os.environ.setdefault("HERMES_GATEWAY_TOKEN", "test-token")
os.environ.setdefault("VOICE_OUTBOUND_ALLOWED_NUMBERS", "+61491570156")
os.environ.setdefault("VOICE_INBOUND_ALLOWED_CALLERS", "+61491570156")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "twiliotest")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+61855501234")
os.environ.setdefault("VOICE_PUBLIC_HOST", "voiceh.test")
# Ticket 07: capture is ON in production and OFF here by default — a unit suite must
# never write audio to the shared volume. The suites that exercise capture switch it
# on explicitly with an injected encoder and a tmp root.
os.environ.setdefault("VOICE_RECORDING_ENABLED", "false")


@pytest.fixture(autouse=True)
def _isolate_lkg_store(monkeypatch, tmp_path):
    """s8 (LKG): a fresh last-known-good store dir per test.

    Calls that complete during a test record their snapshot into THIS dir, so no test
    can seed another test's fallback (a refusal test would otherwise answer with a
    snapshot recorded by an earlier test), and the shared events dir is never touched
    on a dev machine. Tests that assert on the store resolve it through
    ``voicecore.lkg.snapshot_path``, which reads the same env.
    """
    monkeypatch.setenv("VOICE_LKG_DIR", str(tmp_path / "lkg-store"))
    # The SQLite call archive (voicecore.call_store) gets the same treatment: a test whose
    # call reaches teardown with no Hindsight URL archives into THIS file, never the
    # shared events dir, and no test reads another test's calls.
    monkeypatch.setenv("VOICE_ARCHIVE_PATH", str(tmp_path / "calls.sqlite3"))
    monkeypatch.delenv("VOICE_ARCHIVE", raising=False)


class FakeOpenAIWS:
    """Stands in for the OpenAI Realtime socket inside media_stream.

    Iteration blocks until close() — like the real socket idling — but gives up after
    `patience` seconds so a teardown regression FAILS the test instead of hanging it.
    """
    def __init__(self, patience=5.0):
        self.sent = []
        self.patience = patience
        self.timed_out = False
        self._closed = asyncio.Event()
        self.state = type("S", (), {"name": "OPEN"})()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        self.state = type("S", (), {"name": "CLOSED"})()
        self._closed.set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            await asyncio.wait_for(self._closed.wait(), self.patience)
        except asyncio.TimeoutError:
            self.timed_out = True
        raise StopAsyncIteration

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False
