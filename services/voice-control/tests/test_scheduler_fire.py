"""Ticket 11: when a Schedule's time comes, the phone rings — down the manual path.

The rule this file exists to bind: **a scheduled Call travels the identical path
as a manual one**. Not an equivalent path — the same code. Two tests hold that
line and are meant to be the first thing to go red if someone gives the
scheduler its own dial:

  * ``test_the_scheduled_dial_is_byte_identical_to_the_manual_one`` compares the
    actual HTTP request that reaches the phone bridge from each path;
  * ``test_both_paths_go_through_the_one_placement`` replaces the one placement
    function and watches both callers arrive at it.

The rest is the part that is genuinely hard: firing exactly once across a
restart, two workers, a cancellation that arrives as the clock strikes, and
never a second attempt (VC14).
"""
import copy
import json
import time
from datetime import timedelta

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

import place_call
import schedules
import scheduler as call_scheduler
from conftest import RecordingTransport, SentinelTransport

OWNER = "+61491570156"
OTHER = "+61899990000"
PERTH = "Australia/Perth"

AGENT_A = {
    "id": "agent-a",
    "description": "the scheduled one",
    "enabled": True,
    "pipeline": "realtime",
    "providers": {"realtime": "openai-gpt-realtime"},
    "number_policy": {"allow": [OWNER]},
}

AGENT_B = {
    "id": "agent-b",
    "description": "the assigned one",
    "enabled": True,
    "pipeline": "realtime",
    "providers": {"realtime": "openai-gpt-realtime"},
}


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

def _write_agent(tmp_path, doc):
    (tmp_path / "agents").mkdir(exist_ok=True)
    (tmp_path / "agents" / f"{doc['id']}.yaml").write_text(yaml.safe_dump(doc))


def _write_pointer(tmp_path, phone_outbound=None):
    path = tmp_path / "active.yaml"
    path.write_text(yaml.safe_dump({
        "outlets": {
            "phone": {"inbound": None, "outbound": phone_outbound},
            "talk": {"inbound": None, "outbound": None},
        }
    }))
    return path


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HERMES_GATEWAY_TOKEN", "gw-token")
    monkeypatch.setenv("VOICE_MODE_C_URL", "http://127.0.0.1:3336")
    monkeypatch.setenv("VOICE_TIMEZONE", PERTH)
    monkeypatch.setenv("VOICE_LKG_DIR", str(tmp_path / "events"))
    (tmp_path / "events").mkdir()
    _write_agent(tmp_path, AGENT_A)
    _write_agent(tmp_path, AGENT_B)
    _write_pointer(tmp_path, phone_outbound="agent-b")
    return tmp_path


def bridge_transport(status=200, call_sid="CAsched1", error=None):
    async def handler(request):
        assert request.url.host == "127.0.0.1", request.url
        assert request.url.path == "/voice/outbound"
        if status != 200:
            return httpx.Response(status, json={
                "error": error or "an outbound call is already in progress"})
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "placed": True, "call_sid": call_sid,
            "call_id": "cid-" + body.get("agent", "x"),
            "agent": body.get("agent")})

    return RecordingTransport(handler)


def unreachable_transport():
    async def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    return RecordingTransport(handler)


def _scheduler(application):
    """A scheduler over the same config dir and the same transport the app uses.

    Constructed rather than started: ``tick`` is one pass, which is what makes
    'restart the scheduler' expressible as 'throw this one away and build
    another'.
    """
    return call_scheduler.Scheduler(
        transport_get=lambda: application.state.transport)


def _at(seconds_from_now: float) -> str:
    return schedules.to_iso(schedules.now_utc() + timedelta(seconds=seconds_from_now))


def _at_least(seconds_from_now: int) -> str:
    """An instant AT LEAST this many seconds away.

    Instants are stored to the second (they are rounded down), so ``_at(2)`` can
    be anything from just over one second away to two. Where a test asserts
    "not due yet" and then sleeps, that slack is the difference between testing
    the restart and testing the machine's load, so this rounds the other way.
    """
    floor = schedules.now_utc().replace(microsecond=0)
    return schedules.to_iso(floor + timedelta(seconds=seconds_from_now + 1))


def _create(client, at=None, agent="agent-a", to=OWNER,
            mission="Ask if Friday still works.", disclose=False, **extra):
    body = {"agent": agent, "to": to, "mission": mission, "disclose": disclose,
            "at": at if at is not None else _at(-5), **extra}
    return client.post("/api/schedules", json=body)


class _Clock:
    """A clock the test moves by hand.

    Patched over ``schedules.now_utc``, which is the ONE place this service
    reads the time — the routes, the store and the scheduler all go through it,
    so moving this moves all of them together and no half of the system can
    disagree with the other about what "now" is.
    """

    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)

    def iso_in(self, seconds) -> str:
        return schedules.to_iso(self.now + timedelta(seconds=seconds))


@pytest.fixture
def clock(monkeypatch):
    start = schedules.now_utc().replace(microsecond=0)
    instance = _Clock(start)
    monkeypatch.setattr(schedules, "now_utc", instance)
    return instance


def _dials(transport):
    return [q for q in transport.calls if q.url.path == "/voice/outbound"]


def _sent(transport):
    return [json.loads(q.content) for q in _dials(transport)]


# --------------------------------------------------------------------------
# The rule: one path, not two
# --------------------------------------------------------------------------

async def test_the_scheduled_dial_is_byte_identical_to_the_manual_one(
        make_app, config):
    """Same Call, placed both ways: the phone bridge cannot tell them apart.

    This compares the REQUEST that arrives at the bridge — URL, method, auth
    header and body — not the two call sites' intentions. Any field the manual
    path grew that the scheduled one did not (or the reverse) shows up here as
    a diff, which is the point: the scheduled path cannot rot quietly while the
    manual one is maintained.
    """
    transport = bridge_transport()
    application = make_app(transport)
    client = TestClient(application)

    manual = client.post("/api/calls/place", json={
        "agent": "agent-a", "to": "+61 899 990 000",
        "mission": "Ask if Friday still works.", "disclose": True,
        "target_display": "Friday person"})
    assert manual.status_code == 200, manual.text

    created = _create(client, to="+61 899 990 000", disclose=True,
                      target_display="Friday person")
    assert created.status_code == 201, created.text
    settled = await _scheduler(application).tick()
    assert [r["status"] for r in settled] == [schedules.STATUS_PLACED]

    dials = _dials(transport)
    assert len(dials) == 2, "one manual, one scheduled"
    by_hand, by_schedule = dials
    assert by_hand.method == by_schedule.method
    assert str(by_hand.url) == str(by_schedule.url)
    assert by_hand.headers.get("authorization") == \
        by_schedule.headers.get("authorization") == "Bearer gw-token"
    assert json.loads(by_hand.content) == json.loads(by_schedule.content)


async def test_both_paths_go_through_the_one_placement(make_app, config,
                                                       monkeypatch):
    """Replace THE placement, and watch both callers arrive at it.

    A scheduler that reimplemented validation, the Agent check or the dial —
    however faithfully — would not be seen here, and this goes red.
    """
    seen = []
    real = place_call.place_from_request

    async def recording(body, **kw):
        seen.append(copy.deepcopy(body))
        return await real(body, **kw)

    monkeypatch.setattr(place_call, "place_from_request", recording)

    transport = bridge_transport()
    application = make_app(transport)
    client = TestClient(application)
    client.post("/api/calls/place", json={
        "agent": "agent-a", "to": OWNER, "mission": "M", "disclose": True,
        "target_display": ""})
    assert len(seen) == 1, "the manual route does not go through the placement"

    assert _create(client, mission="M", disclose=True).status_code == 201
    await _scheduler(application).tick()
    assert len(seen) == 2, "the scheduled path does not go through the placement"
    assert seen[0] == seen[1]


async def test_a_schedule_is_graded_by_the_placement_it_will_use(make_app, config):
    """The refusals are the manual ones, word for word — at creation AND when it
    fires. A Schedule refused at 3pm for a reason that could have been given at
    the keyboard is the failure this shares code to avoid."""
    transport = bridge_transport()
    client = TestClient(make_app(transport))
    manual = client.post("/api/calls/place", json={
        "agent": "ghost", "to": OWNER, "mission": "M"})
    scheduled = _create(client, agent="ghost")
    assert manual.status_code == scheduled.status_code == 409
    assert manual.json()["detail"] == scheduled.json()["detail"]

    for bad in ({"agent": "agent-a", "to": "not-a-number", "mission": "M"},
                {"agent": "agent-a", "to": OWNER, "mission": "  "},
                {"agent": "", "to": OWNER, "mission": "M"},
                {"agent": "agent-a", "to": OWNER, "mission": "M",
                 "disclose": "yes please"}):
        manual = client.post("/api/calls/place", json=bad)
        scheduled = client.post("/api/schedules", json={**bad, "at": _at(600)})
        assert manual.status_code == scheduled.status_code == 422, bad
        assert manual.json()["detail"] == scheduled.json()["detail"], bad
    assert not _dials(transport)


async def test_the_reason_a_fired_schedule_failed_is_what_the_screen_would_say(
        make_app, config):
    """The recorded reason is the sentence the manual path shows, not a code."""
    transport = bridge_transport(status=409, error="an outbound call is already "
                                                   "in progress")
    application = make_app(transport)
    client = TestClient(application)
    manual = client.post("/api/calls/place", json={
        "agent": "agent-a", "to": OWNER, "mission": "M"})
    assert manual.status_code == 409

    assert _create(client).status_code == 201
    settled = await _scheduler(application).tick()
    assert len(settled) == 1
    assert settled[0]["status"] == schedules.STATUS_FAILED
    assert settled[0]["reason"] == "; ".join(manual.json()["detail"])
    assert settled[0]["failure_status"] == 409


# --------------------------------------------------------------------------
# One-shot: a scheduled Call is not a configuration of the Outlet
# --------------------------------------------------------------------------

async def test_a_fired_schedule_does_not_rewrite_the_outlets_known_good(
        make_app, config):
    """Ticket 09's invariant, kept by ticket 11.

    A scheduled Call is still a per-call binding: it names its Agent on the
    dial, so the bridge takes the one-shot path that skips ``lkg.record`` at
    teardown. Three things are asserted, and the first is the load-bearing one
    because the other two are the dashboard's own hands: the dial carries
    ``agent`` (the one-shot marker the bridge branches on), the pointer file is
    untouched, and no last-known-good snapshot appears.

    The bridge half of this — that a dial carrying ``agent`` really does leave
    the snapshot alone through a whole call — is bound in the phone bridge's own
    suite, against the SAME payload builder:
    services/voice/tests/test_scheduled_call_is_a_oneshot.py
    """
    pointer = config / "active.yaml"
    before = pointer.read_text()
    events = config / "events"
    transport = bridge_transport()
    application = make_app(transport)
    client = TestClient(application)

    assert _create(client, agent="agent-a").status_code == 201
    await _scheduler(application).tick()

    sent = _sent(transport)
    assert len(sent) == 1
    assert sent[0]["agent"] == "agent-a", "a scheduled dial must be a one-shot"
    assert pointer.read_text() == before
    assert list(events.glob("lkg-*.json")) == []


async def test_a_scheduled_call_carries_its_mission_to_the_bridge(make_app, config):
    """The Mission is what makes the Call appear on the Calls screen as itself
    (ticket 05 writes it through the one metadata builder). It travels as the
    bridge's ``brief`` field, exactly as a manual place sends it."""
    transport = bridge_transport()
    application = make_app(transport)
    client = TestClient(application)
    assert _create(client, mission="Ask the plumber about Tuesday.").status_code == 201
    await _scheduler(application).tick()
    assert _sent(transport)[0]["brief"] == "Ask the plumber about Tuesday."


async def test_a_scheduled_call_dials_a_number_no_list_allows(make_app, config):
    """Outbound is allow-any and stays that way on the scheduled path too:
    agent-a's number_policy names only the owner, and OTHER still dials."""
    transport = bridge_transport()
    application = make_app(transport)
    client = TestClient(application)
    assert _create(client, to=OTHER).status_code == 201
    await _scheduler(application).tick()
    assert _sent(transport)[0]["to"] == OTHER


# --------------------------------------------------------------------------
# Exactly once: restarts, second workers, repeated ticks
# --------------------------------------------------------------------------

async def test_a_schedule_due_while_nothing_was_running_fires_once_on_restart(
        make_app, config):
    """The container was down when it came due, and comes back inside the grace
    window. One call — not zero, and not one per tick afterwards."""
    transport = bridge_transport()
    application = make_app(transport)
    client = TestClient(application)
    assert _create(client, at=_at(-30)).status_code == 201

    # "Restart": a scheduler that has never seen this Schedule before.
    fresh = _scheduler(application)
    assert [r["status"] for r in await fresh.tick()] == [schedules.STATUS_PLACED]
    assert len(_dials(transport)) == 1

    # And again, and from another new instance: still one call.
    await fresh.tick()
    await _scheduler(application).tick()
    await _scheduler(application).tick()
    assert len(_dials(transport)) == 1


async def test_a_restart_in_the_middle_of_the_due_window_still_fires_once(
        make_app, config, clock):
    """Ticking, restarting and ticking again around the due instant.

    The scheduler is thrown away and rebuilt between every pass, which is what
    a container restart is: if firing were remembered in the process rather
    than on the disk, the second pass would place a second call.

    **The clock here is the test's, not the wall's.** What this asserts is the
    claim's behaviour either side of a due instant, and the passing of time is
    only the setup for it. An earlier version slept a fixed interval, which
    made it red on a host whose clock stepped — a test that goes red for a
    reason unrelated to the code is worse than no test, because the next person
    to see it will blame the clock and one day be wrong. Real elapsed time is
    still covered, end to end, by
    ``test_the_running_app_places_the_call_at_the_appointed_time`` and by the
    browser suite.
    """
    transport = bridge_transport()
    application = make_app(transport)
    client = TestClient(application)
    assert _create(client, at=clock.iso_in(30)).status_code == 201

    assert await _scheduler(application).tick() == []       # not due yet
    assert len(_dials(transport)) == 0
    clock.advance(60)                                       # the due time passes
    assert len(await _scheduler(application).tick()) == 1   # due: fires
    for _ in range(5):                                      # five more restarts
        assert await _scheduler(application).tick() == []
    assert len(_dials(transport)) == 1


async def test_two_schedulers_racing_the_same_due_schedule_place_one_call(
        make_app, config):
    """Two workers, one Schedule, one phone call.

    The two instances share nothing but the directory, so the claim is what
    stops the second dial — and the second worker has to reach its decision
    while the first is still ON the call, or the race never happens and the
    terminal status does the work instead. So the first dial is held open
    until the second tick has been all the way through: the mock bridge blocks
    the first request, the test lets the loop run the second scheduler, and
    only then releases it.

    Take ``schedules.claim`` out of ``_fire`` and this goes red with two dials.
    An earlier version of this test used a transport that answered without ever
    yielding to the event loop, so the two ticks ran end to end one after the
    other and it passed with the claim removed — a race test that never raced.
    """
    import asyncio

    dialling = asyncio.Event()      # a dial has begun
    release = asyncio.Event()       # let it finish

    async def handler(request):
        dialling.set()
        await release.wait()
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "placed": True, "call_sid": "CAsched1",
            "call_id": "cid-" + body.get("agent", "x"),
            "agent": body.get("agent")})

    transport = RecordingTransport(handler)
    application = make_app(transport)
    client = TestClient(application)
    assert _create(client).status_code == 201

    first, second = _scheduler(application), _scheduler(application)
    both = asyncio.gather(first.tick(), second.tick())
    await dialling.wait()           # the first worker is inside the dial
    await asyncio.sleep(0)          # ...and the second has now had its turn
    release.set()
    outcomes = await both

    assert len(_dials(transport)) == 1, "the second worker dialled as well"
    assert sum(len(o) for o in outcomes) == 1, outcomes
    assert schedules.load_all()[0]["status"] == schedules.STATUS_PLACED


async def test_a_schedule_that_failed_is_never_tried_again(make_app, config):
    """One attempt only (VC14). A refused dial is a failure with a reason, and
    every later tick leaves it alone — the deliberate absence of a retry."""
    transport = bridge_transport(status=409)
    application = make_app(transport)
    client = TestClient(application)
    assert _create(client).status_code == 201

    assert len(await _scheduler(application).tick()) == 1
    for _ in range(5):
        assert await _scheduler(application).tick() == []
    assert len(_dials(transport)) == 1
    assert schedules.load_all()[0]["status"] == schedules.STATUS_FAILED


async def test_an_unreachable_bridge_fails_the_schedule_honestly(make_app, config):
    transport = unreachable_transport()
    application = make_app(transport)
    client = TestClient(application)
    assert _create(client).status_code == 201
    settled = await _scheduler(application).tick()
    assert settled[0]["status"] == schedules.STATUS_FAILED
    assert "unreachable" in settled[0]["reason"]
    assert "no call placed" in settled[0]["reason"]
    assert await _scheduler(application).tick() == []


async def test_an_agent_deleted_after_the_schedule_was_written_fails_it(
        make_app, config):
    """The Agent check happens again at fire time — the same one, so the same
    words — because a profile can be deleted in between."""
    transport = bridge_transport()
    application = make_app(transport)
    client = TestClient(application)
    assert _create(client, agent="agent-a").status_code == 201
    (config / "agents" / "agent-a.yaml").unlink()

    settled = await _scheduler(application).tick()
    assert settled[0]["status"] == schedules.STATUS_FAILED
    assert "agent-a" in settled[0]["reason"]
    assert settled[0]["failure_status"] == 409
    assert not _dials(transport)


# --------------------------------------------------------------------------
# Missed, interrupted, unschedulable
# --------------------------------------------------------------------------

async def test_a_schedule_missed_by_more_than_the_grace_window_is_not_placed(
        make_app, config, monkeypatch):
    """A Call long past its time is not one anyone wants placed by surprise. It
    fails, saying how late it was, and is not retried.

    The grace window is set to a second so the lateness is real elapsed time
    rather than a due_at edited on disk to say so.
    """
    monkeypatch.setenv("VOICE_SCHEDULE_GRACE_S", "1")
    transport = bridge_transport()
    application = make_app(transport)
    client = TestClient(application)
    assert _create(client, at=_at(-100)).status_code == 201

    settled = await _scheduler(application).tick()
    assert settled[0]["status"] == schedules.STATUS_FAILED
    assert "grace" in settled[0]["reason"]
    assert not _dials(transport)
    assert await _scheduler(application).tick() == []


async def test_a_claim_with_no_outcome_is_settled_failed_not_re_dialled(
        make_app, config, monkeypatch):
    """The app died holding the claim. Nobody can know from here whether Twilio
    got that dial, so the honest ending is a failure that says so — and NOT a
    second attempt, which is the one thing that could double-call someone."""
    import os

    monkeypatch.setenv("VOICE_SCHEDULE_STALE_CLAIM_S", "120")
    transport = bridge_transport()
    application = make_app(transport)
    client = TestClient(application)
    created = _create(client).json()
    assert schedules.claim(created["id"], schedules.INTENT_FIRE) is True
    old = schedules.now_utc().timestamp() - 3600
    os.utime(schedules.claim_path(created["id"]), (old, old))

    settled = await _scheduler(application).tick()
    assert settled[0]["status"] == schedules.STATUS_FAILED
    assert "not known" in settled[0]["reason"]
    assert not _dials(transport)


async def test_a_fresh_claim_is_left_alone(make_app, config, monkeypatch):
    """A claim younger than the stale window belongs to a worker that may still
    be dialling. Taking it away would settle a Schedule that is ringing."""
    monkeypatch.setenv("VOICE_SCHEDULE_STALE_CLAIM_S", "120")
    transport = bridge_transport()
    application = make_app(transport)
    client = TestClient(application)
    created = _create(client).json()
    assert schedules.claim(created["id"], schedules.INTENT_FIRE) is True

    assert await _scheduler(application).tick() == []
    assert schedules.load(created["id"])["status"] == schedules.STATUS_PENDING
    assert not _dials(transport)


async def test_a_schedule_with_an_unreadable_time_is_settled_not_left_upcoming(
        make_app, config):
    transport = bridge_transport()
    application = make_app(transport)
    client = TestClient(application)
    created = _create(client, at=_at(600)).json()
    path = config / "schedules" / f"{created['id']}.yaml"
    path.write_text(yaml.safe_dump(dict(created, due_at="whenever")))

    settled = await _scheduler(application).tick()
    assert settled[0]["status"] == schedules.STATUS_FAILED
    assert "never come due" in settled[0]["reason"]
    assert not _dials(transport)


# --------------------------------------------------------------------------
# Cancelling, and the race at the due instant
# --------------------------------------------------------------------------

async def test_cancelling_before_the_time_stops_the_call(make_app, config):
    transport = bridge_transport()
    application = make_app(transport)
    client = TestClient(application)
    created = _create(client, at=_at(-10)).json()

    cancelled = client.delete(f"/api/schedules/{created['id']}")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == schedules.STATUS_CANCELLED

    assert await _scheduler(application).tick() == []
    assert not _dials(transport)
    assert schedules.load(created["id"])["status"] == schedules.STATUS_CANCELLED


async def test_cancelling_loses_to_a_fire_that_already_started(make_app, config):
    """The race at the due instant, decided by the claim rather than by luck.

    The fire has taken the claim (it is mid-dial). Cancelling must NOT answer
    200: the phone is about to ring, and a 200 would tell the owner it had been
    stopped. It answers 409 and the record says what really happened.
    """
    transport = bridge_transport()
    application = make_app(transport)
    client = TestClient(application)
    created = _create(client).json()
    assert schedules.claim(created["id"], schedules.INTENT_FIRE) is True

    refused = client.delete(f"/api/schedules/{created['id']}")
    assert refused.status_code == 409
    assert "already being placed" in refused.text
    assert schedules.load(created["id"])["status"] == schedules.STATUS_PENDING


async def test_a_fire_loses_to_a_cancel_that_already_started(make_app, config):
    """The other side of the same race: the cancel took the claim first, so the
    tick that arrives a moment later does not dial."""
    transport = bridge_transport()
    application = make_app(transport)
    client = TestClient(application)
    created = _create(client).json()
    assert schedules.claim(created["id"], schedules.INTENT_CANCEL) is True

    assert await _scheduler(application).tick() == []
    assert not _dials(transport)


async def test_cancelling_something_already_settled_is_refused(make_app, config):
    transport = bridge_transport()
    application = make_app(transport)
    client = TestClient(application)
    created = _create(client).json()
    await _scheduler(application).tick()

    refused = client.delete(f"/api/schedules/{created['id']}")
    assert refused.status_code == 409
    assert "already placed" in refused.text


async def test_cancelling_something_that_does_not_exist_is_a_404(make_app, config):
    client = TestClient(make_app(SentinelTransport()))
    assert client.delete("/api/schedules/sch-000000000000").status_code == 404
    assert client.delete("/api/schedules/../../etc/passwd").status_code in (404, 405)


# --------------------------------------------------------------------------
# The loop itself, on a real clock
# --------------------------------------------------------------------------

def test_the_running_app_places_the_call_at_the_appointed_time(make_app, config):
    """No fake clock, no hand-driven tick: the app starts, a Schedule comes due
    a moment later, and the phone bridge is dialled by the loop itself."""
    transport = bridge_transport()
    application = make_app(transport)
    with TestClient(application) as client:      # runs the app's lifespan
        assert _create(client, at=_at(0.5)).status_code == 201
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not _dials(transport):
            time.sleep(0.05)
        assert len(_dials(transport)) == 1, "the scheduler never placed the call"
        listed = client.get("/api/schedules").json()["schedules"]
        assert listed[0]["status"] == schedules.STATUS_PLACED
        assert listed[0]["call_id"] == "cid-agent-a"


def test_a_schedule_already_due_when_the_app_starts_fires_once(make_app, config):
    """The third restart case the ticket names: due DURING startup.

    The Schedule is written by one app instance with no scheduler running, and
    is already due when a second instance boots. Starting the app must place it
    exactly once — startup is not a special path, it is the first tick.
    """
    transport = bridge_transport()
    writer = TestClient(make_app(transport))          # no lifespan: nothing fires
    assert _create(writer, at=_at(-20)).status_code == 201
    assert not _dials(transport)

    booted = make_app(transport)
    with TestClient(booted) as client:                # lifespan: the loop starts
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not _dials(transport):
            time.sleep(0.05)
        assert len(_dials(transport)) == 1
        assert client.get("/api/schedules").json()["schedules"][0]["status"] == \
            schedules.STATUS_PLACED

    # And booting again does not place it a second time.
    with TestClient(make_app(transport)):
        time.sleep(0.5)
    assert len(_dials(transport)) == 1


def test_a_graceful_stop_waits_for_a_dial_in_flight(make_app, config):
    """Stopping the app mid-dial finishes the call and records it.

    ``stop()`` shields the in-flight tick rather than cancelling it, because a
    cancelled dial is exactly how a claim ends up with no outcome — the one
    case nobody can settle honestly afterwards. Sabotage: cancel the task
    instead of awaiting it and the Schedule is left pending with its claim
    taken, which the deploy note describes as the interrupted-claim path.

    (On the NAS, Docker's SIGKILL arrives 10s after SIGTERM and the dial
    timeout is 30s, so a slow dial can still be killed. That lands in the
    interrupted-claim path, never in a second ring. The deploy note says so.)
    """
    import asyncio

    async def handler(request):
        await asyncio.sleep(0.4)
        return httpx.Response(200, json={
            "placed": True, "call_sid": "CAslow", "call_id": "cid-slow"})

    transport = RecordingTransport(handler)
    application = make_app(transport)
    client = TestClient(application)
    created = _create(client).json()

    async def run():
        instance = _scheduler(application)
        await instance.start()
        await asyncio.sleep(0.15)          # the dial is in flight
        await instance.stop()              # graceful stop, mid-dial

    asyncio.run(run())

    settled = schedules.load(created["id"])
    assert settled["status"] == schedules.STATUS_PLACED, settled
    assert settled["call_id"] == "cid-slow"
    assert len(_dials(transport)) == 1


def test_the_scheduler_can_be_switched_off_without_a_rebuild(
        make_app, config, monkeypatch):
    monkeypatch.setenv("VOICE_SCHEDULER_ENABLED", "false")
    transport = bridge_transport()
    application = make_app(transport)
    with TestClient(application) as client:
        assert _create(client, at=_at(-1)).status_code == 201
        time.sleep(0.5)
        assert not _dials(transport)
        assert client.get("/api/schedules").json()["schedules"][0]["status"] == \
            schedules.STATUS_PENDING


# --------------------------------------------------------------------------
# Knobs that cannot work are refused, not quietly defaulted
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["VOICE_SCHEDULE_GRACE_S",
                                  "VOICE_SCHEDULE_STALE_CLAIM_S",
                                  "VOICE_SCHEDULE_TICK_S"])
@pytest.mark.parametrize("value", ["0", "0.0", "-30", "five minutes", "300s"])
def test_a_window_that_cannot_work_refuses_the_boot(make_app, config,
                                                    monkeypatch, name, value):
    """An operator who sets a window to zero gets a system that reads as
    configured and never places a Call. It refuses to start instead, naming the
    variable and the value it was given — the way a missing required value is
    refused.

    A silent fallback to the default is the same lie one step quieter, so a
    value that is not a number is refused too rather than ignored.
    """
    monkeypatch.setenv(name, value)
    with pytest.raises(schedules.ScheduleError) as caught:
        with TestClient(make_app(SentinelTransport())):
            pass
    message = "; ".join(caught.value.detail)
    assert name in message
    assert value in message


@pytest.mark.parametrize("name,value", [
    ("VOICE_SCHEDULE_GRACE_S", "600"),
    ("VOICE_SCHEDULE_STALE_CLAIM_S", "90"),
    ("VOICE_SCHEDULE_TICK_S", "2.5"),
])
def test_a_usable_window_boots(make_app, config, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with TestClient(make_app(SentinelTransport())) as client:
        assert client.get("/healthz").status_code == 200


def test_the_refusal_happens_even_with_the_scheduler_switched_off(
        make_app, config, monkeypatch):
    """A knob that will be honoured the moment somebody flips the scheduler
    back on has to be right before then, not after the first missed Call."""
    monkeypatch.setenv("VOICE_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("VOICE_SCHEDULE_GRACE_S", "0")
    with pytest.raises(schedules.ScheduleError):
        with TestClient(make_app(SentinelTransport())):
            pass


def test_the_knobs_the_loop_reads_are_the_ones_the_boot_check_read(config,
                                                                   monkeypatch):
    """One parser, so the startup check and the running loop cannot disagree
    about what a knob means."""
    monkeypatch.setenv("VOICE_SCHEDULE_GRACE_S", "42")
    monkeypatch.setenv("VOICE_SCHEDULE_STALE_CLAIM_S", "77")
    monkeypatch.setenv("VOICE_SCHEDULE_TICK_S", "3")
    call_scheduler.validate_knobs()
    assert schedules.grace_s() == 42.0
    assert schedules.stale_claim_s() == 77.0
    assert call_scheduler.max_sleep_s() == 3.0


def test_unset_knobs_are_the_documented_defaults(config):
    assert schedules.grace_s() == schedules.DEFAULT_GRACE_S == 300.0
    assert schedules.stale_claim_s() == schedules.DEFAULT_STALE_CLAIM_S == 120.0
    assert call_scheduler.max_sleep_s() == call_scheduler.DEFAULT_MAX_SLEEP_S == 15.0
    call_scheduler.validate_knobs()
