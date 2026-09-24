"""Ticket 11: the Schedule records, and the one claim that makes firing happen once.

The claim is the whole concurrency design, so it is tested as a primitive here and
as behaviour in ``test_scheduler_fire.py``. The important test in this file is
``test_only_one_of_many_processes_can_claim``: it runs REAL processes, because an
in-process lock (a set, a module global, an asyncio.Lock) would satisfy every
other test in the suite while two uvicorn workers both placed the call.
"""
import multiprocessing
import os
from datetime import timedelta

import pytest
import yaml

import place_call
import schedules

PERTH = "Australia/Perth"
OWNER = "+61491570156"


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _request(agent="agent-a", to=OWNER, mission="Ask about Friday.", disclose=False):
    return place_call.PlaceRequest(agent=agent, to=to, mission=mission,
                                   disclose=disclose)


def _make(at="2026-08-20T15:00:00", tz=PERTH, **kw):
    return schedules.create(_request(**kw), schedules.resolve_due(at, tz))


# -- records ---------------------------------------------------------------

def test_a_schedule_is_a_call_that_has_not_happened_yet(config):
    record = _make(mission="Ask if Friday still works.", disclose=True)
    assert schedules.is_schedule_id(record["id"])
    assert record["status"] == schedules.STATUS_PENDING
    assert record["agent"] == "agent-a"
    assert record["to"] == OWNER
    assert record["mission"] == "Ask if Friday still works."
    assert record["disclose"] is True
    assert record["due_at"] == "2026-08-20T07:00:00Z"
    assert record["timezone"] == PERTH
    assert record["local_time"] == "2026-08-20T15:00:00"

    on_disk = yaml.safe_load(
        (config / "schedules" / f"{record['id']}.yaml").read_text())
    assert on_disk == record


def test_a_schedule_survives_the_process_that_wrote_it(config):
    """The point of the file: nothing about a Schedule lives in memory."""
    record = _make()
    assert schedules.load(record["id"]) == record
    assert schedules.load_all() == [record]


def test_an_absent_directory_is_an_empty_list_not_an_error(config):
    assert schedules.load_all() == []
    assert schedules.load("sch-000000000000") is None


def test_an_unreadable_record_is_skipped_not_fatal(config):
    good = _make()
    (config / "schedules" / "sch-ffffffffffff.yaml").write_text("{{{ not yaml")
    (config / "schedules" / "sch-eeeeeeeeeeee.yaml").write_text("- a list\n")
    assert [r["id"] for r in schedules.load_all()] == [good["id"]]


def test_a_record_whose_id_does_not_match_its_filename_is_ignored(config):
    """The id inside the document is the id — a file named after another one is
    two Schedules disagreeing, and neither is trustworthy."""
    record = _make()
    (config / "schedules" / "sch-aaaaaaaaaaaa.yaml").write_text(
        yaml.safe_dump(dict(record, id="sch-bbbbbbbbbbbb")))
    assert [r["id"] for r in schedules.load_all()] == [record["id"]]


def test_upcoming_are_soonest_first_and_settled_come_after(config):
    late = _make(at="2026-08-20T17:00:00")
    early = _make(at="2026-08-20T15:00:00")
    done = _make(at="2026-08-19T09:00:00")
    schedules.settle(done["id"], schedules.STATUS_PLACED, call_id="cid-1")
    assert [r["id"] for r in schedules.load_all()] == [early["id"], late["id"],
                                                       done["id"]]


def test_an_id_from_outside_is_never_a_path(config):
    _make()
    for hostile in ("../../etc/passwd", "sch-../../x", "", None, "sch-XYZ",
                    "sch-0123456789012", "agent-a"):
        assert schedules.load(hostile) is None
        assert schedules.is_schedule_id(hostile) is False


# -- settling --------------------------------------------------------------

def test_settle_writes_the_outcome_and_the_time(config):
    record = _make()
    settled = schedules.settle(record["id"], schedules.STATUS_PLACED,
                               call_id="cid-9", call_sid="CA9")
    assert settled["status"] == schedules.STATUS_PLACED
    assert settled["call_id"] == "cid-9"
    assert settled["call_sid"] == "CA9"
    assert settled["settled_at"].endswith("Z")
    assert schedules.load(record["id"]) == settled


def test_the_first_ending_wins(config):
    """A sweep that runs while a fire is finishing must not overwrite what the
    fire recorded. Whoever settles first is what happened."""
    record = _make()
    schedules.settle(record["id"], schedules.STATUS_PLACED, call_id="cid-9")
    again = schedules.settle(record["id"], schedules.STATUS_FAILED,
                             reason="a later sweep decided otherwise")
    assert again["status"] == schedules.STATUS_PLACED
    assert "reason" not in again
    assert schedules.load(record["id"])["call_id"] == "cid-9"


def test_settling_something_that_is_not_there_is_none(config):
    assert schedules.settle("sch-000000000000", schedules.STATUS_FAILED) is None


# -- the claim -------------------------------------------------------------

def test_a_schedule_can_be_claimed_exactly_once(config):
    record = _make()
    assert schedules.claim(record["id"], schedules.INTENT_FIRE) is True
    assert schedules.claim(record["id"], schedules.INTENT_FIRE) is False
    assert schedules.claim(record["id"], schedules.INTENT_CANCEL) is False


def test_the_claim_outlives_the_process(config):
    record = _make()
    assert schedules.claim(record["id"], schedules.INTENT_FIRE) is True
    assert schedules.claim_path(record["id"]).is_file()
    assert schedules.is_claimed(record["id"]) is True


def _claim_in_a_child(directory, schedule_id, barrier, answers):
    """Run in a SEPARATE process: claim, and say which process said so."""
    os.environ["VOICE_CONFIG_DIR"] = directory
    import schedules as fresh
    barrier.wait(timeout=30)          # everybody claims at the same moment
    answers.put((os.getpid(), fresh.claim(schedule_id, fresh.INTENT_FIRE)))


def test_only_one_of_many_processes_can_claim(config):
    """The two-workers case, with eight real workers claiming simultaneously.

    Nothing is shared between these processes except the directory, so this is
    red for any claim that is really a lock inside one interpreter — the
    shortcut that would survive every other test here and then let two uvicorn
    workers place the same call. The barrier and the PID assertion are what
    keep it honest: without them a pool that happened to run every attempt in
    ONE process would pass this while proving nothing.
    """
    record = _make()
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(8)
    answers = context.Queue()
    workers = [context.Process(target=_claim_in_a_child,
                               args=(str(config), record["id"], barrier, answers))
               for _ in range(8)]
    for worker in workers:
        worker.start()
    outcomes = [answers.get(timeout=30) for _ in workers]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0

    assert len({pid for pid, _ in outcomes}) == 8, "these were not 8 processes"
    assert sum(1 for _, won in outcomes if won) == 1, outcomes


# -- what the loop asks ----------------------------------------------------

def _at(record, seconds):
    return schedules.due_at(record) + timedelta(seconds=seconds)


def test_due_now_is_the_window_from_the_due_time_to_the_end_of_grace(config, monkeypatch):
    monkeypatch.setenv("VOICE_SCHEDULE_GRACE_S", "300")
    record = _make()
    every = [record]
    assert schedules.due_now(every, _at(record, -1)) == []
    assert schedules.due_now(every, _at(record, 0)) == [record]
    assert schedules.due_now(every, _at(record, 299)) == [record]
    assert schedules.due_now(every, _at(record, 301)) == []


def test_missed_starts_where_due_now_stops(config, monkeypatch):
    monkeypatch.setenv("VOICE_SCHEDULE_GRACE_S", "300")
    record = _make()
    every = [record]
    assert schedules.missed(every, _at(record, 299)) == []
    assert schedules.missed(every, _at(record, 301)) == [record]


def test_the_grace_window_is_configurable(config, monkeypatch):
    record = _make()
    monkeypatch.setenv("VOICE_SCHEDULE_GRACE_S", "10")
    assert schedules.due_now([record], _at(record, 30)) == []
    assert schedules.missed([record], _at(record, 30)) == [record]
    monkeypatch.setenv("VOICE_SCHEDULE_GRACE_S", "600")
    assert schedules.due_now([record], _at(record, 30)) == [record]
    assert schedules.missed([record], _at(record, 30)) == []


def test_a_settled_schedule_is_never_due_again(config):
    record = _make()
    schedules.settle(record["id"], schedules.STATUS_PLACED, call_id="cid-1")
    every = schedules.load_all()
    assert schedules.due_now(every, _at(record, 1)) == []
    assert schedules.missed(every, _at(record, 100000)) == []
    assert schedules.next_due_at(every) is None


def test_a_schedule_with_no_readable_time_is_reported_not_ignored(config):
    record = _make()
    path = config / "schedules" / f"{record['id']}.yaml"
    path.write_text(yaml.safe_dump(dict(record, due_at="tuesday")))
    every = schedules.load_all()
    assert schedules.unresolvable(every) == every
    assert schedules.due_now(every, schedules.now_utc()) == []
    assert schedules.next_due_at(every) is None


def test_an_interrupted_claim_is_only_stale_after_the_window(config, monkeypatch):
    record = _make()
    schedules.claim(record["id"], schedules.INTENT_FIRE)
    monkeypatch.setenv("VOICE_SCHEDULE_STALE_CLAIM_S", "120")
    assert schedules.interrupted_claims(schedules.load_all()) == []
    # Age the claim: the file's mtime is the claim's age.
    path = schedules.claim_path(record["id"])
    old = schedules.now_utc().timestamp() - 3600
    os.utime(path, (old, old))
    assert [r["id"] for r in schedules.interrupted_claims(schedules.load_all())] == \
        [record["id"]]


def test_next_due_at_is_the_soonest_pending_one(config):
    late = _make(at="2026-08-20T17:00:00")
    early = _make(at="2026-08-20T15:00:00")
    assert schedules.next_due_at(schedules.load_all()) == schedules.due_at(early)
    schedules.settle(early["id"], schedules.STATUS_CANCELLED)
    assert schedules.next_due_at(schedules.load_all()) == schedules.due_at(late)
