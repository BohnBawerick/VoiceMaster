"""The Calls screens, driven across the ``fixture_matrix`` cells.

``test_calls_browser`` covers one scenario per behaviour. This module covers the
same invariants across the matrix, because every defect in five review rounds
sat at an intersection of two dimensions that no single-scenario test visited:

    D3  React fake empty state    corpus size x an EXACT multiple of the page size
    D1  "every call is shown"     several pages x mixed call/non-call content
    D2  "Showing all N calls."    a truncated read x a total that fits one page

The invariants asserted here are the ones the whole ticket is about:

    * a store holding calls never renders an empty state, on any page;
    * no element ever claims completeness the read cannot support;
    * a bank that failed never discards the calls another bank returned;
    * an unretained field always reads "not retained", never 0 or a dash.
"""
import json
import re

import pytest

import calls_page as cp

from browser_harness import body_text
from fixture_matrix import (
    MATRIX,
    REACT_PAGE_SIZE,
    cell,
    hermes_memories,
    retained_calls,
)

pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed (requirements-dev.txt)"
)


# The sentence the React screen shows when it believes the owner has no history.
# Asserted as the behaviour "an empty state appeared", not as this wording: a
# reworded empty state on a store holding calls is the same defect.
def _react_empty_state(page):
    el = page.query_selector(".empty-state")
    return el.inner_text().replace("\n", " | ") if el and el.is_visible() else None


def _api(page, base, path):
    page.goto(f"{base}{path}")
    return json.loads(page.inner_text("body"))


# --------------------------------------------------------------------------
# The page-size constants this file's reasoning rests on.
# --------------------------------------------------------------------------


def test_the_matrix_page_size_still_matches_the_screen():
    """If the screen's page size changes, the matrix's "exact multiple" cell moves.

    D3 was invisible for five rounds because no corpus was an exact multiple of
    the React page size. That property is only meaningful while this constant
    agrees with the screen, so bind it to the source rather than to memory.

    Ticket 15 deleted the second half of this guard along with the legacy
    screen's own ``page_size=100`` request.
    """
    from pathlib import Path

    calls_tsx = (Path(__file__).resolve().parent.parent / "ui/src/CallsView.tsx").read_text()
    assert f"const PAGE_SIZE = {REACT_PAGE_SIZE};" in calls_tsx, (
        "the React page size changed; fixture_matrix's exact-multiple cell must move with it"
    )


# --------------------------------------------------------------------------
# D3: the React pager's terminal state, across the size axis.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "size",
    # 20, 40 and 100 are exact multiples of the React page size -- the cell that
    # had never been tested. 19/21/99/101 bracket them so a fix that merely
    # special-cases a multiple is not enough.
    [1, 19, 20, 21, 40, 99, 100, 101],
)
def test_react_pager_never_lands_on_a_fake_empty_state(stack, page, size):
    """Walk the React pager to the end and assert the empty state never appears.

    Before this test, a corpus that was an exact multiple of the page size left
    Next enabled on the last full page (``!hasMore && calls.length < PAGE_SIZE``
    is false when both halves are), and the page after it rendered "No calls
    recorded yet" against a store holding 100 calls.
    """
    running = stack({"voice": retained_calls(size), "hermes": []})
    page.goto(running.base, wait_until="networkidle")
    page.wait_for_selector(f"{cp.ROWS}, .empty-state")

    seen = set()
    pages_walked = 0
    while True:
        pages_walked += 1
        assert pages_walked <= size + 2, "the pager did not terminate"

        empty = _react_empty_state(page)
        assert empty is None, (
            f"an empty state on page {pages_walked} of a store holding {size} calls: {empty}"
        )
        rows = cp.rows(page)
        assert rows, f"page {pages_walked} rendered no rows and no empty state"
        seen.update(cp.row_ids(page))

        nxt = page.query_selector("button.pagination-btn:has-text('Next')")
        assert nxt is not None
        if nxt.is_disabled():
            break

        # `page` updates on click but `calls` only after the fetch returns, so
        # there is a render showing the NEXT page number over the PREVIOUS
        # page's rows. Waiting on the page number alone samples that frame and
        # silently re-counts a page. Wait for the rows themselves to turn over,
        # OR for an empty state, so a pager that walks off the end fails the
        # assertion above rather than timing out here.
        cp.next_page(page)

    # Next disabling early would hide history just as badly as it enabling late.
    assert len(seen) == size, f"walked {len(seen)} of {size} calls"
    assert pages_walked == (size + REACT_PAGE_SIZE - 1) // REACT_PAGE_SIZE


def test_react_pager_survives_a_response_with_no_has_more(stack, page):
    """The clause that caused D3 existed to keep Next alive without `has_more`.

    Removing it must not silently strand the pager on page 1 if the field ever
    goes missing, so the replacement derives it from the total instead. Driven
    by stripping `has_more` from the real response in the browser, which is the
    only way to reach that branch: this API always sends the field.
    """
    running = stack({"voice": retained_calls(45), "hermes": []})

    def strip_has_more(route):
        response = route.fetch()
        body = response.json()
        body.pop("has_more", None)
        route.fulfill(response=response, json=body)

    page.route("**/api/calls?*", strip_has_more)
    page.goto(running.base, wait_until="networkidle")
    cp.wait_for_rows(page)

    seen = set()
    for expected in (1, 2, 3):
        assert f"Page {expected} " in body_text(page)
        seen.update(cp.row_ids(page))
        nxt = page.query_selector("button.pagination-btn:has-text('Next')")
        if expected < 3:
            assert not nxt.is_disabled(), (
                f"Next died on page {expected} of 3 when has_more was absent"
            )
            cp.next_page(page)
        else:
            assert nxt.is_disabled(), "Next stayed live past the end of the history"

    assert len(seen) == 45
    assert _react_empty_state(page) is None


def test_react_empty_state_still_appears_when_the_store_really_is_empty(stack, page):
    """The contrast: the fix above must not make the empty state unreachable."""
    running = stack({"voice": [], "hermes": []})
    page.goto(running.base, wait_until="networkidle")
    page.wait_for_selector(".empty-state")
    assert "No calls recorded yet" in body_text(page)


# --------------------------------------------------------------------------
# D1 and D2: no element claims completeness the read cannot support.
# --------------------------------------------------------------------------


def _completeness_claims(text):
    """Phrases that assert the owner is seeing his whole history.

    A grep, and therefore the weak half of this file: it only knows the
    sentences previous rounds actually wrote. Prefer `_is_qualified` below,
    which asserts a property of what IS rendered rather than the absence of
    something remembered.
    """
    lowered = text.lower()
    return [phrase for phrase in ("every call is shown", "all calls are shown")
            if phrase in lowered]


# The two ways either screen hedges a number it cannot stand behind. A count
# rendered under `partial` must carry one of these; a count rendered on a
# complete read must carry neither.
_QUALIFIERS = ("at least", "could be read")


def _is_qualified(text):
    lowered = text.lower()
    return any(phrase in lowered for phrase in _QUALIFIERS)


def _expected_count_phrase(scenario):
    """How the React screen must state this cell's total, qualifier included."""
    noun = "call" if scenario.total_calls == 1 else "calls"
    qualifier = "at least " if scenario.expect_partial else ""
    return f"of {qualifier}{scenario.total_calls} {noun}"


_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _assert_years(rendered_cells, scenario, undated_wording):
    """Every dated cell shows the store's year; undated ones say so honestly.

    This is the timestamp axis's invariant. It is deliberately a property --
    "the year on screen is the year in the store" -- and not a check for the
    last defect's output, so it catches a stamp resolved as milliseconds, in the
    server's timezone, or not at all.
    """
    dated = 0
    for cell_text in rendered_cells:
        years = set(_YEAR_RE.findall(cell_text))
        if not years:
            assert undated_wording.lower() in cell_text.lower(), (
                f"a cell with no year that does not say so either: {cell_text!r}"
            )
            continue
        dated += 1
        assert scenario.expect_year is not None, (
            f"a year rendered for a call the store gave no timestamp: {cell_text!r}"
        )
        assert years == {str(scenario.expect_year)}, (
            f"rendered {sorted(years)} for a call stored in {scenario.expect_year}: "
            f"{cell_text!r}"
        )

    if rendered_cells and scenario.expect_year is not None:
        assert dated, "no row rendered a date, but the store stamped every call"
    if rendered_cells and not scenario.expect_some_undated:
        assert dated == len(rendered_cells), (
            "a row lost its timestamp in a corpus where every call has one"
        )


def test_react_heading_badge_states_the_total_not_the_page(stack, page):
    """The badge is the screen's answer to "how many calls do I have".

    Operators are told to treat it as authoritative, and until
    this test nothing bound it: replacing `{total}` with `{calls.length}` made a
    250-call store read "20 calls" in the heading with all 118 browser and
    Calls-API tests still green.
    """
    running = stack({"voice": retained_calls(250), "hermes": []})
    page.goto(running.base, wait_until="networkidle")
    cp.wait_for_rows(page)

    assert len(cp.rows(page)) == REACT_PAGE_SIZE
    badge = page.inner_text(".count-badge").strip()
    assert badge == "250 calls", f"the heading reported {badge!r} for a 250-call store"


def test_react_count_is_qualified_only_when_the_read_was_bounded(stack, page):
    """The badge and pager under a bound, against the same numbers complete.

    Round 6 fixed the word "all" on the legacy count line and left both React
    elements printing `total` flatly, so the screen rendered byte-identical text
    for a complete history of ten calls and for a 210-document bank it could not
    finish reading.
    """
    calls = retained_calls(10)
    memories = hermes_memories(200)

    # A. the same ten calls, read completely
    complete = stack({"voice": calls, "hermes": []})
    page.goto(complete.base, wait_until="networkidle")
    cp.wait_for_rows(page)
    badge_complete = page.inner_text(".count-badge").strip()
    pager_complete = page.inner_text(".pagination-bar").strip()
    assert badge_complete == "10 calls"
    assert not _is_qualified(pager_complete), pager_complete

    # B. the same ten calls behind 200 memories, in a bank the fetch cannot finish
    bounded = stack({"voice": {"docs": calls + memories, "ignore_offset": True},
                     "hermes": []})
    page.goto(bounded.base, wait_until="networkidle")
    cp.wait_for_rows(page)
    text = body_text(page)
    assert "Partial call history" in text
    badge_bounded = page.inner_text(".count-badge").strip()
    pager_bounded = page.inner_text(".pagination-bar").strip()

    assert badge_bounded != badge_complete, (
        "identical text for a complete history of 10 and a bank of 210 documents "
        f"the read could not finish: {badge_bounded!r}"
    )
    assert _is_qualified(badge_bounded), badge_bounded
    assert _is_qualified(pager_bounded), pager_bounded
    assert "10" in badge_bounded, "the floor itself is still worth stating"


# --------------------------------------------------------------------------
# The invariants, asserted against every cell of the matrix.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", MATRIX, ids=lambda c: c.name)
def test_react_first_page_is_honest_for_every_matrix_cell(matrix_stack, page, scenario):
    """One screen, every store shape: never a fake empty state, never a false claim."""
    running = matrix_stack(scenario)
    page.goto(running.base, wait_until="networkidle")
    page.wait_for_selector(f"{cp.ROWS}, .empty-state")
    text = body_text(page)

    rows = cp.rows(page)
    empty = _react_empty_state(page)

    if scenario.total_calls == 0:
        # An honest empty state is required here, and it must not be the
        # "no calls recorded" one when the reason is a store that failed.
        assert empty is not None, "a store with no calls must render an empty state"
        if scenario.expect_partial:
            assert "No calls recorded yet" not in empty
    else:
        assert empty is None, f"empty state against a store holding calls: {empty}"
        assert rows, "a store holding calls rendered no rows"

    if scenario.total_calls not in (None, 0):
        assert _expected_count_phrase(scenario) in text, (
            f"the pager must state the true total as {_expected_count_phrase(scenario)!r}"
        )
        assert len(rows) == min(scenario.total_calls, REACT_PAGE_SIZE)

    # A read that was cut short says so; a complete read does not cry wolf.
    partial_banner = "Partial call history" in text
    assert partial_banner == scenario.expect_partial, (
        f"partial banner={partial_banner}, expected {scenario.expect_partial}"
    )
    assert _completeness_claims(text) == []

    # Under a bound, `total` is a floor. Both places this screen prints it must
    # say so -- the badge and the pager. This assertion replaces an opt-out that
    # skipped the count check for exactly the cells where the count is a floor,
    # so the matrix walked up to the defect and declined to look.
    if rows:
        badge = page.inner_text(".count-badge").strip()
        pager = page.inner_text(".pagination-bar").strip()
        if scenario.expect_partial:
            assert _is_qualified(badge), f"heading badge states a floor as a count: {badge!r}"
            assert _is_qualified(pager), f"pager states a floor as a count: {pager!r}"
        else:
            assert not _is_qualified(badge), (
                f"a complete read must not hedge its count: {badge!r}"
            )
            assert str(scenario.total_calls) in badge

    # No field, at any size, in either document generation, may invent a value for
    # a field that document did not carry. Asserted PER FIELD: "Not retained
    # appears somewhere in this row" is true of a row where one field alone lies,
    # which is the exact rot this file warns about. Each field is addressed by its
    # own data-field name, and a row that lost one fails loudly instead of quietly
    # asserting the wrong one -- which is what happened when ticket 05 added table
    # columns and this check kept passing against the new Outlet cell while never
    # looking at Summary again.
    names = {"Agent": "agent", "Outlet": "outlet", "Outcome": "outcome",
             "Duration": "duration", "Summary": "summary"}
    for row in rows:
        row_text = row.inner_text()
        cells = {}
        for name, field in names.items():
            el = row.query_selector(f'[data-field="{field}"]')
            assert el is not None, f"a row lost its {name} field"
            cells[name] = el.inner_text().strip()
        mission = row.query_selector('[data-field="mission"]')
        cells["Mission"] = mission.inner_text().strip() if mission else None
        # A pre-05 document carries none of these; a post-05 one carries all but
        # Summary. Nothing else is allowed in any of them.
        if scenario.generation == "pre05":
            # Nothing pre-05 wrote an Outlet, an outcome or a duration, so nothing
            # may appear in any of those. The AGENT is the exception and always
            # was: the cascade retainer wrote one from the start, and the other two
            # never did -- which is why this is "not retained OR the cascade
            # document's agent" rather than a blanket absence.
            assert cells["Agent"] in ("Not retained", "hermes-main"), cells
            assert cells["Outlet"] == "Not retained", cells
            assert cells["Outcome"] == "Not retained", cells
            assert cells["Duration"] == "Not retained", cells
        elif scenario.generation == "post05":
            # ... and where it WAS written, "not retained" is equally a lie. Without
            # this half the check passes against a screen that says "not retained"
            # unconditionally, which is how a "no invention" test rots into a tautology.
            assert cells["Outlet"] in ("Phone", "Talk"), cells
            assert cells["Outcome"] == "ok", cells
            assert cells["Duration"][0].isdigit(), cells
        assert cells["Agent"] in ("Not retained", "hermes-main", "talk-answerer"), cells
        assert cells["Outlet"] in ("Not retained", "Phone", "Talk"), cells
        assert cells["Outcome"] in ("Not retained", "ok"), cells
        assert cells["Duration"] == "Not retained" or cells["Duration"][0].isdigit(), cells
        # The row names a Mission only for an outbound call with no summary; an
        # inbound call has no Mission to lose, and the detail view says so.
        assert cells["Mission"] is None or cells["Mission"].startswith("Mission: Book a table"), cells
        assert cells["Summary"] == "Not retained" or "Booked" in cells["Summary"], cells
        assert "incomplete" not in row_text.lower()

    # Whatever shape the store stamped `created_at` in, the year on screen is
    # the year in the store. An epoch stamp used to print "Jan 21, 1970" here.
    _assert_years(
        [cp.rendered_when(page, call_id) for call_id in cp.row_ids(page)],
        scenario,
        undated_wording="Unknown date",
    )


@pytest.mark.parametrize(
    "name",
    ["health-503-with-100-healthy", "health-503-with-250-healthy"],
)
def test_a_failing_bank_never_discards_the_healthy_banks_calls(matrix_stack, page, name):
    """Bank health crossed with a large corpus."""
    scenario = cell(name)
    running = matrix_stack(scenario)

    page.goto(running.base, wait_until="networkidle")
    cp.wait_for_rows(page)
    text = body_text(page)
    assert len(cp.rows(page)) == REACT_PAGE_SIZE
    # A bank that could not be read makes the total a floor, so the count is
    # qualified here even though the healthy bank's calls are all present.
    assert _expected_count_phrase(scenario) in text
    assert "Partial call history" in text and "hermes" in text
    assert "Call Archive Unreachable" not in text
    # ...and nowhere on the page does anything claim the read was complete.
    # (Ticket 15: this assertion used to live on the legacy half of this test.)
    assert _completeness_claims(text) == []


def test_absent_secondary_bank_at_scale_is_not_an_outage(matrix_stack, page):
    """The shipping configuration, crossed with a corpus of 101 and mixed content."""
    scenario = cell("health-absent-voice-shipping-mixed")
    running = matrix_stack(scenario)

    page.goto(running.base, wait_until="networkidle")
    cp.wait_for_rows(page)
    text = body_text(page)
    assert "of 101 calls" in text
    assert "Partial call history" not in text
    assert "Store Unreachable" not in text
    assert _completeness_claims(text) == []


# --------------------------------------------------------------------------
# D4: a call the list just rendered is never denied because the scan was short.
# --------------------------------------------------------------------------


def test_a_call_beyond_the_first_scan_page_is_not_reported_as_missing(stack, page):
    """250 calls in a store with no fetch-by-id endpoint.

    The list renders call 200 on page 11 and then the detail said
    "Call '...' not found" -- an incomplete read reported as proof of absence,
    which is the class round 4 fixed for unreadable banks.
    """
    corpus = retained_calls(250)
    # A store that has no fetch-by-id: every /documents/<id> is a 404, so
    # get_call must fall back to scanning the listing.
    running = stack({"voice": {"docs": corpus, "no_by_id": True}, "hermes": []})

    late = corpus[200]["id"]
    payload = _api(page, running.base, f"/api/calls/{late}")
    assert payload.get("call") is not None, (
        f"a call the list shows was denied by the detail: {payload.get('error')!r}"
    )
    assert payload["call"]["call_id"] == late

    # ...and a call that genuinely is not in the store is still reported as such.
    missing = _api(page, running.base, "/api/calls/voice-talk-voice-999")
    assert missing["call"] is None
    assert "not found" in (missing["error"] or "").lower()


def test_a_call_beyond_a_truncated_scan_is_not_denied(stack, page):
    """A store that ignores `offset` cannot be scanned to the end.

    The honest answer is the one the unreadable-bank path already gives: not
    found in what could be read, and it may exist beyond it.
    """
    corpus = retained_calls(250)
    running = stack(
        {"voice": {"docs": corpus, "no_by_id": True, "ignore_offset": True}, "hermes": []}
    )
    late = corpus[200]["id"]
    payload = _api(page, running.base, f"/api/calls/{late}")
    assert payload["call"] is None
    error = payload["error"] or ""
    assert "may exist" in error, f"an incomplete scan reported as absence: {error!r}"
    assert payload["partial"] is True


# --------------------------------------------------------------------------
# D5: one instant, one day, whatever timezone the browser is in.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tz", ["UTC", "Pacific/Kiritimati", "America/Los_Angeles"])
def test_the_screen_puts_a_naive_timestamp_on_the_retained_day(browser, stack, tz):
    """A `created_at` with no UTC offset must land on the day the store held.

    `_parse_ts` used to resolve a naive stamp in the SERVER's timezone while the
    screen parsed the same string in the BROWSER's, so the same call read as two
    different days at once. Ticket 15 removed the second screen this test used
    to cross-check against; the load-bearing half was never the agreement
    between two renderings, it was the agreement between the rendering and the
    instant the store actually holds, which is what stays asserted below.
    """
    from hindsight_producer_fixtures import talk_outbound

    doc = talk_outbound("voice-talk-voice-000", "2026-08-17T23:30:00Z")
    doc["created_at"] = "2026-08-17T23:30:00"  # naive: no offset at all
    running = stack({"voice": [doc], "hermes": []})

    # A fixed locale so the rendering is parseable; the timezone is the variable
    # under test.
    context = browser.new_context(timezone_id=tz, locale="en-US")
    tz_page = context.new_page()
    tz_page.set_default_timeout(10_000)
    try:
        tz_page.goto(running.base, wait_until="networkidle")
        cp.wait_for_rows(tz_page)
        react_when = cp.rendered_when(tz_page, "voice-talk-voice-000")
    finally:
        context.close()

    # Compare the calendar day the screen resolved to against the instant the
    # store actually held, resolved in this browser's timezone.
    from datetime import datetime, timezone as _tz
    from zoneinfo import ZoneInfo

    expected = (
        datetime(2026, 8, 17, 23, 30, tzinfo=_tz.utc).astimezone(ZoneInfo(tz)).date()
    )
    react_day = _rendered_date(react_when)
    assert react_day == (expected.year, expected.month, expected.day), (
        f"in {tz} the screen rendered {react_when!r} -> {react_day}, "
        f"but the retained instant is {expected}"
    )


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _rendered_date(rendered):
    """(y, m, d) from the screen's en-US rendering.

    The screen renders a day heading ("Tuesday, August 18, 2026") or
    ``dateStyle: 'medium'`` ("Aug 18, 2026"). The slash form
    ("8/18/2026") is still accepted so a locale/format change is a readable
    assertion failure rather than an unparseable one.
    """
    import re as _re

    slash = _re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", rendered)
    if slash:
        month, day, year = (int(g) for g in slash.groups())
        return year, month, day
    named = _re.search(r"([A-Z][a-z]{2})\w*\s+(\d{1,2}),?\s+(\d{4})", rendered)
    assert named, f"unparseable rendered date: {rendered!r}"
    return int(named.group(3)), _MONTHS.index(named.group(1)) + 1, int(named.group(2))


# --------------------------------------------------------------------------
# D6: a search result set that is short of the truth says so.
# --------------------------------------------------------------------------


def test_search_marks_the_result_partial_when_a_hit_could_not_be_read(stack, page):
    """A recall hit whose document cannot be fetched is counted, not silently dropped.

    `total` on the search path is "the hits we could resolve". When that is
    short of the hits the store returned, the screen must not present it as the
    number of matching calls.
    """
    corpus = retained_calls(5)
    running = stack({"voice": {"docs": corpus, "recall_ghost_hits": 3}, "hermes": []})

    payload = _api(page, running.base, "/api/calls?q=transcript")
    assert payload["skipped"] >= 3
    assert payload["partial"] is True, (
        "hits that could not be resolved were dropped without a word"
    )
    assert payload["warning"] and "search" in payload["warning"].lower()

    page.goto(f"{running.base}/?", wait_until="networkidle")
    page.fill("input[type='text'], input[type='search']", "transcript")
    # The pre-search list is already on screen, so waiting for a table row here
    # matches the OLD render and reads the page mid-flight -- an intermittent
    # red that has nothing to do with the claim below. Wait for the search's own
    # response, then for the spinner it put up to come down.
    with page.expect_response(
            lambda r: "/api/calls" in r.url and "q=transcript" in r.url):
        page.keyboard.press("Enter")
    page.wait_for_function("() => !document.querySelector('.spinner')")
    page.wait_for_selector(f"{cp.ROWS}, .empty-state")
    assert "Partial call history" in body_text(page)
