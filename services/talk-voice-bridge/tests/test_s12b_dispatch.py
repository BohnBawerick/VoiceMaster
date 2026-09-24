"""s12b c5/c7 — CallSession lane dispatch + slot parity.

A cascade profile launches a CascadeBridge, a realtime profile a RealtimeBridge — both
under the SAME single-slot lock + generation-guarded reconcile. Bridges are faked
(construction-only): this suite proves the DISPATCH, not the audio path (that is
test_s12b_cascade_bridge). c6's activation-parity lives in test_profiles.py.
"""
import asyncio
import types

import pytest

import session as session_mod
from approval import ApprovalStore
from config import load


def _cfg():
    return load()


class FakeBrowser:
    def __init__(self):
        self.started = []
        self.left = 0

    async def start_call(self, token):
        self.started.append(token)

    async def join_call(self, token):
        pass

    async def leave_call(self):
        self.left += 1


class _BridgeFake:
    def __init__(self, *a, **kw):
        self.args = a
        self.kwargs = kw
        self._ended = asyncio.Event()
        self.stops = 0

    async def run(self):
        await self._ended.wait()

    async def stop(self):
        self.stops += 1
        self._ended.set()


class CascadeFake(_BridgeFake):
    pass


class RealtimeFake(_BridgeFake):
    pass


class ShortCascade(_BridgeFake):
    async def run(self):           # ends on its own → reconcile frees the slot
        await asyncio.sleep(0.01)


class StubProfile:
    def __init__(self, pipeline):
        self._p = pipeline

    @property
    def pipeline(self):
        return self._p

    def retain_enabled(self, default):
        return default


def _mission():
    return types.SimpleNamespace(to="+61400000000", brief="say hi", target_display="Mobile")


def _patch(monkeypatch, pipeline, *, cascade_cls=CascadeFake):
    monkeypatch.setattr(session_mod, "RealtimeBridge", RealtimeFake)
    monkeypatch.setattr(session_mod.cascade_bridge, "CascadeBridge", cascade_cls)
    monkeypatch.setattr(session_mod.config, "overlay_profile", lambda cfg, p: cfg)
    monkeypatch.setattr(session_mod.outbound, "outbound_base_prompt", lambda *a, **k: "p")
    monkeypatch.setattr(session_mod.hermes, "build_system_prompt", lambda *a, **k: "p")
    monkeypatch.setattr(session_mod.profiles, "load_effective_profile",
                        lambda direction, outlet=None, env=None: StubProfile(pipeline))


@pytest.mark.asyncio
async def test_c5_cascade_profile_dispatches_cascade_bridge(monkeypatch):
    _patch(monkeypatch, "cascade")
    cs = session_mod.CallSession(_cfg(), FakeBrowser(), ApprovalStore())
    assert await cs.start("A", "guest", "+61", "X", mission=_mission()) is True
    assert isinstance(cs._bridge, CascadeFake)
    assert not isinstance(cs._bridge, RealtimeFake)
    await cs.stop("A")


@pytest.mark.asyncio
async def test_c5_realtime_profile_dispatches_realtime_bridge(monkeypatch):
    _patch(monkeypatch, "realtime")
    cs = session_mod.CallSession(_cfg(), FakeBrowser(), ApprovalStore())
    assert await cs.start("A", "guest", "+61", "X", mission=_mission()) is True
    assert isinstance(cs._bridge, RealtimeFake)
    await cs.stop("A")


@pytest.mark.asyncio
async def test_c5_cascade_rides_the_same_single_slot_lock(monkeypatch):
    """The cascade lane contends for the ONE slot exactly like realtime: a 2nd start is
    rejected while a cascade call is live, and the first cascade bridge is untouched."""
    _patch(monkeypatch, "cascade")
    cs = session_mod.CallSession(_cfg(), FakeBrowser(), ApprovalStore())
    assert await cs.start("A", "guest", "+61", "X", mission=_mission()) is True
    first = cs._bridge
    assert await cs.start("B", "guest", "+61", "Y", mission=_mission()) is False
    assert cs.busy and cs._bridge is first
    await cs.stop("A")
    assert not cs.busy


@pytest.mark.asyncio
async def test_c7_cascade_bridge_self_end_frees_slot(monkeypatch):
    """A cascade bridge whose run() returns on its own reconciles through the SAME
    done-callback path as realtime — the slot frees (busy False)."""
    _patch(monkeypatch, "cascade", cascade_cls=ShortCascade)
    cs = session_mod.CallSession(_cfg(), FakeBrowser(), ApprovalStore())
    assert await cs.start("A", "guest", "+61", "X", mission=_mission()) is True
    for _ in range(50):
        if not cs.busy:
            break
        await asyncio.sleep(0.01)
    assert not cs.busy
