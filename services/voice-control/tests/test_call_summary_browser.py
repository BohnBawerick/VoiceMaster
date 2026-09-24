"""Ticket 06: the Summary column and the Summary block, opened in a real browser.

`test_call_summary_api` proves the API relays which absence a call has. This module
proves the SCREEN says it, because the whole of this ticket's third bar is about what a
reader can tell apart by looking - and every defect the earlier Calls-screen rounds found
was visible by opening a page and invisible to the API tests.

The habits those rounds left behind are kept:

* **assert the cell, not the row** - "Not retained" somewhere in a ten-column row stays
  true while the Summary cell alone says the wrong thing;
* **assert the property, not the last defect's wording** - the bar is that the three
  absences are DISTINGUISHABLE and that none of them reads like a summary or like
  something still on its way, not that any of them uses a particular sentence.
"""
import pytest

import calls_page as cp

from browser_harness import body_text as _body_text
from hindsight_producer_fixtures import (
    summary_never_asked_v5,
    summary_nothing_to_say_v5,
    summary_unavailable_v5,
    summary_written_v5,
)

pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed (requirements-dev.txt)"
)

WRITTEN = summary_written_v5("voice-twilio-sum-written", "2026-08-18T09:00:00+00:00")
NOTHING = summary_nothing_to_say_v5("voice-twilio-sum-nothing", "2026-08-18T10:00:00+00:00")
BROKEN = summary_unavailable_v5("voice-twilio-sum-broken", "2026-08-18T11:00:00+00:00")
UNASKED = summary_never_asked_v5("voice-twilio-sum-unasked", "2026-08-18T12:00:00+00:00")

CORPUS = [WRITTEN, NOTHING, BROKEN, UNASKED]
ABSENCES = ["voice-twilio-sum-nothing", "voice-twilio-sum-broken",
            "voice-twilio-sum-unasked"]


def _summary_cell(page, call_id):
    return cp.field(page, call_id, "summary")


def _open(stack, page, docs=None):
    running = stack({"voice": docs if docs is not None else CORPUS, "hermes": []})
    page.goto(running.base, wait_until="networkidle")
    cp.wait_for_rows(page)
    return running


def test_the_summary_the_agent_wrote_is_on_the_screen(stack, page):
    """The positive control: without it every "absence" assertion below is vacuous."""
    _open(stack, page)
    assert "Chased the Tuesday delivery" in _summary_cell(page,
                                                          "voice-twilio-sum-written")


def test_the_three_absences_read_as_three_different_things(stack, page):
    """The bar. A reader must be able to tell them apart by looking at the cell."""
    _open(stack, page)
    wordings = [_summary_cell(page, call_id) for call_id in ABSENCES]
    assert all(wordings), "an absent summary rendered as an empty cell"
    assert len(set(wordings)) == 3, f"these absences look the same: {wordings}"


def test_no_absence_reads_like_a_summary_of_the_call(stack, page):
    """None of the three may be mistakable for something the Agent wrote.

    They are rendered in the screen's absence style, the same one every unrecorded field
    uses, so they cannot be read as the call's own words.
    """
    _open(stack, page)
    for call_id in ABSENCES:
        cell = cp.row(page, call_id).query_selector('[data-field="summary"]')
        assert cell.query_selector(".meta-item-absent") is not None, call_id
        assert "delivery" not in cell.inner_text()


def test_nothing_on_the_screen_says_a_summary_is_still_coming(stack, page):
    """There is no pending state to render, so there must be no spinner to wait on.

    A summary is settled before the call's document is written, so a row on this screen
    is a row whose summary question is closed. A "still being written" cell would be
    waiting for something that is never going to arrive.
    """
    _open(stack, page)
    for call_id in ABSENCES:
        wording = _summary_cell(page, call_id).lower()
        for pending in ("pending", "loading", "coming", "in progress", "being written",
                        "…", "..."):
            assert pending not in wording, f"{call_id} claims a summary is on its way"


def test_the_detail_view_says_which_absence_this_call_has(stack, page):
    _open(stack, page)
    cp.open_call(page, "voice-twilio-sum-nothing")
    absent = page.query_selector("[data-testid='summary-detail-absent']")
    assert absent is not None, "the detail view showed no Summary block at all"
    assert absent.inner_text().strip()
    assert page.query_selector("[data-testid='summary-detail']") is None


def test_the_detail_view_shows_a_written_summary_in_full(stack, page):
    """The list cuts a long summary to one line; the detail view shows the whole one."""
    _open(stack, page)
    cp.open_call(page, "voice-twilio-sum-written")
    text = _body_text(page)
    assert ("Chased the Tuesday delivery; it shipped Monday and lands tomorrow."
            in text)
    assert page.query_selector("[data-testid='summary-detail-absent']") is None
    # ...and the rest of the call is still on the page (ticket 05 / 07 are untouched).
    assert "hermes-main" in text
    assert "Book a table for 7pm." in text
