"""s15b-B b7: the Talk-bridge failure-code vocabulary, from the producer's source.

The dashboard consumer of these codes was ``POST /api/test-call`` with
``bridge=mode-v``. Ticket 09 deleted that surface (Place a call is phone-only).
The producer scan stays so a later Talk place-a-call cannot invent a second
vocabulary; the HTTP labelling tests went with the endpoint.
"""
import re
from pathlib import Path

SERVICES = Path(__file__).resolve().parent.parent.parent
SESSION_PY = SERVICES / "talk-voice-bridge" / "session.py"
SERVER_PY = SERVICES / "talk-voice-bridge" / "server.py"


def producer_failure_codes() -> set:
    """Every failure code the BRIDGE can put on the wire, read from its own source."""
    codes = set()
    for path in (SESSION_PY, SERVER_PY):
        codes |= set(re.findall(r'"code":\s*"([a-z_]+)"', path.read_text()))
    return codes


def test_b7_producer_vocabulary_is_discoverable():
    """Anti-vacuity: the source scan must actually find codes."""
    codes = producer_failure_codes()
    assert len(codes) >= 4, f"only found {codes} — the scan is not reading the producer"
    assert "busy" in codes and "setup_failed" in codes
