"""s14b: `session.start()` must say WHY it returned False.

Session 1 lost several minutes to a phantom busy state. The bridge answered a Talk
cascade fire with a bare `{"placed": false, "token": "room8tok"}` and HTTP 409;
voice-control turned every 409 into "mode-v is BUSY — a Talk call is already in
progress". It was not busy — `overlay_profile` had raised KeyError. The label actively
misdirected the diagnosis while the real traceback sat in the container log.

`start()` has FOUR distinct False paths (slot busy / join failed / superseded mid-join /
bridge setup failed) and they were indistinguishable to every caller. These pin that
each records a machine-readable code the API can propagate, and that a real busy is
still reported as busy.
"""
import asyncio

import pytest

from voicecore import profiles
import session as session_mod


class _Browser:
    """A TalkBrowser stand-in; `boom` makes the join/start leg fail."""

    def __init__(self, boom=False):
        self.boom = boom
        self.left = False

    async def start_call(self, token):
        if self.boom:
            raise RuntimeError("browser exploded")

    join_call = start_call

    async def leave_call(self):
        self.left = True


def _session(monkeypatch, *, browser=None, profile_exc=None):
    from approval import ApprovalStore
    import config

    sess = session_mod.CallSession(config.load_base(), browser or _Browser(),
                                   ApprovalStore())
    if profile_exc is not None:
        monkeypatch.setattr(profiles, "load_effective_profile",
                            lambda direction, outlet=None, env=None: (_ for _ in ()).throw(profile_exc))
    return sess


def _mission():
    from outbound import OutboundMission
    return OutboundMission(brief="b")


def test_success_clears_any_previous_failure(monkeypatch):
    sess = _session(monkeypatch)
    sess._last_start_failure = {"code": "stale", "detail": "from an earlier call"}

    async def drive():
        ok = await sess.start("tok", "outbound", "", "", mission=_mission())
        failure = sess.last_start_failure
        await sess.stop("tok")
        return ok, failure

    ok, failure = asyncio.run(drive())
    assert ok is True
    assert failure is None


def test_bridge_setup_failure_is_not_reported_as_busy(monkeypatch):
    """THE s14b BUG: overlay/profile explosion during setup. Must NOT read as busy."""
    sess = _session(monkeypatch, profile_exc=KeyError("realtime"))
    ok = asyncio.run(sess.start("tok", "outbound", "", "", mission=_mission()))
    assert ok is False
    failure = sess.last_start_failure
    assert failure["code"] == "setup_failed"
    assert failure["code"] != "busy"
    assert "realtime" in failure["detail"]


def test_join_failure_is_distinguishable(monkeypatch):
    sess = _session(monkeypatch, browser=_Browser(boom=True))
    ok = asyncio.run(sess.start("tok", "outbound", "", "", mission=_mission()))
    assert ok is False
    assert sess.last_start_failure["code"] == "join_failed"
    assert "browser exploded" in sess.last_start_failure["detail"]


def test_a_real_busy_still_reports_busy(monkeypatch):
    """The regression guard on the fix: do not cure the false busy by losing the true one."""
    sess = _session(monkeypatch)

    async def drive():
        assert await sess.start("first", "outbound", "", "", mission=_mission()) is True
        second = await sess.start("second", "outbound", "", "", mission=_mission())
        await sess.stop("first")
        return second

    assert asyncio.run(drive()) is False
    assert sess.last_start_failure["code"] == "busy"
    assert "first" in sess.last_start_failure["detail"]
