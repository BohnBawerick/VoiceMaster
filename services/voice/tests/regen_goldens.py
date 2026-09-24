"""Regenerate the Mode C golden fixtures for the s1 voice-profile parity tests.

CONTRACT (pinned rule 6): goldens MUST be captured from the clean ``d36113c`` product
code. Exact regeneration recipe, from the repo root:

    git checkout d36113c -- services/voice/server.py
    cd services/voice && .venv/bin/python tests/regen_goldens.py
    git checkout HEAD  -- services/voice/server.py

Goldens produced from post-change code are VOID — the refactored code must reproduce
the pre-change bytes, not its own.
"""
import json
import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS.parent))   # service dir: server, outbound, ...
sys.path.insert(0, str(_TESTS))          # tests dir: parity_env

import parity_env as pe  # noqa: E402


def main() -> None:
    pe.FIXTURES.mkdir(exist_ok=True)

    # -- session.update byte goldens + defaults config golden (empty env) ----
    with pe.env_sandbox() as srv:
        (pe.FIXTURES / "golden_mc_session_inbound.json").write_text(
            pe.capture_session_raw(srv, outbound=False))
        (pe.FIXTURES / "golden_mc_session_outbound.json").write_text(
            pe.capture_session_raw(srv, outbound=True))
        (pe.FIXTURES / "golden_mc_config_defaults.json").write_text(
            json.dumps(pe.config_snapshot(srv), indent=2, sort_keys=True) + "\n")

    # -- full-env config golden: every var set to a non-default value --------
    with pe.env_sandbox(pe.FULL_ENV) as srv:
        (pe.FIXTURES / "golden_mc_config_fullenv.json").write_text(
            json.dumps(pe.config_snapshot(srv), indent=2, sort_keys=True) + "\n")

    print("Mode C goldens written to", pe.FIXTURES)


if __name__ == "__main__":
    main()
