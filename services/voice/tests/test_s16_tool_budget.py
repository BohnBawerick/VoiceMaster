"""s16 c1: ONE declared tool budget — the 47.0s stacking, closed.

s14b call 7 logged `hermes_agent` twice at EXACTLY 47.0s. Identical durations mean a
timer, not load, and the timer was two timers in series inside `_run_tool`:

    await asyncio.wait({task}, timeout=self._filler_debounce_s)   # burns the debounce
    await asyncio.wait_for(task, timeout=TOOL_TIMEOUT_S)          # ...THEN starts 45s

`cascade_bridge.py` floored the debounce at 2.0, so the real ceiling was 2.0 + 45.0 =
47.0 — the logged figure, to the decimal. The debounce is now a filler-start deadline
INSIDE the budget rather than runway added on top of it.

These tests live in `services/voice` because `cascade_live.py` is one md5-pinned twin
shared by both cascade lanes; proving the arithmetic once proves it for PSTN and Talk.
"""
import asyncio
import time

import pytest

from voicecore import cascade_live
from test_cascade_live import (CONFIG, ENV, FakeDeepgram, FakeRecorder, FakeTwilioWS,
                               make_session, make_transport)


def _hung_tool_session(monkeypatch, *, budget, debounce, record=None):
    """A session whose backend never returns, so the budget is what ends the turn."""
    async def hung(instruction):
        await asyncio.sleep(30)
        return "never"

    monkeypatch.setattr(cascade_live, "TOOL_BUDGET_S", budget)
    transport = make_transport(replies=("Sorry about that.",), tool_call_first=True,
                               record=record if record is not None else [])
    return make_session(FakeTwilioWS(), FakeDeepgram(), FakeRecorder(),
                        transport=transport, tools_enabled=True, hermes_call=hung,
                        filler_debounce_s=debounce)


# -- the headline: total wall-clock == the DECLARED budget --------------------

def test_c1_total_wall_clock_is_the_budget_not_debounce_plus_budget(monkeypatch):
    """THE DEFECT, in miniature. budget 0.6 + debounce 0.4:

    pre-fix  -> 0.4 burned, THEN 0.6 counted = ~1.0s  (the 2.0+45.0=47.0 shape)
    post-fix -> the debounce comes out of the budget  = ~0.6s

    Asserted with a NON-ZERO debounce on purpose: a test using debounce=0 never executes
    the stacked path at all and passes on the broken tree.
    """
    session = _hung_tool_session(monkeypatch, budget=0.6, debounce=0.4)

    started = time.monotonic()
    asyncio.run(session._agent_turn(opener=True))
    elapsed = time.monotonic() - started

    assert elapsed < 0.85, (
        f"tool turn took {elapsed:.2f}s against a declared 0.6s budget — the debounce is "
        "still being added to the cap instead of spent inside it (this is the 2.0+45.0="
        "47.0s shape that produced two identical 47.0s rows in s14b)")
    assert elapsed >= 0.55, (
        f"tool turn took only {elapsed:.2f}s — the budget is not being honoured at all")


def test_c1_a_larger_debounce_does_not_extend_the_ceiling(monkeypatch):
    """Same budget, debounce nearly as large: the ceiling must not move.

    This is the arm that a 'fix' which merely LOWERS TOOL_TIMEOUT_S (so 2+43=45 looks
    right) cannot pass — with the structure still additive, a bigger debounce pushes the
    total straight back out.
    """
    session = _hung_tool_session(monkeypatch, budget=0.6, debounce=0.55)

    started = time.monotonic()
    asyncio.run(session._agent_turn(opener=True))
    elapsed = time.monotonic() - started

    assert elapsed < 0.85, (
        f"a larger debounce pushed the turn to {elapsed:.2f}s — the budget is not a single "
        "wall-clock deadline")


def test_c1_the_timeout_still_yields_a_tool_message_never_a_hang(monkeypatch):
    """The budget must END the turn with something to say — the pre-existing s8 c1
    contract, re-pinned so the c1 rework cannot regress it into silence."""
    session = _hung_tool_session(monkeypatch, budget=0.3, debounce=0.05)
    asyncio.run(session._agent_turn(opener=True))
    tool_msgs = [m for m in session.messages if m.get("role") == "tool"]
    # c2 changed the tool message from the apology TEXT to an instruction saying the
    # apology was already spoken aloud (speech no longer depends on a second LLM round).
    assert tool_msgs and "timed out" in tool_msgs[0]["content"]


def test_c1_a_fast_tool_is_untouched_by_the_budget(monkeypatch):
    """A tool that answers inside the debounce must not pay any of it — trivial turns
    stay snappy and never even start a filler."""
    async def fast(instruction):
        return "the answer"

    monkeypatch.setattr(cascade_live, "TOOL_BUDGET_S", 5.0)
    transport = make_transport(replies=("Got it.",), tool_call_first=True, record=[])
    session = make_session(FakeTwilioWS(), FakeDeepgram(), FakeRecorder(),
                           transport=transport, tools_enabled=True, hermes_call=fast,
                           filler_debounce_s=1.0)

    started = time.monotonic()
    asyncio.run(session._agent_turn(opener=True))
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"a fast tool waited {elapsed:.2f}s — the debounce is blocking"
    tool_msgs = [m for m in session.messages if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["content"] == "the answer"


# -- s8 c1 regression: a barge must not kill the dispatch ---------------------

def test_c1_barge_during_a_tool_call_does_NOT_cancel_the_dispatch(monkeypatch):
    """s8 c1, re-pinned. The caller interjecting while a tool runs stops the filler AUDIO
    but must leave the backend call running — otherwise the interjection destroys the
    answer and the turn ends with nothing, which is the s7 dead-air defect returning by
    the back door. A budget rework that collapses the two waits into one cancel-on-barge
    is exactly how that regression would ship.
    """
    finished = asyncio.Event()

    async def slow(instruction):
        await asyncio.sleep(0.2)
        finished.set()
        return "the real answer"

    monkeypatch.setattr(cascade_live, "TOOL_BUDGET_S", 5.0)
    transport = make_transport(replies=("Right.",), tool_call_first=True, record=[])
    session = make_session(FakeTwilioWS(), FakeDeepgram(), FakeRecorder(),
                           transport=transport, tools_enabled=True, hermes_call=slow,
                           filler_debounce_s=0.02)

    async def drive():
        turn = asyncio.create_task(session._agent_turn(opener=True))
        await asyncio.sleep(0.08)              # filler is playing by now
        await session._handle_barge_in()       # the caller interjects
        await turn

    asyncio.run(drive())

    assert finished.is_set(), "the barge cancelled the backend dispatch (s8 c1 regression)"
    tool_msgs = [m for m in session.messages if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["content"] == "the real answer", (
        "the caller's interjection destroyed the tool result")
