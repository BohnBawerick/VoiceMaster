"""How the browser suites address the Calls screen.

The list is a day-grouped set of rows, not a table. Each row carries its Call's
id as ``data-call-id`` (the id itself is shown only on the detail view), and
every field on it carries ``data-field``, so a test reads ONE field of ONE row
rather than grepping a row's text. A Call opens at ``/calls/<call_id>`` in a
drawer; its metadata is a list of label/value rows under
``[data-testid="call-meta"]``, and the verbatim transcript sits behind the
Transcript tab's Verbatim toggle.
"""

ROWS = '[data-testid="call-row"]'
DETAIL = '[data-testid="call-detail"]'
LOADED = '[data-testid="call-detail"] .detail-title, [data-testid="call-detail"] .alert-banner'


def row_selector(call_id: str) -> str:
    return f'{ROWS}[data-call-id="{call_id}"]'


def wait_for_rows(page, timeout: float = 10_000):
    page.wait_for_selector(ROWS, timeout=timeout)


def rows(page):
    return page.query_selector_all(ROWS)


def row_ids(page) -> list:
    return [r.get_attribute("data-call-id") for r in rows(page)]


def row(page, call_id: str):
    return page.query_selector(row_selector(call_id))


def field(page, call_id: str, name: str) -> str:
    """The rendered text of one field of one row."""
    cell = page.query_selector(f'{row_selector(call_id)} [data-field="{name}"]')
    assert cell is not None, f"row {call_id} has no {name} field"
    return cell.inner_text().strip()


def open_call(page, call_id: str):
    page.click(row_selector(call_id))
    page.wait_for_selector(LOADED)


def meta(page) -> dict:
    """The detail view's label -> value rows."""
    return page.eval_on_selector_all(
        '[data-testid="call-meta"] .meta-row',
        """rows => Object.fromEntries(rows.map(r => [
            r.querySelector('dt').innerText.trim(), r.querySelector('dd').innerText.trim()]))""",
    )


def verbatim(page) -> str:
    """Open the Transcript tab's verbatim view and return its text."""
    page.click('[data-testid="call-tab-transcript"]')
    page.click('[data-testid="transcript-verbatim"]')
    page.wait_for_selector(".transcript-body")
    return page.inner_text(".transcript-body")


def next_page(page):
    """Press Next and wait until the rows are the next page's, not the old ones."""
    first = row_ids(page)[0]
    page.click("button.pagination-btn:has-text('Next')")
    page.wait_for_function(
        """first => {
            if (document.querySelector('.empty-state')) return true;
            const row = document.querySelector('[data-testid="call-row"]');
            return !!row && row.getAttribute('data-call-id') !== first;
        }""",
        arg=first,
    )


def filter_options(page, name: str) -> list:
    """What the "+ Agent" / "+ Outlet" chip offers, in order."""
    page.click(f'[data-testid="filter-{name}"]')
    options = page.eval_on_selector_all(
        f'[data-testid^="filter-{name}-option-"]',
        f"els => els.map(e => e.getAttribute('data-testid').slice('filter-{name}-option-'.length))",
    )
    page.keyboard.press("Escape")
    return options


def pick_filter(page, name: str, value: str):
    """Choose one value from a filter chip and wait for the list to reload."""
    page.click(f'[data-testid="filter-{name}"]')
    with page.expect_response(lambda r: "/api/calls?" in r.url):
        page.click(f'[data-testid="filter-{name}-option-{value}"]')
    page.wait_for_selector(f'[data-testid="filter-{name}-set"]')
    wait_for_list(page)


def wait_for_list(page):
    """The list has settled: no spinner, and either rows or an empty state."""
    page.wait_for_function(
        """() => !document.querySelector('.calls-list .spinner') &&
                 !!document.querySelector('[data-testid="call-row"], .empty-state')"""
    )


def rendered_when(page, call_id: str) -> str:
    """When a row says its Call happened, as the screen shows it: the day heading
    the row sits under, then the row's own time (or date, when only a date was
    retained)."""
    return page.eval_on_selector(
        row_selector(call_id),
        """row => {
            const heading = row.closest('.day-group').querySelector('.day-heading');
            const day = heading.firstChild.textContent.trim();
            const when = row.querySelector('[data-field="when"]').innerText.trim();
            return `${day} ${when}`;
        }""",
    )
