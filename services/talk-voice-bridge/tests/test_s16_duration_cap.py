"""s16 c3c: the lane-agnostic hard call ceiling.

s14b call 7 wedged the single-call slot: the owner hung up, Nextcloud cleared the room,
and the bridge sat `busy:true` until a human posted `/call/stop` by hand. `duration_s`
reached 2955.9 — the eventlog arithmetic was right, its input was a call that never ended.

The plugin-side detector is the real fix (c3b, `plugins/nextcloud_talk/`). This is the
BACKSTOP for when every detector fails, and it is deliberately NOT an idle watchdog:

  RealtimeBridge can measure idleness because its WS delivers discrete speech events
  (`realtime_bridge.py:429`). The cascade lane cannot -- parec streams silence forever, so
  frame arrival says nothing, and treating "no detected speech" as idle would hang up on a
  quiet callee mid-listen. A wall-clock ceiling needs no activity signal at all, which is
  exactly why it can live in the SHARED layer and cover both pipelines identically.

Placing it in `CallSession` rather than duplicating an `_idle_watchdog` into
`cascade_bridge` is the s15 thesis applied: the s14b defects were realtime-shaped
assumptions in the thin shared layer, and a fourth lane should inherit the bound for free.
"""
import asyncio

import pytest

import parity_env as pe
import session as session_mod
from approval import ApprovalStore
from config import load


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Teardown makes real OCS calls (s15d verification) with a 20s OCS_TIMEOUT.

    Run alone these fail instantly ("URL is missing an http:// protocol") and the ceiling
    looks fast; run after a test that leaves a plausible NEXTCLOUD_BASE_URL in os.environ
    they attempt a real connection and teardown takes ~20s. That made this file pass in
    isolation and fail in the full suite — ambient env, not a slow fix. Sandboxing the
    config env makes the timing deterministic either way.
    """
    for var in pe.CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class FakeBrowser:
    def __init__(self):
        self.join_calls = []
        self.leave_calls = 0

    async def join_call(self, token):
        self.join_calls.append(token)

    async def start_call(self, token):      # outbound: ring the other party
        self.join_calls.append(token)

    async def leave_call(self):
        self.leave_calls += 1


class RunForeverBridge:
    """A call that never ends on its own — the wedge, reproduced."""

    def __init__(self, *a, **kw):
        self.stop_calls = 0
        self._ended = asyncio.Event()

    async def run(self):
        await self._ended.wait()

    async def stop(self):
        self.stop_calls += 1
        self._ended.set()


def _session(max_call_s):
    """A CallSession with a short ceiling.

    `_max_call_s` is a CallSession attribute read from TALK_VOICE_MAX_CALL_S, NOT a Config
    field — Config's surface is pinned byte-for-byte by the d36113c parity goldens, and
    this is a session-lifecycle tunable (same idiom as `_rejoin_cooldown_s`, s15d).
    """
    cs = session_mod.CallSession(load(), FakeBrowser(), ApprovalStore())
    cs._max_call_s = max_call_s
    return cs


def _cap_tasks():
    return [t for t in asyncio.all_tasks()
            if (t.get_name() or "").startswith("mode-v-maxdur-") and not t.done()]


async def _wait_free(cs, timeout=5.0):
    """Wait for the slot to free, rather than racing a fixed sleep against teardown.

    The criterion is "a wedged call is bounded", NOT "bounded within 200ms": teardown does
    real work (leave_call, then two OCS verification calls from s15d) whose duration
    depends on ambient env — the fixed-sleep version passed alone and failed in the full
    suite, which is a flaky test, not a slower fix.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while cs.busy and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.01)
    return not cs.busy


@pytest.mark.asyncio
async def test_c3c_a_wedged_call_is_torn_down_at_the_ceiling(monkeypatch):
    """THE BACKSTOP. A bridge that never returns must not hold the slot forever."""
    monkeypatch.setattr(session_mod, "RealtimeBridge", RunForeverBridge)
    browser = FakeBrowser()
    cs = _session(0.05)
    cs._browser = browser

    assert await cs.start("tokW", "owner", "+61", "Alex") is True
    assert cs.busy is True

    assert await _wait_free(cs), (
        "the ceiling did not fire — this is the s14b wedge, where the slot stayed busy "
        "until a human posted /call/stop")
    assert cs.active_token is None
    assert browser.leave_calls >= 1, "tearing down must also leave the Talk call"


@pytest.mark.asyncio
async def test_c3c_a_normal_call_is_untouched_by_the_ceiling(monkeypatch):
    """The ceiling must be invisible to real calls. A call that ends normally well inside
    the bound tears down through its OWN path, and leaves no pending cap task behind."""
    monkeypatch.setattr(session_mod, "RealtimeBridge", RunForeverBridge)
    cs = _session(30.0)

    assert await cs.start("tokN", "owner", "+61", "Alex") is True
    await cs.stop("tokN")

    assert cs.busy is False
    await asyncio.sleep(0)
    assert _cap_tasks() == [], "the cap task leaked past the call it belonged to"


@pytest.mark.asyncio
async def test_c3c_a_stale_ceiling_cannot_tear_down_a_NEWER_call(monkeypatch, caplog):
    """Generation safety — the trap this class of fix usually falls into.

    Call A arms a ceiling. A ends early. Call B claims the slot. A's ceiling then fires.
    It must recognise it is stale and do nothing: killing a healthy call B would be a far
    worse defect than the wedge this exists to bound.

    HONEST SCOPE (verified by sabotage, s16): the survival of call B is enforced by TWO
    independent guards — `_duration_cap`'s generation check AND `_teardown`'s own
    `expected_gen` refusal — so removing the former alone does NOT redden the survival
    assertion. What `_duration_cap`'s check uniquely prevents is the misleading
    "exceeded max_call_duration" WARNING being logged against a call that ended normally,
    which is asserted separately below. Do not claim the first assertion proves that
    branch is load-bearing; it does not.
    """
    monkeypatch.setattr(session_mod, "RealtimeBridge", RunForeverBridge)
    cs = _session(0.05)

    # Call A: arm a SHORT ceiling, then neutralise the cancel-on-teardown so the task
    # SURVIVES its own call -- reproducing a late timer that teardown failed to clean up.
    assert await cs.start("tokA", "owner", "+61", "Alex") is True
    leaked = cs._duration_task
    cs._duration_task = None                     # teardown will no longer cancel it
    await cs.stop("tokA")

    # Call B claims the freed slot with a LONG ceiling of its own, so anything that tears
    # it down inside this test can only be A's stale timer -- not B's legitimate one.
    cs._max_call_s = 30.0
    assert await cs.start("tokB", "owner", "+61", "Alex") is True
    assert cs.active_token == "tokB"

    await asyncio.sleep(0.2)                     # A's stale ceiling fires in here

    assert cs.busy is True and cs.active_token == "tokB", (
        "a stale ceiling from call A tore down the live call B")
    assert leaked.done()

    # THIS is the assertion that pins _duration_cap's own generation check: a stale
    # ceiling must stay SILENT. Without that branch the warning fires against tokB, and an
    # operator reading the log after a wedge sees a defect signal for a call that was fine.
    exceeded = [r for r in caplog.records if "exceeded max_call_duration" in r.getMessage()]
    assert exceeded == [], (
        f"a stale ceiling logged a false defect signal: {[r.getMessage() for r in exceeded]}")
    await cs.stop("tokB")


@pytest.mark.asyncio
async def test_c3c_the_ceiling_is_armed_for_the_CASCADE_lane_too(monkeypatch):
    """The whole point of putting this in CallSession: it is pipeline-agnostic.

    Asserted on the cascade construction path, because the lane that cannot self-bound is
    the one that most needs the backstop — and a fix that only ever runs for realtime
    would reproduce the exact s14b blindness (realtime-shaped assumption, shared layer).
    """
    import cascade_bridge

    monkeypatch.setattr(cascade_bridge, "CascadeBridge", RunForeverBridge)
    cs = _session(30.0)

    mission = session_mod.OutboundMission(brief="say hi", target_display="Alex")
    assert await cs.start("tokC", "owner", "+61", "Alex", mission=mission) is True
    assert cs._duration_task is not None and not cs._duration_task.done(), (
        "the cascade lane got no duration ceiling")
    await cs.stop("tokC")
