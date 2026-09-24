"""
Unit tests for the pure call-detection/trust predicates in voice_calls.py (Talk voice
plugin-side coordination: Nextcloud Talk <-> the audio sidecar in
``services/talk-voice-bridge``).

Covers call-active detection, trigger-mode/allowlist eligibility, owner/guest trust
classification from a room's participant list, human-hangup detection, and the
active-call transition tracker feeding the plugin's watch loop. No gateway, no
network: voice_calls.py imports only transport.py's pure constants/helpers at
module scope (httpx is used lazily inside VoiceCoordinator's async methods).

Run (from hermes/): PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests -q
"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins", "nextcloud_talk"))

import voice_calls as vc  # noqa: E402


ONE_TO_ONE = {"token": "r1", "type": 1, "hasCall": True}
QUIET_1V1  = {"token": "r1", "type": 1, "hasCall": False}
GROUP_CALL = {"token": "g1", "type": 2, "hasCall": True}


def test_call_active():
    assert vc.call_active(ONE_TO_ONE) is True
    assert vc.call_active(QUIET_1V1) is False
    assert vc.call_active({"token": "x", "type": 1, "callFlag": 3}) is True


def test_eligible_smart():
    assert vc.eligible(ONE_TO_ONE, mode="smart", allowlist=[]) is True
    assert vc.eligible(GROUP_CALL, mode="smart", allowlist=[]) is False
    assert vc.eligible(GROUP_CALL, mode="smart", allowlist=["g1"]) is True


def test_classify_trust_owner_vs_guest():
    parts = [{"actorType": "users", "actorId": "ai-agent", "inCall": 1},
             {"actorType": "users", "actorId": "Olivia", "inCall": 1, "displayName": "Olivia"}]
    trust, caller, disp = vc.classify_call_trust(parts, our_user="ai-agent", owner_set={"olivia"})
    assert (trust, caller, disp) == ("owner", "Olivia", "Olivia")
    parts2 = [{"actorType": "users", "actorId": "ai-agent", "inCall": 1},
              {"actorType": "users", "actorId": "alice", "inCall": 1}]
    trust2, caller2, _ = vc.classify_call_trust(parts2, our_user="ai-agent", owner_set={"olivia"})
    assert (trust2, caller2) == ("guest", "alice")


def test_call_has_other_human():
    both = [{"actorId": "ai-agent", "inCall": 1}, {"actorId": "Olivia", "inCall": 1}]
    gone = [{"actorId": "ai-agent", "inCall": 1}, {"actorId": "Olivia", "inCall": 0}]
    assert vc.call_has_other_human(both, our_user="ai-agent") is True
    assert vc.call_has_other_human(gone, our_user="ai-agent") is False


def test_transitions_start_then_stop():
    w = vc.TransitionTracker(mode="smart", allowlist=[])
    assert w.diff([QUIET_1V1]) == []
    assert w.diff([ONE_TO_ONE]) == [("start", "r1")]
    assert w.diff([ONE_TO_ONE]) == []
    assert w.diff([QUIET_1V1]) == [("stop", "r1")]


# --- tracker retry/reconcile API (forget/reset) ----------------------------

def test_tracker_forget_reemits_start():
    # 409-busy / failed-join retry contract: a still-active room the sidecar couldn't
    # take must re-emit as a fresh "start" on the next poll, not stay stranded as active.
    w = vc.TransitionTracker(mode="smart", allowlist=[])
    assert w.diff([ONE_TO_ONE]) == [("start", "r1")]
    assert w.diff([ONE_TO_ONE]) == []                 # steady state - no re-emit
    w.forget("r1")                                    # e.g. /call/start returned 409
    assert w.diff([ONE_TO_ONE]) == [("start", "r1")]  # re-emitted as a fresh start
    assert w.active == {"r1"}


def test_tracker_reset_clears_active():
    # Sidecar self-teardown reconcile: after reset, a still-active room re-emits as
    # "start" (the tracker forgot it), never a spurious "stop".
    w = vc.TransitionTracker(mode="smart", allowlist=[])
    assert w.diff([ONE_TO_ONE]) == [("start", "r1")]
    assert w.active == {"r1"}
    w.reset()
    assert w.active == set()
    assert w.diff([ONE_TO_ONE]) == [("start", "r1")]
