"""Regenerate the Mode V golden fixtures for the s1 voice-profile parity tests.

CONTRACT (pinned rule 6): goldens MUST be captured from the clean ``d36113c`` product
code. Exact regeneration recipe, from the repo root:

    git checkout d36113c -- services/talk-voice-bridge/config.py \
                            services/talk-voice-bridge/realtime_bridge.py
    cd services/talk-voice-bridge && .venv/bin/python tests/regen_goldens.py
    git checkout HEAD  -- services/talk-voice-bridge/config.py \
                          services/talk-voice-bridge/realtime_bridge.py

Goldens produced from post-change code are VOID — the whole point is that the refactored
code must reproduce the pre-change bytes, not its own.
"""
import json
import os
import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS.parent))   # service dir: config, realtime_bridge, ...
sys.path.insert(0, str(_TESTS))          # tests dir: parity_env

import parity_env as pe  # noqa: E402


def main() -> None:
    pe.FIXTURES.mkdir(exist_ok=True)

    # -- session.update byte goldens (no profile, scrubbed env) --------------
    for var in pe.CONFIG_ENV_VARS:
        os.environ.pop(var, None)
    (pe.FIXTURES / "golden_mv_session_inbound.json").write_text(
        pe.capture_session_raw(outbound=False))
    (pe.FIXTURES / "golden_mv_session_outbound.json").write_text(
        pe.capture_session_raw(outbound=True))

    # -- Config parity goldens: (a) empty env, (b) every var non-default -----
    (pe.FIXTURES / "golden_mv_config_defaults.json").write_text(
        json.dumps(pe.config_snapshot(), indent=2, sort_keys=True) + "\n")
    os.environ.update(pe.FULL_ENV)
    (pe.FIXTURES / "golden_mv_config_fullenv.json").write_text(
        json.dumps(pe.config_snapshot(), indent=2, sort_keys=True) + "\n")

    print("Mode V goldens written to", pe.FIXTURES)


if __name__ == "__main__":
    main()
