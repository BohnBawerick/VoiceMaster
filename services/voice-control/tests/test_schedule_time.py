"""Ticket 11: a Schedule set for 3pm rings at 3pm local, daylight saving included.

**These tests deliberately do not run against the deployed zone.** The original deployment sets
TZ=Australia/Perth, which has never observed daylight saving, so every one of the
assertions below would pass against an implementation that stored a naive local
time and resolved it at fire time — the exact bug this ticket names. Sydney and
London are used instead, on both sides of the year: the spring-forward gap (a
local time that does not exist) and the autumn-back overlap (one that happens
twice). Perth appears once, to show what it cannot tell you.

The property under test is the one that matters at 3pm: **the stored instant is
the instant that zone's clocks read that wall time**, computed once at creation.
Each expected instant here is written out by hand from the transition rules, not
taken from the code under test.
"""
from datetime import datetime, timedelta, timezone

import pytest

import schedules

SYDNEY = "Australia/Sydney"
LONDON = "Europe/London"
PERTH = "Australia/Perth"


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


# -- ordinary times, both sides of a transition ------------------------------

@pytest.mark.parametrize("zone,local,expected_utc,offset", [
    # Sydney: AEST (+10) in September, AEDT (+11) from the first Sunday in October.
    (SYDNEY, "2026-09-20T15:00:00", "2026-09-20T05:00:00Z", "+10:00"),
    (SYDNEY, "2026-10-20T15:00:00", "2026-10-20T04:00:00Z", "+11:00"),
    # London: GMT in February, BST (+1) in May.
    (LONDON, "2026-02-10T15:00:00", "2026-02-10T15:00:00Z", "+00:00"),
    (LONDON, "2026-05-10T15:00:00", "2026-05-10T14:00:00Z", "+01:00"),
    # Perth: +8 all year. The deployed zone, and the reason it is not enough.
    (PERTH, "2026-09-20T15:00:00", "2026-09-20T07:00:00Z", "+08:00"),
    (PERTH, "2026-12-20T15:00:00", "2026-12-20T07:00:00Z", "+08:00"),
])
def test_3pm_local_is_the_instant_that_zone_reads_3pm(zone, local, expected_utc, offset):
    resolved = schedules.resolve_due(local, zone)
    assert _instant(resolved["due_at"]) == _instant(expected_utc)
    assert resolved["timezone"] == zone
    assert resolved["local_time"] == local
    assert resolved["utc_offset"] == offset
    assert "ambiguous_local_time" not in resolved


def test_the_same_wall_clock_is_a_different_instant_either_side_of_dst():
    """Two Schedules for 3pm Sydney, five weeks apart, are an hour apart in UTC
    relative to their dates. A naive local time stored and resolved later cannot
    express this; an instant can, and does."""
    before = _instant(schedules.resolve_due("2026-09-20T15:00:00", SYDNEY)["due_at"])
    after = _instant(schedules.resolve_due("2026-10-20T15:00:00", SYDNEY)["due_at"])
    assert before.hour == 5 and after.hour == 4
    # Same wall clock in Perth is the same offset all year — which is why the
    # deployed zone alone proves nothing here.
    perth_before = _instant(schedules.resolve_due("2026-09-20T15:00:00", PERTH)["due_at"])
    perth_after = _instant(schedules.resolve_due("2026-10-20T15:00:00", PERTH)["due_at"])
    assert perth_before.hour == perth_after.hour == 7


# -- the spring-forward gap: a local time that does not exist ----------------

@pytest.mark.parametrize("zone,local", [
    (SYDNEY, "2026-10-04T02:30:00"),   # clocks go 02:00 -> 03:00
    (SYDNEY, "2026-10-04T02:00:00"),   # the first instant of the gap
    (SYDNEY, "2026-10-04T02:59:00"),   # the last
    (LONDON, "2026-03-29T01:30:00"),   # clocks go 01:00 -> 02:00
])
def test_a_local_time_that_does_not_exist_is_refused(zone, local):
    """There is no instant to promise, so the Schedule is refused when it is
    written rather than silently sliding an hour when it fires."""
    with pytest.raises(schedules.ScheduleError) as caught:
        schedules.resolve_due(local, zone)
    assert caught.value.status == 422
    message = "; ".join(caught.value.detail)
    assert "does not exist" in message
    assert zone in message


@pytest.mark.parametrize("zone,local", [
    (SYDNEY, "2026-10-04T01:59:00"),
    (SYDNEY, "2026-10-04T03:00:00"),
])
def test_the_times_either_side_of_the_gap_are_fine(zone, local):
    resolved = schedules.resolve_due(local, zone)
    assert resolved["local_time"] == local


def test_the_gap_refusal_says_where_the_clocks_go():
    with pytest.raises(schedules.ScheduleError) as caught:
        schedules.resolve_due("2026-10-04T02:30:00", SYDNEY)
    message = "; ".join(caught.value.detail)
    assert "1:00" in message          # the size of the jump
    assert "03:30" in message         # where that local time would have landed


# -- the autumn-back overlap: a local time that happens twice ----------------

@pytest.mark.parametrize("zone,local,expected_utc,offset", [
    # Sydney 2026-04-05: 03:00 AEDT -> 02:00 AEST, so 02:30 happens at +11 then +10.
    (SYDNEY, "2026-04-05T02:30:00", "2026-04-04T15:30:00Z", "+11:00"),
    # London 2026-10-25: 02:00 BST -> 01:00 GMT, so 01:30 happens at +01 then +00.
    (LONDON, "2026-10-25T01:30:00", "2026-10-25T00:30:00Z", "+01:00"),
])
def test_a_local_time_that_happens_twice_takes_the_first_and_says_so(
        zone, local, expected_utc, offset):
    """Both instants are that wall clock. The earlier one is what the clock
    reads the first time it says it, and the record carries the flag so the
    screen can say which was taken."""
    resolved = schedules.resolve_due(local, zone)
    assert _instant(resolved["due_at"]) == _instant(expected_utc)
    assert resolved["utc_offset"] == offset
    assert resolved["ambiguous_local_time"] is True


def test_the_second_of_two_is_reachable_by_naming_the_offset():
    """An owner who means the repeat sends the instant, and gets it exactly."""
    second = schedules.resolve_due("2026-04-05T02:30:00+10:00", SYDNEY)
    assert _instant(second["due_at"]) == _instant("2026-04-04T16:30:00Z")
    assert second["utc_offset"] == "+10:00"
    first = schedules.resolve_due("2026-04-05T02:30:00", SYDNEY)
    assert _instant(second["due_at"]) - _instant(first["due_at"]) == timedelta(hours=1)


def test_an_ordinary_time_is_not_flagged_ambiguous():
    assert "ambiguous_local_time" not in schedules.resolve_due(
        "2026-04-05T15:00:00", SYDNEY)


# -- instants, offsets and the zone that reads them --------------------------

def test_an_instant_with_an_offset_needs_no_zone():
    resolved = schedules.resolve_due("2026-08-20T15:00:00+08:00")
    assert _instant(resolved["due_at"]) == _instant("2026-08-20T07:00:00Z")
    assert resolved["timezone"] == "+08:00"
    assert resolved["local_time"] == "2026-08-20T15:00:00"


def test_a_zulu_instant_is_read_as_utc():
    resolved = schedules.resolve_due("2026-08-20T07:00:00Z")
    assert _instant(resolved["due_at"]) == _instant("2026-08-20T07:00:00Z")


def test_an_offset_that_contradicts_the_zone_is_refused():
    """+08:00 is not Sydney's offset in August. Storing either one silently
    would put the call an hour or two from where the caller meant."""
    with pytest.raises(schedules.ScheduleError) as caught:
        schedules.resolve_due("2026-08-20T15:00:00+08:00", SYDNEY)
    assert caught.value.status == 422
    assert "is not" in "; ".join(caught.value.detail)


def test_an_offset_that_agrees_with_the_zone_keeps_the_zone_name():
    resolved = schedules.resolve_due("2026-08-20T15:00:00+10:00", SYDNEY)
    assert resolved["timezone"] == SYDNEY
    assert resolved["local_time"] == "2026-08-20T15:00:00"


# -- refusals --------------------------------------------------------------

def test_an_unknown_zone_is_refused_by_name():
    with pytest.raises(schedules.ScheduleError) as caught:
        schedules.resolve_due("2026-08-20T15:00:00", "Mars/Olympus_Mons")
    assert caught.value.status == 422
    assert "Mars/Olympus_Mons" in "; ".join(caught.value.detail)


def test_a_naive_time_with_no_zone_and_no_default_is_refused():
    """Reading "3pm" as UTC would ring at 11pm in Perth. Better to ask."""
    with pytest.raises(schedules.ScheduleError) as caught:
        schedules.resolve_due("2026-08-20T15:00:00", None, env={})
    assert caught.value.status == 422
    assert "tz" in "; ".join(caught.value.detail)


def test_the_default_zone_comes_from_voice_timezone_then_tz():
    perth = schedules.resolve_due("2026-08-20T15:00:00", None,
                                  env={"TZ": PERTH})
    assert perth["timezone"] == PERTH
    assert _instant(perth["due_at"]) == _instant("2026-08-20T07:00:00Z")
    override = schedules.resolve_due("2026-08-20T15:00:00", None,
                                     env={"TZ": PERTH, "VOICE_TIMEZONE": SYDNEY})
    assert override["timezone"] == SYDNEY
    assert _instant(override["due_at"]) == _instant("2026-08-20T05:00:00Z")


@pytest.mark.parametrize("bad", ["", "   ", "tuesday at 3", "2026-13-40T99:00",
                                 None, 15, "3pm"])
def test_a_time_that_is_not_a_time_is_refused(bad):
    with pytest.raises(schedules.ScheduleError) as caught:
        schedules.resolve_due(bad, PERTH)
    assert caught.value.status == 422


def test_seconds_are_kept_and_microseconds_are_not():
    resolved = schedules.resolve_due("2026-08-20T15:00:45.123456", PERTH)
    assert resolved["local_time"] == "2026-08-20T15:00:45"
    assert resolved["due_at"] == "2026-08-20T07:00:45Z"
