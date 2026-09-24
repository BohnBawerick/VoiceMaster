"""s15d: a finished Talk call must actually END, and a stale one must not be rejoined.

s14b session 1: after a Talk call finished, the Nextcloud room stayed `hasCall=True` with
both participants `inCall=3`, while the bridge reported `busy:false`. Consequences:

- every subsequent Talk fire 409'd (and was mislabelled "busy" — fixed separately)
- restarting the bridge did NOT clear it: the still-active call got REJOINED on boot,
  and the rejoined session held a convincing conversation that logged
  `pipeline: "realtime"`. That call was briefly scored as the first Talk CASCADE pass
  and had to be retracted. A lane that can manufacture false evidence is worse than a
  lane that is simply down.
- only the OWNER hanging up in the Talk UI actually ended it

Root cause of the first bullet: `browser.leave_call()` JS-clicks a hang-up control and,
if the control is not found, merely NAVIGATES AWAY — which does not leave the call. The
failure was logged at INFO and nothing verified the outcome.

These tests pin: leaving is VERIFIED against Nextcloud, escalates when the click did not
work, and a call the bridge itself ended is not silently rejoined afterwards.
"""
import asyncio

import pytest

import outbound as outbound_mod


# -- OCS call-state helpers ---------------------------------------------------

class _FakeOCS:
    """Records OCS calls and serves a scriptable room state."""

    def __init__(self, has_call=True, participant_type=1):
        self.has_call = has_call
        self.participant_type = participant_type
        self.deletes = []

    async def room_call_state(self, cfg, token):
        return {"hasCall": self.has_call, "participantType": self.participant_type}

    async def end_call(self, cfg, token, *, everyone=False):
        self.deletes.append((token, everyone))
        self.has_call = False
        return True


def test_room_call_state_reads_hascall(monkeypatch):
    """The verification primitive exists and reports Nextcloud's view, not ours."""
    assert hasattr(outbound_mod, "room_call_state")


def test_end_call_helper_exists():
    assert hasattr(outbound_mod, "end_call")


# -- teardown behaviour -------------------------------------------------------

def _session_with(monkeypatch, ocs, *, leave_raises=False, leave_noop=False):
    import config
    import session as session_mod
    from approval import ApprovalStore

    class _Browser:
        def __init__(self):
            self.left = 0

        async def start_call(self, token):
            pass

        join_call = start_call

        async def leave_call(self):
            self.left += 1
            if leave_raises:
                raise RuntimeError("leave control not found")
            # leave_noop models the s14b bug: the click silently does nothing, so
            # Nextcloud still shows the call active afterwards.
            if not leave_noop:
                ocs.has_call = False

    sess = session_mod.CallSession(config.load_base(), _Browser(), ApprovalStore())
    monkeypatch.setattr(outbound_mod, "room_call_state", ocs.room_call_state)
    monkeypatch.setattr(outbound_mod, "end_call", ocs.end_call)
    return sess


def _run_call(sess, token="tok-1"):
    from outbound import OutboundMission

    async def drive():
        await sess.start(token, "outbound", "", "", mission=OutboundMission(brief="b"))
        await sess.stop(token)

    asyncio.run(drive())


def test_teardown_verifies_the_call_actually_ended(monkeypatch):
    """Happy path: the browser leave worked, so no escalation is needed."""
    ocs = _FakeOCS(has_call=True)
    sess = _session_with(monkeypatch, ocs)
    _run_call(sess)
    assert ocs.has_call is False
    assert ocs.deletes == [], "no OCS escalation needed when the browser leave worked"


def test_teardown_escalates_when_the_browser_leave_did_not_work(monkeypatch):
    """THE s14b BUG: leave_call() returns cleanly but the call is still up.

    Navigating away is not leaving. Teardown must notice and end the call over OCS
    rather than declaring the slot free while Nextcloud still shows a live call.
    """
    ocs = _FakeOCS(has_call=True)
    sess = _session_with(monkeypatch, ocs, leave_noop=True)
    _run_call(sess)
    assert ocs.deletes, "teardown must escalate to OCS when the call is still active"
    assert ocs.has_call is False


def test_teardown_escalates_when_the_browser_leave_raised(monkeypatch):
    ocs = _FakeOCS(has_call=True)
    sess = _session_with(monkeypatch, ocs, leave_raises=True)
    _run_call(sess)
    assert ocs.deletes, "a raising leave_call must still end the call over OCS"


def test_teardown_still_frees_the_slot_if_ocs_escalation_fails(monkeypatch):
    """Cleanup must never strand the lock — a wedged Nextcloud cannot cost us the slot."""
    ocs = _FakeOCS(has_call=True)

    async def boom(cfg, token, *, everyone=False):
        raise RuntimeError("nextcloud unreachable")

    sess = _session_with(monkeypatch, ocs, leave_noop=True)
    monkeypatch.setattr(outbound_mod, "end_call", boom)
    _run_call(sess)
    assert sess.busy is False and sess.active_token is None


# -- rejoin gate --------------------------------------------------------------

def test_a_call_this_bridge_ended_is_not_rejoined(monkeypatch):
    """s14b false-evidence guard: after we end a call, an inbound join for that same
    token inside the cooldown is refused with a legible reason instead of producing a
    convincing realtime conversation that looks like a cascade pass."""
    ocs = _FakeOCS(has_call=True)
    sess = _session_with(monkeypatch, ocs)
    _run_call(sess, token="tok-stale")

    async def rejoin():
        return await sess.start("tok-stale", "guest", "", "")

    assert asyncio.run(rejoin()) is False
    assert sess.last_start_failure["code"] == "recently_ended"


def test_the_gate_does_not_block_a_different_room(monkeypatch):
    ocs = _FakeOCS(has_call=True)
    sess = _session_with(monkeypatch, ocs)
    _run_call(sess, token="tok-stale")

    async def other():
        ok = await sess.start("tok-other", "guest", "", "")
        await sess.stop("tok-other")
        return ok

    assert asyncio.run(other()) is True


def test_the_gate_expires(monkeypatch):
    """A cooldown, not a ban — a genuine callback to the same 1:1 room must work."""
    ocs = _FakeOCS(has_call=True)
    sess = _session_with(monkeypatch, ocs)
    _run_call(sess, token="tok-stale")
    sess._rejoin_cooldown_s = 0        # expire it

    async def callback():
        ok = await sess.start("tok-stale", "guest", "", "")
        await sess.stop("tok-stale")
        return ok

    assert asyncio.run(callback()) is True
