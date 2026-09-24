"""Ticket 11: a Schedule set for 3pm rings at 3pm local, daylight saving included.

**These tests deliberately do not lean on a zone without daylight saving.** In a zone
like UTC, every one of the assertions below would pass against an implementation that
stored a naive local time and resolved it at fire time - the exact bug this ticket
names. New York and London are used instead, on both sides of the year: the
spring-forward gap (a local time that does not exist) and the autumn-back overlap (one
that happens twice). UTC appears once, to show what it cannot tell you.

The property under test is the one that matters at 3pm: **the stored instant is
the instant that zone's clocks read that wall time**, computed once at creation.
Each expected instant here is written out by hand from the transition rules, not
taken from the code under test.
"""
from datetime import datetime, timedelta, timezone

import pytest

import schedules

NEW_YORK = "America/New_York"
LONDON = "Europe/London"
UTC = "UTC"


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


# -- ordinary times, both sides of a transition ------------------------------

@pytest.mark.parametrize("zone,local,expected_utc,offset", [
    # New York: EDT (-4) in September, EST (-5) from the first Sunday in November.
    (NEW_YORK, "2026-09-20T15:00:00", "2026-09-20T19:00:00Z", "-04:00"),
    (NEW_YORK, "2026-11-20T15:00:00", "2026-11-20T20:00:00Z", "-05:00"),
    # London: GMT in February, BST (+1) in May.
    (LONDON, "2026-02-10T15:00:00", "2026-02-10T15:00:00Z", "+00:00"),
    (LONDON, "2026-05-10T15:00:00", "2026-05-10T14:00:00Z", "+01:00"),
    # UTC: the same offset all year, and the reason a zone like it is not enough.
    (UTC, "2026-09-20T15:00:00", "2026-09-20T15:00:00Z", "+00:00"),
    (UTC, "2026-12-20T15:00:00", "2026-12-20T15:00:00Z", "+00:00"),
])
def test_3pm_local_is_the_instant_that_zone_reads_3pm(zone, local, expected_utc, offset):
    resolved = schedules.resolve_due(local, zone)
    assert _instant(resolved["due_at"]) == _instant(expected_utc)
    assert resolved["timezone"] == zone
    assert resolved["local_time"] == local
    assert resolved["utc_offset"] == offset
    assert "ambiguous_local_time" not in resolved


def test_the_same_wall_clock_is_a_different_instant_either_side_of_dst():
    """Two Schedules for 3pm New York, two months apart, are an hour apart in UTC
    relative to their dates. A naive local time stored and resolved later cannot
    express this; an instant can, and does."""
    before = _instant(schedules.resolve_due("2026-09-20T15:00:00", NEW_YORK)["due_at"])
    after = _instant(schedules.resolve_due("2026-11-20T15:00:00", NEW_YORK)["due_at"])
    assert before.hour == 19 and after.hour == 20
    # The same wall clock in UTC is the same offset all year - which is why a zone
    # without daylight saving proves nothing here.
    utc_before = _instant(schedules.resolve_due("2026-09-20T15:00:00", UTC)["due_at"])
    utc_after = _instant(schedules.resolve_due("2026-11-20T15:00:00", UTC)["due_at"])
    assert utc_before.hour == utc_after.hour == 15


# -- the spring-forward gap: a local time that does not exist ----------------

@pytest.mark.parametrize("zone,local", [
    (NEW_YORK, "2026-03-08T02:30:00"),   # clocks go 02:00 -> 03:00
    (NEW_YORK, "2026-03-08T02:00:00"),   # the first instant of the gap
    (NEW_YORK, "2026-03-08T02:59:00"),   # the last
    (LONDON, "2026-03-29T01:30:00"),     # clocks go 01:00 -> 02:00
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
    (NEW_YORK, "2026-03-08T01:59:00"),
    (NEW_YORK, "2026-03-08T03:00:00"),
])
def test_the_times_either_side_of_the_gap_are_fine(zone, local):
    resolved = schedules.resolve_due(local, zone)
    assert resolved["local_time"] == local


def test_the_gap_refusal_says_where_the_clocks_go():
    with pytest.raises(schedules.ScheduleError) as caught:
        schedules.resolve_due("2026-03-08T02:30:00", NEW_YORK)
    message = "; ".join(caught.value.detail)
    assert "1:00" in message          # the size of the jump
    assert "03:30" in message         # where that local time would have landed


# -- the autumn-back overlap: a local time that happens twice ----------------

@pytest.mark.parametrize("zone,local,expected_utc,offset", [
    # New York 2026-11-01: 02:00 EDT -> 01:00 EST, so 01:30 happens at -4 then -5.
    (NEW_YORK, "2026-11-01T01:30:00", "2026-11-01T05:30:00Z", "-04:00"),
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
    second = schedules.resolve_due("2026-11-01T01:30:00-05:00", NEW_YORK)
    assert _instant(second["due_at"]) == _instant("2026-11-01T06:30:00Z")
    assert second["utc_offset"] == "-05:00"
    first = schedules.resolve_due("2026-11-01T01:30:00", NEW_YORK)
    assert _instant(second["due_at"]) - _instant(first["due_at"]) == timedelta(hours=1)


def test_an_ordinary_time_is_not_flagged_ambiguous():
    assert "ambiguous_local_time" not in schedules.resolve_due(
        "2026-11-01T15:00:00", NEW_YORK)


# -- instants, offsets and the zone that reads them --------------------------

def test_an_instant_with_an_offset_needs_no_zone():
    resolved = schedules.resolve_due("2026-08-20T15:00:00+02:00")
    assert _instant(resolved["due_at"]) == _instant("2026-08-20T13:00:00Z")
    assert resolved["timezone"] == "+02:00"
    assert resolved["local_time"] == "2026-08-20T15:00:00"


def test_a_zulu_instant_is_read_as_utc():
    resolved = schedules.resolve_due("2026-08-20T07:00:00Z")
    assert _instant(resolved["due_at"]) == _instant("2026-08-20T07:00:00Z")


def test_an_offset_that_contradicts_the_zone_is_refused():
    """+02:00 is not New York's offset in August. Storing either one silently
    would put the call hours from where the caller meant."""
    with pytest.raises(schedules.ScheduleError) as caught:
        schedules.resolve_due("2026-08-20T15:00:00+02:00", NEW_YORK)
    assert caught.value.status == 422
    assert "is not" in "; ".join(caught.value.detail)


def test_an_offset_that_agrees_with_the_zone_keeps_the_zone_name():
    resolved = schedules.resolve_due("2026-08-20T15:00:00-04:00", NEW_YORK)
    assert resolved["timezone"] == NEW_YORK
    assert resolved["local_time"] == "2026-08-20T15:00:00"


# -- refusals --------------------------------------------------------------

def test_an_unknown_zone_is_refused_by_name():
    with pytest.raises(schedules.ScheduleError) as caught:
        schedules.resolve_due("2026-08-20T15:00:00", "Mars/Olympus_Mons")
    assert caught.value.status == 422
    assert "Mars/Olympus_Mons" in "; ".join(caught.value.detail)


def test_a_naive_time_with_no_zone_and_no_default_is_refused():
    """Reading "3pm" as UTC would ring at 11am in New York. Better to ask."""
    with pytest.raises(schedules.ScheduleError) as caught:
        schedules.resolve_due("2026-08-20T15:00:00", None, env={})
    assert caught.value.status == 422
    assert "tz" in "; ".join(caught.value.detail)


def test_the_default_zone_comes_from_voice_timezone_then_tz():
    from_tz = schedules.resolve_due("2026-08-20T15:00:00", None,
                                    env={"TZ": NEW_YORK})
    assert from_tz["timezone"] == NEW_YORK
    assert _instant(from_tz["due_at"]) == _instant("2026-08-20T19:00:00Z")
    override = schedules.resolve_due("2026-08-20T15:00:00", None,
                                     env={"TZ": NEW_YORK, "VOICE_TIMEZONE": LONDON})
    assert override["timezone"] == LONDON
    assert _instant(override["due_at"]) == _instant("2026-08-20T14:00:00Z")


@pytest.mark.parametrize("bad", ["", "   ", "tuesday at 3", "2026-13-40T99:00",
                                 None, 15, "3pm"])
def test_a_time_that_is_not_a_time_is_refused(bad):
    with pytest.raises(schedules.ScheduleError) as caught:
        schedules.resolve_due(bad, NEW_YORK)
    assert caught.value.status == 422


def test_seconds_are_kept_and_microseconds_are_not():
    resolved = schedules.resolve_due("2026-08-20T15:00:45.123456", NEW_YORK)
    assert resolved["local_time"] == "2026-08-20T15:00:45"
    assert resolved["due_at"] == "2026-08-20T19:00:45Z"
