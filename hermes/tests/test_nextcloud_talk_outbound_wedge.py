"""s16 c3: the outbound hangup wedge - the slot that never frees.

s14b session 2, call 7: the owner hung up, Nextcloud cleared the room (`hasCall:false`),
and the bridge sat `busy:true` past every imagined watchdog until a manual
`POST /call/stop`. That row's `duration_s` inflated to 2955.9 (wall time to the manual
stop, not talk time - `eventlog.py:183` arithmetic is correct, its input was not).

ROOT CAUSE (c3a, proven by `test_c3a_*` below) - ONE mechanism, both broken links:

The plugin's 409 retry contract is INCOMPATIBLE with the outbound case. On an outbound
call the sidecar is busy *with the very room the plugin is looking at*, so:

  poll N   : diff -> ("start", tok); _active := {tok}
             _start_call -> POST /call/start -> 409 (busy with THIS call)
             -> tracker.forget(tok)  =>  _active := {}
  poll N+1 : same again, forever -- the token oscillates IN on diff, OUT on forget

The token is therefore ALWAYS out of `_active` at the moment the next diff runs. When the
room finally clears, `stops = _active - current` is EMPTY, so:

  * no ("stop", tok) is ever emitted  => `_stop_call` never runs => no POST /call/stop
  * `hangup_loop` was never armed either (it is created only after a 200 at :294-296)

kimi's two candidate links ("was hangup_loop armed?" / "did the stop diff fire?") are not
independent failures - they are the same 409-forget flaw seen from two ends. For an
INBOUND call 409 genuinely means "someone else's call, retry later"; for an OUTBOUND call
it means "this is MY call, track it and watch for the hangup". The plugin cannot tell
those apart today.

Run: python3 plugins/nextcloud_talk/tests/run_tests.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins", "nextcloud_talk"))

import voice_calls as vc  # noqa: E402


ACTIVE = {"token": "r1", "type": 1, "hasCall": True}
CLEARED = {"token": "r1", "type": 1, "hasCall": False}


class _StatusError(Exception):
    """The shape production actually raises.

    `_fetch_participants` calls `raise_for_status()` on a real httpx response, so the
    exception `_is_room_gone` inspects carries `.response.status_code`. This double used
    to raise `RuntimeError("HTTP 404")` instead, which meant every hangup test below
    exercised only the RuntimeError *fallback* arm - the s16 evaluator showed that
    `return status in (404, 403, 401)` could be replaced with `return False` and the
    whole plugin suite stayed green. httpx is deliberately NOT importable here (the
    plugin must import with no gateway dependency), so we mirror its structure rather
    than its class.
    """

    def __init__(self, response):
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"ocs": {"data": []}}
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _StatusError(self)


class FakeHTTP:
    """Records every sidecar POST and serves the room list the test scripts.

    `/call/start` answers 409 unconditionally - the steady state during an outbound call,
    because the sidecar really IS busy with that room. `/health` reports which call the
    sidecar is on, which is how the fix tells the two meanings of 409 apart.
    """

    def __init__(self, rooms, active_token="r1", participants=None, health_status=200):
        self.rooms = rooms
        self.active_token = active_token
        self.participants = participants if participants is not None else []
        self.health_status = health_status
        self.posts: list = []

    async def get(self, url, **kw):
        if url.endswith("/health"):
            return _Resp(self.health_status,
                         payload={"status": "ok", "busy": True,
                                  "active_token": self.active_token})
        if "/participants" in url:
            return _Resp(payload={"ocs": {"data": self.participants}})
        return _Resp(payload={"ocs": {"data": self.rooms}})

    async def post(self, url, **kw):
        self.posts.append((url, (kw.get("json") or {}).get("token")))
        if url.endswith("/call/start"):
            return _Resp(409)
        return _Resp(200)


class FakeClient:
    our_actor_id = "hermes-bot"

    def __init__(self, http):
        self._client = http


def _coord(http):
    return vc.VoiceCoordinator(
        FakeClient(http), "http://sidecar",
        owner_set={"Olivia"}, home="home", trigger_mode="smart", allowlist=[],
    )


def _stops(http):
    return [t for url, t in http.posts if url.endswith("/call/stop")]


def _starts(http):
    return [t for url, t in http.posts if url.endswith("/call/start")]


def _cancel(coord):
    for task in coord._hangup_tasks.values():
        task.cancel()


HUMAN = [{"actorId": "Olivia", "actorType": "users", "inCall": 1}]


# -- c3b: the wedge is closed -------------------------------------------------

def test_c3b_outbound_hangup_now_posts_call_stop_and_frees_the_slot():
    """THE FIX, end-to-end. Room active (bridge on an outbound call) -> 409 -> the plugin
    ADOPTS the call -> room clears -> /call/stop IS posted. RED before s16 c3."""
    http = FakeHTTP([ACTIVE])
    coord = _coord(http)

    asyncio.run(coord._poll_rooms())            # sees the active room, tries to join, 409
    asyncio.run(coord._poll_rooms())            # steady state: still 409, still adopted

    http.rooms = [CLEARED]                      # the owner hangs up; Nextcloud clears it
    asyncio.run(coord._poll_rooms())

    assert _starts(http), "precondition: the plugin did try to join"
    assert _stops(http) == ["r1"], (
        "the cleared room must diff into a ('stop', token) and post /call/stop -- this is "
        "the s14b call-7 wedge, and its absence is what left duration_s at 2955.9")
    _cancel(coord)


def test_c3b_the_token_stays_tracked_across_diff_boundaries():
    """WHY it works: adopt() keeps the token in _active, so `stops = _active - current`
    can actually contain it when the room clears."""
    http = FakeHTTP([ACTIVE])
    coord = _coord(http)

    asyncio.run(coord._poll_rooms())
    assert coord._tracker.active == {"r1"}, (
        "the 409-on-our-own-call path must ADOPT rather than forget")

    http.rooms = [CLEARED]
    assert coord._tracker.diff(http.rooms) == [("stop", "r1")]
    _cancel(coord)


def test_c3b_hangup_watcher_is_armed_on_the_adopted_call():
    """The other end of the same flaw: an adopted call gets a hangup watcher too, so a
    human leaving is noticed BEFORE the room clears."""
    http = FakeHTTP([ACTIVE])
    coord = _coord(http)
    asyncio.run(coord._poll_rooms())
    assert set(coord._hangup_tasks) == {"r1"}
    _cancel(coord)


def test_c3b_adopting_is_idempotent_across_repeated_409s():
    """The 409 recurs on EVERY poll for the life of an outbound call. Adoption must not
    stack a new hangup watcher each time (task leak / duplicate /call/stop)."""
    http = FakeHTTP([ACTIVE])
    coord = _coord(http)
    for _ in range(5):
        asyncio.run(coord._poll_rooms())
    assert set(coord._hangup_tasks) == {"r1"}
    assert len(coord._hangup_tasks) == 1, "a watcher was stacked per poll"
    _cancel(coord)


def test_c3b_409_for_someone_elses_call_still_forgets_and_retries():
    """The retry contract MUST survive. When the sidecar is on a DIFFERENT room, 409 is
    transient and the old forget-and-re-emit behaviour is correct - adopting here would
    strand a real caller and suppress the retry."""
    http = FakeHTTP([ACTIVE], active_token="some-other-room")
    coord = _coord(http)
    asyncio.run(coord._poll_rooms())
    assert coord._tracker.active == set(), "must forget: this is not our call"
    assert coord._hangup_tasks == {}, "must not watch a call we are not on"
    # ...and the next poll re-emits it as a fresh start (the retry contract)
    asyncio.run(coord._poll_rooms())
    assert _starts(http) == ["r1", "r1"]


def test_c3b_health_probe_failure_fails_closed():
    """If we cannot tell whose call it is, keep the OLD behaviour. Wrongly adopting a room
    the sidecar is not on would suppress a legitimate retry forever."""
    http = FakeHTTP([ACTIVE], health_status=500)
    coord = _coord(http)
    asyncio.run(coord._poll_rooms())
    assert coord._tracker.active == set()
    assert coord._hangup_tasks == {}


# -- c3b: a deleted room ends the call instead of spinning forever ------------

def _run_hangup(coord, token="r1", timeout=2.0):
    """Drive the REAL hangup_loop to completion, bounded.

    `poll_interval = 0` makes its `asyncio.sleep` a plain yield - the loop still goes
    through the real await points (a no-op sleep coroutine would starve the event loop and
    hang `wait_for` instead of timing out). A loop that never terminates surfaces as
    TimeoutError, which is the assertion in the transient-failure test.
    """
    coord.poll_interval = 0
    return asyncio.run(asyncio.wait_for(coord.hangup_loop(token), timeout=timeout))


def test_c3b_deleted_room_counts_toward_hangup_and_stops_the_call():
    """404 on participants = the room is GONE. Before s16 this was swallowed by the broad
    `except Exception`, so gone_streak never advanced and the loop spun forever against a
    room that no longer existed."""
    http = FakeHTTP([ACTIVE], participants=HUMAN)
    coord = _coord(http)
    coord._seen_present.add("r1")               # a human WAS in the call

    async def gone(url, **kw):                  # every participants read 404s
        if url.endswith("/health"):
            return _Resp(200, payload={"active_token": "r1"})
        return _Resp(404)
    http.get = gone

    _run_hangup(coord)
    assert _stops(http) == ["r1"], "a deleted room must end the call"


def test_c3b_transient_5xx_does_NOT_end_a_live_call():
    """The counterpart that keeps the fix honest: a flaky OCS 500 must not tear down a
    healthy call. Only gone-shaped failures count."""
    http = FakeHTTP([ACTIVE], participants=HUMAN)
    coord = _coord(http)
    coord._seen_present.add("r1")
    calls = {"n": 0}

    async def flaky(url, **kw):
        if url.endswith("/health"):
            return _Resp(200, payload={"active_token": "r1"})
        calls["n"] += 1
        return _Resp(500)
    http.get = flaky

    try:
        _run_hangup(coord)
    except asyncio.TimeoutError:
        pass                                     # expected: it keeps trying, as it should
    assert _stops(http) == [], "a transient 5xx must never end a live call"
    assert calls["n"] > HANGUP_GONE_THRESHOLD_LOCAL, "it should have retried, not bailed"


def test_c3b_is_room_gone_reads_the_status_off_the_production_exception_shape():
    """Direct cover for the line the evaluator sabotaged.

    The integration tests above go through `raise_for_status`; this pins the classifier
    itself against the httpx-shaped exception, so a regression is attributed to
    `_is_room_gone` rather than to a hangup loop three layers up.
    """
    for code in (404, 403, 401):
        assert vc._is_room_gone(_StatusError(_Resp(code))) is True, (
            f"HTTP {code} means the room is gone as far as we can see - it must end "
            "the call, not spin forever against a room that no longer exists")


def test_c3b_is_room_gone_rejects_transient_and_unknown_shapes():
    """The counterpart: nothing that is merely broken may tear down a live call."""
    for code in (500, 502, 503, 429):
        assert vc._is_room_gone(_StatusError(_Resp(code))) is False, (
            f"HTTP {code} is transient - ending a live call on it is worse than the "
            "wedge this fix exists to close")
    assert vc._is_room_gone(TimeoutError("read timeout")) is False
    assert vc._is_room_gone(Exception("something else entirely")) is False


HANGUP_GONE_THRESHOLD_LOCAL = vc.HANGUP_GONE_THRESHOLD


def test_c3a_positive_control_inbound_still_works():
    """Not a blanket breakage: when the sidecar ACCEPTS the join (200 - the inbound
    case), the token stays tracked and the cleared room DOES post /call/stop.

    Without this arm the defect tests above could pass on a coordinator that is simply
    broken for every call, which would misdirect the fix."""
    http = FakeHTTP([ACTIVE])

    async def post_ok(url, **kw):
        http.posts.append((url, (kw.get("json") or {}).get("token")))
        return _Resp(200)

    http.post = post_ok
    coord = _coord(http)

    asyncio.run(coord._poll_rooms())
    assert coord._tracker.active == {"r1"}
    assert coord._hangup_tasks, "a 200 join DOES arm the hangup watcher"

    http.rooms = [CLEARED]
    asyncio.run(coord._poll_rooms())
    assert _stops(http) == ["r1"], "the inbound path posts /call/stop correctly"

    for task in coord._hangup_tasks.values():
        task.cancel()
