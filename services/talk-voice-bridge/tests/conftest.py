import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Ticket 07: capture is ON in production and OFF here by default — a unit suite must
# never write audio to the shared volume. The suites that exercise capture switch it on
# explicitly with an injected encoder and a tmp root.
os.environ.setdefault("VOICE_RECORDING_ENABLED", "false")
# VC24: the dialing endpoints are fail-closed - with no gateway token configured they
# refuse every caller. The suites that drive them authenticate with whatever token the
# imported server actually holds (``server._cfg.hermes_gateway_token``), as Hermes does.
os.environ.setdefault("HERMES_GATEWAY_TOKEN", "test-token")


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
