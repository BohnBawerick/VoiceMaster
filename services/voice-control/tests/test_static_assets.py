"""The committed bundle really contains every screen (ticket 11, round 2).

**The failure this exists to catch.** `static/assets/index-*.js` is ONE file
containing every screen, `static/` is committed, and no Python suite builds it.
So a merge conflict in `static/` resolved by picking a side ships a dashboard
with an entire feature missing — the other ticket's whole screen, gone — while
every test in every suite stays green. That near-miss has happened three times
on this project. Nothing else in the repo can see it: the browser tests drive
whatever bundle is on disk, so they pass happily against a stale one that simply
does not have the screen they are not looking at.

So these tests read the COMMITTED tree, never a freshly built one. A test that
built the bundle first would prove the sources are fine and say nothing about
what is about to be deployed, which is the only thing at issue.

Three questions, in the order they catch things:

1. does every asset `index.html` points at exist? (a dangling src means the
   bundle was resolved to a filename that is not there);
2. is there anything in `assets/` that `index.html` does NOT point at? (a
   leftover second bundle is the signature of a hand-resolved conflict — the
   protocol is to delete BOTH sides and rebuild, never to pick one);
3. does the referenced bundle contain a marker from every screen? (this is the
   one that catches the stale-but-valid bundle, where every file exists and
   agrees and the JavaScript is simply older than a screen).

**Adding a screen? Add it to SCREEN_MARKERS below.** That list is the point of
this module; a screen that is not in it is a screen this guard cannot protect.
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
ASSETS = STATIC / "assets"
INDEX = STATIC / "index.html"

# Every screen the dashboard ships, and strings only that screen's own source
# can put in the bundle. Prefer route and API paths over visible labels: labels
# get reworded by design work and would make this fail for a good reason, which
# is how a guard gets deleted. The `data-testid` values are the other stable
# choice - the browser suites already depend on them, so they cannot be renamed
# quietly either.
#
# Each screen needs ALL of its markers present.
SCREEN_MARKERS = {
    "calls (ticket 01)": ["/api/calls?", "summary-cell"],
    "agents + outlets (ticket 02)": ["/api/active", "agent-card-"],
    "new-agent wizard (ticket 13)": ["/api/agents/create", "agent-wizard"],
    "agent page (voice and tools per Agent)": ["agent-detail", "agent-tab-", "settings-agent-voice"],
    "call page (/calls/<id>, waveform)": ["call-detail", "waveform-canvas", "transcript-verbatim"],
    "place a call (ticket 09)": ["/api/calls/place", "place-call"],
    "mission authoring (ticket 10)": ["/api/missions/expand",
                                      "/api/missions/dictate",
                                      "place-assist-status"],
    "settings (ticket 14)": ["/api/settings", "settings-advanced", "settings-hermes-tools"],
    "schedule (ticket 11)": ["/api/schedules", "schedule-row-"],
}

_ASSET_REF = re.compile(r'(?:src|href)="(/assets/[^"]+)"')


def referenced_assets() -> list:
    """Every /assets/… path index.html asks the browser to load."""
    return _ASSET_REF.findall(INDEX.read_text())


def test_index_html_exists_and_references_a_bundle():
    assert INDEX.is_file(), f"{INDEX} is missing — static/ is committed and served"
    refs = referenced_assets()
    scripts = [r for r in refs if r.endswith(".js")]
    styles = [r for r in refs if r.endswith(".css")]
    assert scripts, "index.html references no script — the dashboard would be blank"
    assert styles, "index.html references no stylesheet"


def test_every_referenced_asset_exists_on_disk():
    """A dangling src is a bundle resolved to a filename nobody built."""
    missing = [ref for ref in referenced_assets()
               if not (STATIC / ref.lstrip("/")).is_file()]
    assert not missing, (
        "index.html references files that are not in the tree: "
        + ", ".join(missing)
        + f".\nWhat is actually in {ASSETS.name}/: "
        + ", ".join(sorted(p.name for p in ASSETS.iterdir()))
        + "\nThis is what a hand-resolved static/ conflict looks like. Delete "
          "everything in static/assets/, run `npm run build` in ui/, and commit "
          "what that produced.")


def test_nothing_in_assets_is_unreferenced():
    """A leftover second bundle means a conflict was resolved by keeping both
    sides. Whichever one index.html points at, the other is a whole ticket's
    work sitting in the tree and never loaded."""
    referenced = {Path(ref).name for ref in referenced_assets()}
    on_disk = {p.name for p in ASSETS.iterdir() if p.is_file()}
    orphans = sorted(on_disk - referenced)
    assert not orphans, (
        "static/assets/ holds files index.html never loads: " + ", ".join(orphans)
        + "\nThat is the signature of a merge resolved by keeping both bundles. "
          "The protocol is to delete BOTH sides and rebuild, never to pick one: "
          "rm static/assets/*, then `npm run build` in ui/.")


def _bundle_text() -> str:
    scripts = [r for r in referenced_assets() if r.endswith(".js")]
    return "\n".join((STATIC / s.lstrip("/")).read_text(errors="replace")
                     for s in scripts)


@pytest.mark.parametrize("screen", sorted(SCREEN_MARKERS))
def test_the_committed_bundle_carries_every_screen(screen):
    """The stale-bundle case: every file present, every reference valid, and
    the JavaScript simply older than one of the screens it is supposed to
    serve. Nothing else in this repo notices."""
    bundle = _bundle_text()
    absent = [m for m in SCREEN_MARKERS[screen] if m not in bundle]
    assert not absent, (
        f"the committed bundle is missing the {screen} screen — none of "
        f"{absent} is in it.\nThe bundle was probably built from a tree that "
        f"predates that screen, or a static/ merge conflict was resolved by "
        f"picking one side. Rebuild it: rm static/assets/*, then `npm run "
        f"build` in ui/, and commit the result.")


def test_every_screen_the_ui_renders_is_in_the_marker_list():
    """The list above cannot protect a screen nobody added to it, so the list
    itself is checked against the router: every `screen === '…'` branch in
    App.tsx must appear as a marker entry."""
    app_tsx = (STATIC.parent / "ui" / "src" / "App.tsx").read_text()
    rendered = set(re.findall(r"screen === '([a-z-]+)'", app_tsx))
    covered = " ".join(SCREEN_MARKERS)
    uncovered = sorted(s for s in rendered if s not in covered)
    assert not uncovered, (
        f"App.tsx renders {uncovered} and SCREEN_MARKERS says nothing about "
        f"it. Add a marker for that screen — a route or API path only its own "
        f"source produces — or this guard silently stops covering it.")
