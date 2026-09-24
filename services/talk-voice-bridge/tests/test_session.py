"""Concurrency/orchestration tests for CallSession — the single-call lock, the mid-join
stop() race (no orphan bridge), and generation-bound reconcile (a stale reconcile from a
finished call must not tear down a newer call).

Uses lightweight fakes for TalkBrowser and RealtimeBridge — no real Playwright/OpenAI.
"""
import asyncio

import pytest

import session as session_mod
from approval import ApprovalStore
from config import load


def _cfg():
    return load()


class FakeBrowser:
    """Records join/leave and can optionally hold join_call open (gate) or fail it."""

    def __init__(self, *, fail_join=False):
        self.fail_join = fail_join
        self.join_calls = []
        self.leave_calls = 0
        self.join_gate = None       # asyncio.Event: if set, join_call blocks until fired
        self.join_started = None    # asyncio.Event: fired the moment join_call begins

    async def join_call(self, token):
        self.join_calls.append(token)
        if self.join_started is not None:
            self.join_started.set()
        if self.join_gate is not None:
            await self.join_gate.wait()
        if self.fail_join:
            raise RuntimeError("join boom")

    async def leave_call(self):
        self.leave_calls += 1


class RunForeverBridge:
    """bridge.run() blocks until stop() — simulates a live call."""

    def __init__(self, *a, **kw):
        self.stop_calls = 0
        self._ended = asyncio.Event()

    async def run(self):
        await self._ended.wait()

    async def stop(self):
        self.stop_calls += 1
        self._ended.set()


class ShortBridge:
    """bridge.run() returns on its own — simulates idle-timeout self-teardown."""

    def __init__(self, *a, **kw):
        self.stop_calls = 0

    async def run(self):
        await asyncio.sleep(0.02)

    async def stop(self):
        self.stop_calls += 1


def _live_bridge_tasks():
    return [t for t in asyncio.all_tasks()
            if (t.get_name() or "").startswith("mode-v-bridge-") and not t.done()]


@pytest.mark.asyncio
async def test_mid_join_stop_leaves_no_orphan_bridge(monkeypatch):
    """A stop() arriving while start() is still inside join_call() must fully tear the call
    down and launch NO bridge — the core orphan-bridge invariant."""
    monkeypatch.setattr(session_mod, "RealtimeBridge", RunForeverBridge)
    browser = FakeBrowser()
    browser.join_gate = asyncio.Event()
    browser.join_started = asyncio.Event()
    cs = session_mod.CallSession(_cfg(), browser, ApprovalStore())

    start_task = asyncio.create_task(cs.start("tokX", "guest", "+61", "Alice"))
    await browser.join_started.wait()          # start() is now suspended inside join_call
    assert cs.busy is True and cs.active_token == "tokX"
    assert _live_bridge_tasks() == []          # nothing launched yet

    stop_task = asyncio.create_task(cs.stop("tokX"))
    await asyncio.sleep(0)                       # let teardown run + bump the generation
    browser.join_gate.set()                     # join finally completes

    result = await start_task                    # start() resumes and re-checks generation
    await stop_task
    await asyncio.sleep(0.02)                     # let any scheduled reconcile settle

    assert result is False                       # superseded start returns False
    assert _live_bridge_tasks() == []            # NO orphan bridge
    assert cs.busy is False and cs.active_token is None
    assert not cs._lock.locked()                 # slot free

    # The slot is reusable after the race.
    browser.join_gate = None
    assert await cs.start("tokY", "owner", "+61", "Owner") is True
    await cs.stop("tokY")
    assert cs.busy is False


@pytest.mark.asyncio
async def test_stale_reconcile_does_not_tear_down_new_call(monkeypatch):
    """A late reconcile from finished call A (carrying A's bound generation) must no-op
    once call B owns the slot — B keeps its lock, token and bridge."""
    monkeypatch.setattr(session_mod, "RealtimeBridge", RunForeverBridge)
    browser = FakeBrowser()
    cs = session_mod.CallSession(_cfg(), browser, ApprovalStore())

    # Call A owns the slot; a_gen is the generation bound to A's bridge-done callback.
    assert await cs.start("A", "owner", "+61", "Owner") is True
    a_gen = cs._generation
    await cs.stop("A")                            # A torn down cleanly
    assert cs.busy is False

    # Call B claims the slot — generation moves past A's.
    assert await cs.start("B", "guest", "+61", "Guest") is True
    b_bridge = cs._bridge
    assert cs.active_token == "B" and cs._generation != a_gen

    # A's LATE/stale reconcile fires now (exactly what _on_bridge_done schedules for A).
    await cs._teardown(expected_gen=a_gen)

    # B is completely untouched.
    assert cs.busy is True and cs.active_token == "B"
    assert cs._bridge is b_bridge
    assert b_bridge.stop_calls == 0              # B's bridge NOT stopped
    assert cs._lock.locked()                     # B still owns the lock

    await cs.stop("B")
    await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_busy_rejects_second_start(monkeypatch):
    """A second start() while a call is live returns False and does not disturb it."""
    monkeypatch.setattr(session_mod, "RealtimeBridge", RunForeverBridge)
    cs = session_mod.CallSession(_cfg(), FakeBrowser(), ApprovalStore())

    assert await cs.start("A", "guest", "+61", "X") is True
    assert await cs.start("B", "guest", "+61", "Y") is False
    assert cs.active_token == "A"                # live call undisturbed

    await cs.stop("A")
    assert cs.busy is False


@pytest.mark.asyncio
async def test_join_failure_frees_lock_and_leaves_call(monkeypatch):
    """A failing join_call() leaves any half-joined call, frees the lock, and a later
    start() succeeds."""
    monkeypatch.setattr(session_mod, "RealtimeBridge", RunForeverBridge)
    browser = FakeBrowser(fail_join=True)
    cs = session_mod.CallSession(_cfg(), browser, ApprovalStore())

    assert await cs.start("F", "guest", "+61", "C") is False
    assert cs.busy is False and not cs._lock.locked()
    assert browser.leave_calls == 1              # half-joined call left (symmetry item)

    browser.fail_join = False                    # transient failure recovered
    assert await cs.start("F2", "guest", "+61", "D") is True
    await cs.stop("F2")
    assert cs.busy is False


@pytest.mark.asyncio
async def test_bridge_self_end_reconciles_and_frees_slot(monkeypatch):
    """When the bridge ends on its own, the reconcile frees the slot exactly once."""
    monkeypatch.setattr(session_mod, "RealtimeBridge", ShortBridge)
    browser = FakeBrowser()
    cs = session_mod.CallSession(_cfg(), browser, ApprovalStore())

    assert await cs.start("A", "guest", "+61", "X") is True
    for _ in range(50):                          # ShortBridge.run() ends ~20ms in
        await asyncio.sleep(0.01)
        if not cs.busy:
            break
    assert cs.busy is False and cs.active_token is None
    assert browser.leave_calls == 1              # freed exactly once (no redundant leave)
    assert not cs._lock.locked()
    assert _live_bridge_tasks() == []


@pytest.mark.asyncio
async def test_double_stop_is_idempotent(monkeypatch):
    """Two concurrent stop()s tear down once (single leave), and the trailing reconcile
    no-ops (generation-bound) — leave_calls stays at 1."""
    monkeypatch.setattr(session_mod, "RealtimeBridge", RunForeverBridge)
    browser = FakeBrowser()
    cs = session_mod.CallSession(_cfg(), browser, ApprovalStore())

    assert await cs.start("A", "owner", "+61", "E") is True
    await asyncio.gather(cs.stop("A"), cs.stop("A"))
    await asyncio.sleep(0.02)                     # let the trailing bridge-done reconcile run

    assert cs.busy is False
    assert browser.leave_calls == 1              # single teardown despite the reconcile
    assert _live_bridge_tasks() == []
