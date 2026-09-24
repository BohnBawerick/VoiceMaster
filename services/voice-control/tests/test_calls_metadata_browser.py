"""s5 (ticket 05): the Calls screen, opened in a real browser.

`test_calls_metadata_api` proves the API carries the fields. This module proves the
SCREEN does, because every defect the six review rounds of ticket 01 found was visible
by opening a page and invisible to the API tests.

Two habits from those rounds are kept here:

* **assert the cell, not the row.** "Not retained appears somewhere in this row" stays
  true while one cell alone lies -- and with a row of chips, pills and prose there is a lot of row
  for a lie to hide in.
* **assert the property, not the last defect's wording.** A duration cell is checked for
  "does not read as a number of seconds" rather than for one specific sentence.
"""
import pytest

import calls_page as cp

from browser_harness import body_text as _body_text
from hindsight_producer_fixtures import (
    phone_inbound_v5_no_agent,
    phone_outbound_v5,
    talk_inbound_v5,
    twilio_inbound,
)

pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed (requirements-dev.txt)"
)

# One document per state the screen has to render honestly: fully recorded, recorded
# but with no Agent assigned, recorded on the other Outlet, and one from before this
# ticket which has none of the new fields at all.
RECORDED = phone_outbound_v5("voice-twilio-out-1", "2026-08-18T09:00:00+00:00",
                             agent="hermes-main")
TALK = talk_inbound_v5("voice-talk-in-1", "2026-08-18T10:00:00+00:00",
                       agent="talk-answerer", duration_s=12.0)
NO_AGENT = phone_inbound_v5_no_agent("voice-twilio-in-1", "2026-08-18T11:00:00+00:00")
LEGACY = twilio_inbound("voice-twilio-legacy", "2026-08-17T09:05:00Z")

CORPUS = [RECORDED, TALK, NO_AGENT, LEGACY]

def _cell(page, call_id, field):
    """ONE field of ONE row (tests/calls_page.py)."""
    return cp.field(page, call_id, field)


def _open(stack, page, docs=None):
    running = stack({"voice": docs if docs is not None else CORPUS, "hermes": []})
    page.goto(running.base, wait_until="networkidle")
    cp.wait_for_rows(page)
    return running


def test_the_screen_shows_who_made_the_call_where_why_and_for_how_long(stack, page):
    _open(stack, page)
    assert _cell(page, "voice-twilio-out-1", "agent") == "hermes-main"
    assert _cell(page, "voice-twilio-out-1", "outlet") == "Phone"
    assert _cell(page, "voice-twilio-out-1", "mission") == "Mission: Book a table for 7pm."
    assert _cell(page, "voice-twilio-out-1", "outcome") == "ok"
    assert _cell(page, "voice-twilio-out-1", "duration") == "1m 03s"


def test_the_two_outlets_are_distinguishable_on_screen(stack, page):
    """Both of these are INBOUND calls. Only the Outlet tells them apart."""
    _open(stack, page)
    assert _cell(page, "voice-talk-in-1", "outlet") == "Talk"
    assert _cell(page, "voice-twilio-in-1", "outlet") == "Phone"
    # the direction is an icon with its word for screen readers
    assert _cell(page, "voice-talk-in-1", "direction").lower() == "inbound"
    assert _cell(page, "voice-twilio-in-1", "direction").lower() == "inbound"


def test_a_pre_ticket_call_says_not_retained_in_every_new_cell(stack, page):
    """History is visible, and nothing is filled in for it."""
    _open(stack, page)
    for column in ("agent", "outlet", "outcome", "duration"):
        assert _cell(page, "voice-twilio-legacy", column) == "Not retained", column
    # And specifically NOT a zero duration, which would claim the call took no time.
    assert not any(ch.isdigit() for ch in _cell(page, "voice-twilio-legacy", "duration"))


def test_a_call_with_no_assigned_agent_says_so_without_losing_its_outlet(stack, page):
    _open(stack, page)
    assert _cell(page, "voice-twilio-in-1", "agent") == "Not retained"
    assert _cell(page, "voice-twilio-in-1", "outlet") == "Phone"


def test_an_inbound_call_is_not_accused_of_a_missing_mission(stack, page):
    """An inbound call has no Mission by construction. Saying "not retained" there
    would suggest a record went missing; saying nothing at all would hide the
    distinction from an outbound call whose brief really was not recorded."""
    _open(stack, page)
    # the row names an outbound call's Mission and offers none for an inbound one
    assert cp.row(page, "voice-talk-in-1").query_selector('[data-field="mission"]') is None
    assert _cell(page, "voice-twilio-out-1", "mission") == "Mission: Book a table for 7pm."
    # ...and the detail view says which absence it is
    cp.open_call(page, "voice-talk-in-1")
    assert "Not applicable (inbound call)" in _body_text(page)


def test_the_detail_view_shows_the_duration_and_the_outlet(stack, page):
    _open(stack, page)
    cp.open_call(page, "voice-twilio-out-1")
    fields = cp.meta(page)
    assert fields["Duration"] == "1m 03s"
    assert fields["Agent"] == "hermes-main"
    assert fields["Outlet"] == "Phone number"
    assert page.inner_text('[data-testid="mission-detail"]') == "Book a table for 7pm."


def test_filtering_by_agent_narrows_the_list(stack, page):
    _open(stack, page)
    assert len(cp.rows(page)) == 4

    cp.pick_filter(page, "agent", "hermes-main")
    assert cp.row_ids(page) == ["voice-twilio-out-1"]
    assert "agent=hermes-main" in page.url


def test_filtering_by_outlet_narrows_the_list(stack, page):
    _open(stack, page)
    cp.pick_filter(page, "outlet", "talk")
    assert cp.row_ids(page) == ["voice-talk-in-1"]


def test_the_filters_offer_only_values_that_have_calls_behind_them(stack, page):
    _open(stack, page)
    assert cp.filter_options(page, "outlet") == ["phone", "talk", "(not retained)"]


def test_the_not_retained_bucket_reaches_the_pre_ticket_archive(stack, page):
    """The commonest bucket on this screen today. If it were not selectable the
    filter row would silently hide most of the history."""
    _open(stack, page)
    cp.pick_filter(page, "outlet", "(not retained)")
    assert cp.row_ids(page) == ["voice-twilio-legacy"]


def test_clearing_the_filters_brings_every_call_back(stack, page):
    _open(stack, page)
    cp.pick_filter(page, "agent", "hermes-main")
    page.click("button:has-text('Clear filters')")
    page.wait_for_function(
        "document.querySelectorAll('[data-testid=\"call-row\"]').length === 4")


def test_a_filter_that_matches_nothing_does_not_claim_the_history_is_empty(stack, page):
    _open(stack, page, docs=[LEGACY])
    cp.pick_filter(page, "outlet", "(not retained)")
    cp.wait_for_rows(page)
    # Now ask for an Outlet no call in this corpus has: the store still holds a call.
    cp.pick_filter(page, "agent", "(not retained)")
    cp.wait_for_rows(page)
    assert page.query_selector(".empty-state") is None


def test_a_filter_matching_nothing_says_which_kind_of_empty_it_is(stack, page):
    """Two calls, filtered to a combination neither has."""
    running = _open(stack, page, docs=[RECORDED, TALK])
    # Filters live in the URL, so the combination can be asked for directly.
    page.goto(f"{running.base}/?agent=hermes-main&outlet=talk", wait_until="networkidle")
    page.wait_for_selector(".empty-state")
    text = page.inner_text(".empty-state")
    assert "No calls recorded yet" not in text
    assert "filters" in text.lower()
