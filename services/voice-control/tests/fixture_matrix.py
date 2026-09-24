"""The fixture matrix the Calls-screen tests are written against.

Six review rounds of ticket 01 each closed the defect it was handed and left
the same class alive one element to the left. The reviewer's diagnosis of why:

    "No scenario ever combined two dimensions at once. Round 4's fixtures were
     four documents. Round 5's are 250 documents *or* mixed-content, never both,
     and never 100 exactly."

Every defect in those rounds sat at an intersection nobody had visited, and
three of them at an intersection of exactly two dimensions. So corpora for
screen tests are built here, crossed, and named -- not invented per-test.

THE FIVE DIMENSIONS

1. corpus size      0, 1, under a page, EXACTLY one page, one page plus one,
                    several pages, and EXACTLY a multiple of the page size
                    (``CallsView.tsx`` ``PAGE_SIZE``). 100 is the exact-multiple cell
                    D3 and D1 hid in. It was also exactly the one page the
                    pager-less legacy screen asked for, which is why the number
                    is 100 and not something rounder; ticket 15 deleted that
                    screen but the cell is still the multiple.
2. content mix      calls only, or calls mixed with the ordinary Hermes
                    memories that share the ``hermes`` bank. Under the shipping
                    configuration (``HINDSIGHT_BANK=hermes``) a non-empty mix is
                    the EXPECTED state, not an edge case, so it is crossed with
                    size rather than tested alone.
3. read completeness  a full read, or a read a bound stopped (``partial``).
                    ``ignore_offset`` is the real-world shape that causes it: a
                    store that honours ``limit`` and ignores ``offset`` hands
                    back page 1 forever, so the fetch stops after one page and
                    what it holds is a prefix of the bank.
4. bank health      all banks answer, one bank 503 (an outage: its calls exist
                    and are not shown), one bank 404 (absent: it does not exist,
                    which is the expected answer for ``voice`` today).
5. document generation  WHICH ERA OF METADATA the document carries: a pre-05
                    document (platform/direction/target/date and nothing else)
                    or a post-05 one (outlet, agent, mission, outcome,
                    duration_s and an ISO ``timestamp``). The store holds both
                    forever - the owner confirmed no migration is wanted - so
                    "both generations in one list" is the NORMAL state, not an
                    edge case, and it is crossed with size, content mix and read
                    completeness. A screen that renders one generation correctly
                    can still coerce the other: filling a pre-05 row's Outlet
                    from its ``platform`` was the first draft of ticket 05, and
                    it put "voice_twilio" in a column the owner filters on.
6. timestamp shape  what ``created_at`` actually is: ISO with an offset, ISO
                    with none, an epoch number, an epoch numeric string, absent
                    (leaving the retainers' date-only ``metadata["date"]``), or
                    absent with no date at all. **Four of the last eight review
                    items have been timestamp defects** -- a date-only stamp
                    printed as midnight, a naive stamp putting one call on
                    two days, and an epoch stamp printing "Jan 21, 1970" on the
                    screen while the API said 2026. Every earlier version of this
                    matrix hard-coded ISO-Z for every document in every cell,
                    which is exactly why the epoch one survived a matrix that
                    caught everything else.

Every document comes from ``hindsight_producer_fixtures``, which is built from
the three retainers' source. Nothing here is derived from an existing test.

COVERAGE, AND WHERE THE GAPS DELIBERATELY ARE

The full cross product is 9 sizes x 2 mixes x 2 completenesses x 3 healths x 6
timestamp shapes = 648 browser scenarios, which would take longer to run than
anyone will wait. ``MATRIX`` below is the chosen subset.
Each cell states which intersection it exists to cover, and
``test_calls_matrix.py`` asserts the same honesty invariants against all of
them, so a new defect has to survive every cell rather than the one its author
had in mind. What is NOT covered, on purpose:

- size x health beyond one bank failing: two banks failing at once is the
  "no bank answered" path, covered as an API case, not per size.
- mix x completeness x health all three at once: covered pairwise only.
- sizes 1 and 19 are only crossed with the healthy/calls-only cell; they exist
  to pin singular/plural wording and the under-one-page count line.
- timestamp shape x bank health: a stamp renders the same however the OTHER
  bank answered, so this cross buys nothing. Timestamp IS crossed with size,
  content mix and read completeness.
- generation x timestamp shape: a post-05 document carries its own ISO
  ``timestamp`` in metadata as well as whatever ``created_at`` the store stamps,
  so the two axes are not independent for it. **If a timestamp defect ever turns
  up on a post-05 row specifically, this is the cross to build.**
- generation x the FILTERS: the filter row is covered by dedicated tests
  (``test_calls_metadata_browser``), not per cell -- a filter narrows the list,
  which would fight every per-cell count assertion here. **This is a declared
  gap:** the matrix checks that both generations RENDER honestly at every size,
  never that they FILTER correctly at every size.
- **summary state (ticket 06) is a seventh axis, and it is NOT on this matrix.**
  A call now carries one of four summary answers (a written summary,
  ``nothing_to_summarise``, ``unavailable``, or no state at all), and the screen
  has to render the three absences as three different things. That is covered by
  dedicated tests -- ``test_call_summary_api`` and ``test_call_summary_browser``
  -- at one size, one mix, one health, because the rendering of one cell does not
  depend on how many rows are around it. **What this therefore does not check:
  summary state crossed with a bounded read or a failing bank**, where a partial
  view might carry a state the count line then misreports. If a summary defect
  ever turns up on a truncated or degraded list, this is the cross to build.
- the search path is not on the matrix at all: recall replaces the listing, so
  the size and completeness axes do not apply to it in the same way. It is
  covered by dedicated tests. **This is the largest declared gap** -- if a
  defect turns up in search, this is the first place to look.

WHAT THIS MATRIX DECLARES AND DOES NOT CHECK

Ask this before adding anything, because it is how the last two rounds' defects
survived: F1 sat in a cell this matrix declared (`truncated-*`, "a floor, not a
count") while the per-cell test opted out of the count assertion for exactly
those cells; F2 sat on an axis it did not have. Currently believed to be checked
everywhere it is declared. The assertions most likely to rot in the same way are
the ones that grep for a phrase rather than assert a property -- prefer
"the count carries a qualifier" over "the page does not contain this sentence".

If you add a screen test, add its corpus here and say which intersection it
adds. If you find a defect, the first question is which intersection it was
hiding in -- and that cell goes in.
"""
from datetime import datetime

from hindsight_producer_fixtures import (
    cascade_outbound,
    drop_created_at,
    non_call_memory,
    phone_inbound_v5_no_agent,
    phone_outbound_v5,
    talk_inbound_v5,
    talk_outbound,
    twilio_inbound,
)

# The screen's page size, which is what makes "exactly one page" and "exact
# multiple" mean two different corpus sizes. Asserted against ``CallsView.tsx`` in
# test_calls_matrix.py, so it cannot drift away from this file silently.
REACT_PAGE_SIZE = 20


# The year every corpus below is stamped in. The screen must render THIS year
# for every dated call, whatever shape the stamp arrived in.
CORPUS_YEAR = 2026

# The timestamp shapes a store might hand back. "iso" is what the earlier
# matrix assumed for everything; the rest are the shapes that produced defects.
TIMESTAMP_SHAPES = ("iso", "naive", "epoch", "epoch-string", "date-only", "absent")


def _restamp(doc, shape, iso):
    """Re-express one document's ``created_at`` in the given shape."""
    if shape == "iso":
        return doc
    if shape == "naive":
        # The same instant with no offset at all: whoever parses it decides.
        doc = dict(doc)
        doc["created_at"] = iso.replace("Z", "")
        return doc
    if shape in ("epoch", "epoch-string"):
        epoch = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        doc = dict(doc)
        doc["created_at"] = epoch if shape == "epoch" else str(epoch)
        return doc
    if shape == "date-only":
        # No `created_at`: twilio/talk keep the retainers' `%Y-%m-%d`, and a
        # cascade document is left with no timestamp at all.
        return drop_created_at(doc)
    if shape == "absent":
        doc = drop_created_at(doc)
        doc["metadata"] = {k: v for k, v in doc["metadata"].items() if k != "date"}
        return doc
    raise ValueError(f"unknown timestamp shape {shape!r}")


# The document generations the store holds (dimension 5). "mixed" is the shape
# the live store is in from the moment ticket 05 deploys, and stays in.
GENERATIONS = ("pre05", "post05", "mixed")


def _pre05_doc(doc_id, stamp, i):
    """Rotating the retainer means no cell is accidentally single-producer: the
    inbound Twilio documents carry an empty ``target``, and the cascade documents
    carry an ``agent`` and no ``date``."""
    if i % 3 == 0:
        return twilio_inbound(doc_id, stamp)
    if i % 3 == 1:
        return talk_outbound(doc_id, stamp)
    return cascade_outbound(doc_id, stamp)


def _post05_doc(doc_id, stamp, i):
    """The three states a post-05 document can be in, rotated for the same reason:
    fully recorded on the phone Outlet, recorded on the Talk Outlet, and recorded
    on an Outlet with no Agent assigned (agent absent, everything else present)."""
    if i % 3 == 0:
        return phone_outbound_v5(doc_id, stamp, agent="hermes-main")
    if i % 3 == 1:
        return talk_inbound_v5(doc_id, stamp, agent="talk-answerer", duration_s=12.0)
    return phone_inbound_v5_no_agent(doc_id, stamp)


def retained_calls(n, prefix="voice-talk", bank_tag="voice", timestamp="iso",
                   generation="pre05"):
    """``n`` retained calls, newest first when sorted, from all the retainers.

    ``timestamp`` selects the shape of ``created_at`` (see ``TIMESTAMP_SHAPES``);
    every shape names the same instants. ``generation`` selects which era of
    metadata the documents carry (see ``GENERATIONS``); "mixed" alternates, which
    is what the live store looks like from the ticket-05 deploy onwards.
    """
    docs = []
    for i in range(n):
        # Spread across days and minutes so the global sort has real work to do
        # and two banks genuinely interleave.
        day = 17 - (i // 500)
        stamp = f"{CORPUS_YEAR}-08-{day:02d}T{6 + (i % 16):02d}:{i % 60:02d}:00Z"
        doc_id = f"{prefix}-{bank_tag}-{i:03d}"
        if generation == "pre05":
            doc = _pre05_doc(doc_id, stamp, i)
        elif generation == "post05":
            doc = _post05_doc(doc_id, stamp, i)
        elif generation == "mixed":
            doc = (_pre05_doc(doc_id, stamp, i) if i % 2 == 0
                   else _post05_doc(doc_id, stamp, i))
        else:
            raise ValueError(f"unknown generation {generation!r}")
        docs.append(_restamp(doc, timestamp, stamp))
    return docs


def hermes_memories(n, prefix="hermes-note"):
    """``n`` ordinary Hermes memories: the documents that make ``skipped`` > 0."""
    return [
        non_call_memory(f"{prefix}-{i:03d}", f"2026-08-16T09:{i % 60:02d}:00Z")
        for i in range(n)
    ]


class Cell:
    """One matrix cell: a store shape plus the intersection it covers.

    ``banks`` is the mock's own spec language (see ``_StoreHandler`` in
    ``test_calls_browser``): a list of documents is a healthy bank, an ``int`` is
    that HTTP status, a bank absent from the mapping is a 404, and
    ``{"docs": [...], "ignore_offset": True}`` is a store that honours ``limit``
    and ignores ``offset``.
    """

    def __init__(self, name, banks, configured_bank, covers,
                 total_calls, expect_partial=False, expect_skipped=0,
                 expect_year=CORPUS_YEAR, expect_some_undated=False,
                 generation="pre05"):
        self.name = name
        self.banks = banks
        self.configured_bank = configured_bank
        self.covers = covers
        # What a truthful screen must report for this store, computed here from
        # the corpus rather than read back off the API under test.
        self.total_calls = total_calls
        self.expect_partial = expect_partial
        self.expect_skipped = expect_skipped
        # The year every DATED row must render. `None` means no row in this cell
        # carries a timestamp at all. `expect_some_undated` allows rows that
        # honestly say so alongside dated ones (a date-only corpus leaves the
        # cascade documents with nothing, since no retainer stamps them).
        self.expect_year = expect_year
        self.expect_some_undated = expect_some_undated
        # Which era of metadata this cell's documents carry (dimension 5). It
        # decides which cells the screens are allowed to fill in: a pre-05 row
        # must say "not retained" in every ticket-05 column, a post-05 row must
        # carry the value, and a "mixed" corpus must do BOTH in one list without
        # coercing either into the other.
        self.generation = generation

    def __repr__(self):
        return f"<Cell {self.name}>"


def _cells():
    cells = []

    # ---- size, healthy, calls only -------------------------------------
    # The size axis on its own. 100 is the cell D3 lived in for five rounds:
    # an exact multiple of the React page size.
    for size in (0, 1, 19, 20, 21, 99, 100, 101, 250):
        cells.append(Cell(
            name=f"size-{size}",
            banks={"voice": retained_calls(size), "hermes": []},
            configured_bank="voice",
            covers=f"corpus size {size}, healthy banks, calls only",
            total_calls=size,
        ))

    # ---- size x content mix --------------------------------------------
    # The intersection D1 lived in: more than one page AND non-call documents,
    # so `skipped` > 0 and the read is a subset at the same time. Plus its
    # contrast, where the read IS complete and skipped is still > 0.
    cells.append(Cell(
        name="mixed-250-calls-40-memories",
        banks={"hermes": retained_calls(250, bank_tag="hermes") + hermes_memories(40)},
        configured_bank="hermes",
        covers="size 250 (several pages) x mixed content -- the shipping shape",
        total_calls=250,
        expect_skipped=40,
    ))
    cells.append(Cell(
        name="mixed-100-calls-40-memories",
        banks={"hermes": retained_calls(100, bank_tag="hermes") + hermes_memories(40)},
        configured_bank="hermes",
        covers="size 100 (exact React page-size multiple) x mixed content",
        total_calls=100,
        expect_skipped=40,
    ))
    cells.append(Cell(
        name="mixed-20-calls-5-memories",
        banks={"hermes": retained_calls(20, bank_tag="hermes") + hermes_memories(5)},
        configured_bank="hermes",
        covers="size 20 (exact React page) x mixed content",
        total_calls=20,
        expect_skipped=5,
    ))
    cells.append(Cell(
        name="mixed-only-memories",
        banks={"hermes": hermes_memories(30)},
        configured_bank="hermes",
        covers="a bank of nothing but non-call documents: an honest empty list",
        total_calls=0,
        expect_skipped=30,
    ))

    # ---- read completeness ----------------------------------------------
    # A store that ignores `offset`: the fetch stops after one page, so what
    # came back is a prefix and `partial` must be set. Crossed with mixed
    # content, because a truncated read of a mixed bank is what the `hermes`
    # bank looks like in production once it is busy.
    cells.append(Cell(
        name="truncated-250",
        banks={"voice": {"docs": retained_calls(250), "ignore_offset": True},
               "hermes": []},
        configured_bank="voice",
        covers="several pages x truncated read (store ignores offset)",
        total_calls=None,  # a floor, not a count -- that is the point
        expect_partial=True,
    ))
    cells.append(Cell(
        name="truncated-mixed",
        # 10 calls at the head, 200 ordinary memories behind them: the first
        # page the fetch can reach holds all 10 calls and 90 memories, so the
        # surviving total (10) FITS one screen while the read is still a prefix
        # of a 210-document bank. That is the shape in which "Showing all 10
        # calls." is a false completeness claim.
        banks={"hermes": {"docs": retained_calls(10, bank_tag="hermes") + hermes_memories(200),
                          "ignore_offset": True}},
        configured_bank="hermes",
        covers="truncated read x mixed content -- few calls, busy bank",
        total_calls=None,
        expect_partial=True,
        expect_skipped=None,  # whatever the prefix held
    ))

    # ---- bank health -----------------------------------------------------
    # A failing bank never discards what the healthy bank returned; an absent
    # bank is not an outage. Crossed with size so neither is only ever seen
    # against four documents.
    cells.append(Cell(
        name="health-503-with-100-healthy",
        banks={"voice": retained_calls(100), "hermes": 503},
        configured_bank="voice",
        covers="size 100 (exact multiple) x one bank 503",
        total_calls=100,
        expect_partial=True,
    ))
    cells.append(Cell(
        name="health-503-with-250-healthy",
        banks={"voice": retained_calls(250), "hermes": 503},
        configured_bank="voice",
        covers="size 250 (several pages) x one bank 503",
        total_calls=250,
        expect_partial=True,
    ))
    cells.append(Cell(
        name="health-absent-voice-shipping",
        banks={"hermes": retained_calls(100, bank_tag="hermes")},
        configured_bank="hermes",
        covers="the shipping configuration: HINDSIGHT_BANK=hermes, `voice` 404s",
        total_calls=100,
        expect_partial=False,
    ))
    cells.append(Cell(
        name="health-absent-voice-shipping-mixed",
        banks={"hermes": retained_calls(101, bank_tag="hermes") + hermes_memories(12)},
        configured_bank="hermes",
        covers="shipping configuration x one page plus one x mixed content",
        total_calls=101,
        expect_partial=False,
        expect_skipped=12,
    ))

    # ---- document generation --------------------------------------------
    # Dimension 5, crossed with size, content mix and read completeness. The
    # "mixed" cells are the live store from the ticket-05 deploy onward.
    for size in (1, 20, 101):
        cells.append(Cell(
            name=f"gen-post05-{size}",
            banks={"voice": retained_calls(size, generation="post05"), "hermes": []},
            configured_bank="voice",
            covers=f"corpus size {size} x every document carrying ticket-05 metadata",
            total_calls=size,
            generation="post05",
        ))
    cells.append(Cell(
        name="gen-mixed-100",
        banks={"voice": retained_calls(100, generation="mixed"), "hermes": []},
        configured_bank="voice",
        covers="an exact React multiple x BOTH generations in one list",
        total_calls=100,
        generation="mixed",
    ))
    cells.append(Cell(
        name="gen-mixed-250-memories",
        banks={"hermes": retained_calls(250, bank_tag="hermes", generation="mixed")
               + hermes_memories(40)},
        configured_bank="hermes",
        covers="several pages x mixed content x BOTH generations",
        total_calls=250,
        expect_skipped=40,
        generation="mixed",
    ))
    cells.append(Cell(
        name="gen-mixed-truncated",
        banks={"voice": {"docs": retained_calls(250, generation="mixed"),
                         "ignore_offset": True},
               "hermes": []},
        configured_bank="voice",
        covers="truncated read x BOTH generations",
        total_calls=None,
        expect_partial=True,
        generation="mixed",
    ))

    # ---- timestamp shape ------------------------------------------------
    # The axis that produced four of the last eight review items and was on no
    # earlier version of this matrix. Every cell holds the same instants; only
    # the shape `created_at` arrives in differs, and every screen must still
    # render the same year for all of them.
    for shape in ("naive", "epoch", "epoch-string"):
        cells.append(Cell(
            name=f"ts-{shape}-20",
            banks={"voice": retained_calls(20, timestamp=shape), "hermes": []},
            configured_bank="voice",
            covers=f"exact React page x created_at as {shape}",
            total_calls=20,
        ))
    cells.append(Cell(
        name="ts-date-only-20",
        banks={"voice": retained_calls(20, timestamp="date-only"), "hermes": []},
        configured_bank="voice",
        covers="exact React page x no created_at (retainers' %Y-%m-%d survives)",
        total_calls=20,
        expect_some_undated=True,  # cascade documents carry no date at all
    ))
    cells.append(Cell(
        name="ts-absent-20",
        banks={"voice": retained_calls(20, timestamp="absent"), "hermes": []},
        configured_bank="voice",
        covers="exact React page x no timestamp of any kind",
        total_calls=20,
        expect_year=None,
        expect_some_undated=True,
    ))
    # ...crossed with size and with content mix, as the other axes are.
    cells.append(Cell(
        name="ts-epoch-250",
        banks={"voice": retained_calls(250, timestamp="epoch"), "hermes": []},
        configured_bank="voice",
        covers="several pages x created_at as an epoch number",
        total_calls=250,
    ))
    cells.append(Cell(
        name="ts-epoch-mixed-101",
        banks={"hermes": retained_calls(101, bank_tag="hermes", timestamp="epoch")
               + hermes_memories(12)},
        configured_bank="hermes",
        covers="one page plus one x mixed content x created_at as an epoch number",
        total_calls=101,
        expect_skipped=12,
    ))
    cells.append(Cell(
        name="ts-naive-truncated",
        banks={"voice": {"docs": retained_calls(250, timestamp="naive"),
                         "ignore_offset": True},
               "hermes": []},
        configured_bank="voice",
        covers="truncated read x created_at with no offset",
        total_calls=None,
        expect_partial=True,
    ))

    return cells


MATRIX = _cells()
BY_NAME = {cell.name: cell for cell in MATRIX}


def cell(name):
    return BY_NAME[name]
