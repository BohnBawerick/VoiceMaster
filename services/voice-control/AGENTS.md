# Project agent memory

Voice Control dashboard. Repo-wide rules are in the root `CLAUDE.md`; this file holds what is
specific to this service.

## `PUT /api/agents/{id}` can silently kill the live inbound line — do not "fix" this outside the rebuild

`PUT /api/active` (`app.py:520-650`, guard in `_validate_active_slot` at `app.py:637-643`) checks `activation_problem`
before storing a pointer and 422s if the change would break that direction's calls. `PUT
/api/agents/{agent_id}` (`app.py:454-483`) does not - it validates schema only and writes. Three
ordinary agent edits (switch `providers.realtime` off `openai-gpt-realtime`, set `pipeline:
cascade`, or `enabled: false`) save HTTP 200 on the agent the inbound pointer already names, and
every subsequent inbound call gets dead air - while `GET /api/agents` still reports that agent
`valid: true` (cascade is a valid document) and no event-log row is written (the bridge
refusal happens before the call recorder is constructed). That write hole was reproduced end
to end by the inbound-outage investigation. `GET /api/active` no longer hides it:
`_active_response` returns `outlets`, `outlet_order`, `warnings`, `slot_warnings` and
`voice_agent_env`, and `_read_pointer` fills `warnings` (a flat list of distinct messages)
and `slot_warnings` (`{outlet: {direction: message|null}}`) from one pass, including
`_slot_health_warning` when the stored agent is missing, disabled, invalid, or
`activation_problem` refuses that direction.

**This asymmetry is knowingly unfixed, on purpose — not an oversight to "helpfully" close.** The
owner decided on 2026-08-18 that the guard folds into the planned inbound rebuild rather than
shipping to the live system piecemeal. The hazard stays live and accepted until then. Do not add
the missing `activation_problem` call to `update_agent` outside that rebuild.

Cost to keep in mind when the rebuild lands it: mirroring the pointer-screen guard into the
agent editor makes disabling or re-plumbing the *active* inbound agent a two-step operation —
unset the pointer first, then edit — same as the delete path already forces. The fallback
posture on an activation failure was decided 2026-08-18: the bridges now answer from a
per-Outlet last-known-good snapshot, loudly (ticket 08 - `voicecore/lkg.py` and the root
AGENTS.md). The dashboard deliberately does NOT fall back: a broken slot
keeps reporting broken here while the bridges answer with the snapshot. (Ticket 15 deleted
`dryrun.py`, which was the other half of that sentence; `GET /api/active`'s per-slot health
check is what reports broken now.)

## Agent identity is the document `id:`, never the filename

The roster (`GET /api/agents`) and the bridges (`profiles._scan_agents_dir`) key an Agent by
the `id:` inside the document and accept both `*.yaml` and `*.yml`. Every dashboard endpoint
that reads or writes an existing Agent - `GET`/`PUT`/`DELETE /api/agents/{id}`,
`PUT /api/agents/{id}/voice`, the active-slot guard, eval - resolves the same way, through
`_lookup_agent`. Creating a new Agent still writes `<id>.yaml`; it refuses if that id already
exists under any filename, or if `<id>.yaml` is already on disk.

Ticket 14 shipped a three-line stop-gap on `PUT /api/agents/{id}/voice` (404 when the file
named `<id>.yaml` carried a different `id:`). That closed the silent cross-Agent write but
left `.yml` and renamed files listed and then refused. `PUT /api/agents/{id}` was the
documented sibling of that hazard - it addressed `<id>.yaml` and overwrote whatever document
lived there - and was deliberately deferred. `DELETE /api/agents/{id}` had no `id:` guard at
all, so a file whose document named another Agent could be unlinked out from under it,
bypassing the in-use 409. That deferral ended here: there is no filename address to disagree
with. Do not add `_agent_path` back onto an existing-Agent route.

This is a different hazard from the inbound `activation_problem` hole above. Do not add that
call to `update_agent` outside the inbound rebuild.

**Ticket 18 guarded the two NARROW writes, and left `update_agent` alone.** The agent-type
switch (on Settings then, on each Agent's Voice tab since the redesign) turned "set `pipeline: cascade` on the Agent answering the phone" into one
click, so `PUT /api/agents/{id}/voice` and `PUT /api/agents/{id}/hermes-profile` now run
`_slot_break_error`: the same `activation_problem`, asked for every slot the Agent holds, 409 and
nothing written. The way through is the delete path's: unassign, edit, assign. `update_agent`
is still unguarded, no screen calls it, and
`test_direct_lane_api.py::test_the_whole_document_put_is_deliberately_left_alone` pins that so
the guard is not copied across without the owner's 2026-08-18 decision being revisited. The
profile endpoint also refuses an unroutable profile on a held slot: that is a quiet outage (the
direct lane falls back on every call, the Realtime lane's tools all fail) behind an assignment
that still looks healthy.

## The active pointer is per-Outlet (ticket 16)

`active.yaml` is modeled as `outlets: {phone: {inbound, outbound}, talk: {inbound, outbound}}`
and lives in the shared `voicecore.profiles` (`OUTLETS`, `read_active_pointer`,
`load_effective_profile(direction, outlet=...)`). Ticket 17 made that the ONLY shape: a
top-level `inbound`/`outbound` key raises on read and is a 422 on write, so an assignment that
names no Outlet lands nowhere (a structurally unreadable stored value inside an outlet entry is
still dropped on the next write - documented behaviour, deliberately not a refusal).
`PUT /api/active` takes the `outlets` map and nothing else (null outlet entries are rejected,
not silently no-opped); `GET /api/active` answers with the `outlets` breakdown and no flattened
mirror of it, and warns in two layers - structural faults from the parser, and a
per-slot health check (`_slot_health_warning` in app.py) for slots that went stale after storage
(agent disabled, invalid, deleted out of band, or edited into an activation the slot's direction
refuses). That last check calls the same `profiles.activation_problem` the PUT-side
`_validate_active_slot` calls, deliberately: the two must not diverge, or the dashboard refuses
to create a state it cannot then see. `GET /api/active` must never fail because of a
broken configuration - an unresolvable slot produces a warning. `GET /api/agents` rows carry
per-agent `outlets` flags and no flattened `active` map. The two fire arms resolve their own
outlet (mode-c → phone, mode-v → talk).

**The flat keys 422, and the screen that sent them is gone.** Ticket 17 removed the shape and
ticket 15 deleted `static-legacy/agents.js`, so the hazard has neither a shape nor a caller: a
direction with no Outlet could only ever mean "both", so one click there used to collapse a
per-outlet split with a clean 200 and no warning. The React Agents screen at `/agents`
(`ui/src/AgentsView.tsx`, ticket 02) is the ONLY surface: it reads `outlets` / `slot_warnings` /
`outlet_order`, and every write names exactly one outlet and one direction. **Do not add a
flat-key write to it.** `tests/test_outlet_axis_api.py` keeps the refused flat request verbatim
so no future screen can reintroduce the click.

`GET`/`PUT /api/active` also return `slot_warnings` ({outlet: {direction: message|null}}) and
`outlet_order` (`profiles.OUTLETS`). `slot_warnings` and the flat `warnings` list come from ONE
pass in `_read_pointer` on purpose: two passes are two chances for a banner and a card to
disagree about which outlet is dead.

## Creating an Agent writes into the OTHER repo's data (ticket 13)

`hermes_profiles.py` writes a **real Hermes profile directory** - the wizard at `/agents/new`
does not invent a local imitation. The contract it writes against is not ours: it is
the Hermes supervisor's profile registry (`hermes/supervisor/hermes_profile_registry.py`), which scans
`~/.hermes/profiles/` every interval and decides what starts. The constants mirrored from it
(name rule, reserved `default`, `.incomplete`, `config.yaml`, `gateways/gateways.json`) are
asserted as constants in `tests/test_agent_wizard_api.py`, because a silent drift there breaks
creation while every behavioural test stays green. **Read that file before changing any of
them.**

`HERMES_PROFILES_DIR` has **no default, deliberately**. The deployment's compose file mounts
the `hermes-data` `profiles` subpath into this container and sets the variable. Until the mount is visible, every creation path answers 503 naming
the compose line that is missing. A default would create a profile inside this container that
no supervisor scans - a profile only this app believes in. The repository's
`docker-compose.yml` shows the mount. Ownership matters: this container runs as root and the
Hermes process usually does not, so created files are chowned to whoever owns the profiles
directory.

The abandonment guarantee is two things, and both are load-bearing: nothing is written until the
final submit (no draft endpoint, no autosave), and the one write is staged behind `.incomplete`,
removed last in a single atomic unlink. Cleanup removes `config.yaml` **first**, because a
`rmtree` that fails halfway can take the marker and leave a startable profile.

## The Calls screens relay Hindsight, they never fill in for it

Hindsight retains **one document per call**, and the store holds TWO GENERATIONS of them, forever
(the owner declined a migration). Since ticket 05 every retainer builds its metadata through
one builder, `services/voicecore/call_record.py`; before it, each hand-rolled its own. Read that
builder and the retainers before assuming a field exists, and read
`tests/hindsight_producer_fixtures.py`, which models both generations (`retained_doc` /
`retained_doc_v5`).

**The builder omits any field it was not given.** An absent key means exactly one thing: nothing
was recorded. So a pre-05 document has no outlet, mission, outcome or duration - and no agent
either, except from the cascade retainer, which wrote one from the start - and a post-05 inbound
call has no mission because it never had one. **When a field was not retained the
payload carries `None`/`""` and both screens say "not retained".** Do not supply a value in its
place, do not back-fill the Outlet from `platform` (the transport is not the Outlet, and that
guess put "voice_twilio" in the column the owner filters on), and do not infer a claim about the
call from a missing field: a document exists only because the call reached teardown with a
transcript, so "no summary" is not evidence of a bad call. Three review rounds of ticket 01 were
rejected for exactly this, each time in a new place.

Since ticket 06 a summary IS written, by the Agent that was on the call - but only when the
call held a conversation to describe. That means an empty Summary cell is one of THREE facts,
and `summary_state` is what tells them apart (`nothing_to_summarise` / `unavailable` / no key
at all). Render the three differently; never as one blank, never as prose that could be read as
the call's own words, and never as a spinner - there is no "still coming" state to wait for,
because the summary is settled before the document is written. See the root `AGENTS.md`.

Still written by nobody: a turn count.

Filtering by Agent and Outlet happens HERE, after the fetch, because the store cannot filter over
our metadata - see the paging decision in `hindsight_calls.py`. The facet lists come from the
calls actually read, so an offered filter always has calls behind it, and
`hindsight_calls.UNKNOWN_FILTER` selects the calls where the field was never written (the whole
pre-05 archive). A filtered view keeps the same `partial` warnings an unfiltered one gets.

A bank that cannot be read is likewise not evidence: a failing bank never discards the calls
another bank returned, an absent (HTTP 404) bank is not a store outage, and `get_call` never
answers "not found" when a bank it could not read might hold the call.

Nor is a **partial view** ever presented as a whole one. The bounded fetch marks a short read
`partial`, and the screen's count then reads "at least N" rather than N. A screen that renders a
subset and says nothing tells the same lie by omission - that was the round-4 blocker.

**One assumption the producers do not support: `created_at`.** No retainer sends it; we assume
Hindsight stamps it and returns it. If it does not, the only timestamp is the retainers'
`%Y-%m-%d`, which parses to midnight - so `when_precision` carries `"date"` and the screen
renders a date with no clock rather than a time nothing retained. Keep
`drop_created_at` fixtures covering that path.

**Every timestamp shape is settled in one place, `_normalize_when`.** The screen parses `when`
in the BROWSER's timezone, so any stamp that could resolve to a different instant there has to be
resolved before it leaves the API - it is not the reader's job to guess. A stamp with no offset
is assumed UTC (the choice is arbitrary; making it once is not), and an epoch number or numeric
string is converted to ISO - `new Date(1787010240.0)` reads a bare number as *milliseconds* and
printed "Jan 21, 1970" for a 2026 call while the API said 2026. **Four of the last eight review
items were timestamp defects**, which is why timestamp shape is a matrix axis and not a one-off
test. The payload half is pinned in `test_calls_hindsight_api.py`, the rendered half in
`test_calls_matrix.test_the_screen_puts_a_naive_timestamp_on_the_retained_day`, which drives
three browser timezones.

**One known residual on the React pager**: a response carrying neither `has_more` nor `total`
leaves the pager on page 1, with no count line to say there is more. Our API always sends both,
so this needs a proxy or a future API change to reach.

**Search reports the hits it could resolve, not the store's match count.** If Hindsight's recall
caps its result set, that cap is what `total` reports on the search path; we cannot see past it.
A hit whose document could not then be read does set `partial`, so the screen says the count is
short - but a capped recall is invisible to us and is not flagged. Verify against the live store
before treating a search total as a history count.

## Build fixtures from the producers, and open the page

`tests/hindsight_producer_fixtures.py` is the only sanctioned source of test documents; building
them from an existing test is how each rejected round passed its own author's testing.

`tests/test_calls_browser.py` drives a real Chromium (Playwright) over the built React bundle
against a Hindsight mock. Every defect the three rounds found was visible by opening a page and
invisible to the API tests. Setup:

```bash
.venv/bin/pip install -r requirements-dev.txt && .venv/bin/playwright install chromium
.venv/bin/python -m pytest tests/ -q
```

Changing the React UI means `cd ui && npm run build` - `static/` is committed and served, and it
is the ONLY frontend since ticket 15. Delete `static/assets/*` first and commit exactly what the
build produced: `static/assets/` must end up holding exactly one `.js` and one `.css`, and the
script/link lines in `static/index.html` are the build's to write, never yours.

`tests/test_agents_browser.py` does the same for the Agents screen. It adds a third habit worth
keeping: **assert the rendered colour, not only the class name.** Ticket 02's held-back first
attempt rendered a dead Outlet in the same plain white as a healthy one; a test reading only a
class attribute passes against a stylesheet that never uses it. The screen exposes `data-testid`
hooks per slot (`slot-<outlet>-<direction>`, `slot-agent-…`, `slot-warning-…`, `slot-select-…`,
`outlet-card-<outlet>`) so an assertion can address ONE slot instead of the page.

`tests/test_agent_wizard_browser.py` covers the creation wizard the same way, and adds one habit
of its own: **assert the disk, not only the screen.** The thing that wizard must never do is leave
something behind, and no selector can be asked about that.

**When you sabotage, check the tree afterwards.** A sabotage run killed part way through leaves
its mutation in place, and every later row is then measured against a baseline that already
carries a defect - which looks like extra failures, not like a broken harness. `git diff` before
trusting a mutation table, and account for every hunk.

Two habits these tests are written to keep, both learned from tests that passed while the screen
lied: **assert the cell, not the row** (checking that "not retained" appears somewhere in a row
stays true while the outcome cell alone lies), and **assert the behaviour, not the last defect's
wording** (a banner that must not appear is checked by `.banner-warn` being invisible, not by
grepping for the sentence the previous round used). Where a constant is the contract - the fetch
bounds, the screen's page size - assert the constant too, against its source
(`test_calls_matrix.test_the_matrix_page_size_still_matches_the_screen` reads it out of
`App.tsx`); a behavioural test scaled to a constant cannot notice the constant going away.

The browser fixtures live in `tests/conftest.py` and the Hindsight mock in
`tests/browser_harness.py` (its docstring is the bank-spec language: healthy, an HTTP status, absent,
`ignore_offset`, `no_by_id`, `recall_ghost_hits`). Reading `stack.base` re-points the dashboard at
that store, which is what makes a test holding **two** stacks - a complete read beside a bounded one -
compare two stores rather than one store with itself. **The `browser` fixture must stay module-scoped.**
Playwright's sync API runs its own event loop, and holding it open past the last browser test leaves
`asyncio_mode = auto` handing that loop to later modules' async tests, which then never await their
coroutines - 16 unrelated failures in `test_cascade.py`, from a one-word scope change.

## The UI is one palette and a few shared pieces

`ui/src/index.css` defines every colour once, as tokens on `:root` (Vapi's dashboard palette:
teal accent, orange for the caller's channel); no rule below that block names a raw colour, and
no component carries an inline colour. `tests/test_css_source.py` fails on an unclosed or nested
rule - the bundle builds either way, and an open `.spin {` once silenced every Schedule style.
The browser suites pin rendered colours (`BROKEN_RED`, `PROVEN_GREEN`, ...) to these tokens, so a
palette change moves those constants in the same commit. Shared vocabulary is `ui/src/format.ts`
(absence wording, dates, Outlet names) and `ui/src/ui.tsx` (its components); routing is
`router.ts` plus `Link.tsx` over the History API.

The Calls list is rows, not a table: address a row by `data-call-id` and one field by
`data-field`, through `tests/calls_page.py`, never by the row's text. A Call opens at
`/calls/<id>`; the per-Agent voice form lives at `/agents/<id>/voice` and is keyed by Agent id so
switching Agents can never show the previous Agent's draft.

## Write screen tests against the fixture matrix

`tests/fixture_matrix.py`, driven by `tests/test_calls_matrix.py`. Six review rounds each closed the
defect they were handed and left the same class alive one element to the left, because **no scenario
ever combined two dimensions at once**: fixtures were 250 documents *or* mixed-content, never both,
and never exactly one page. Three defects sat at an intersection of exactly two dimensions.

The six dimensions are corpus size (including **an exact multiple of the page size** - the two
screens page differently, so 20 and 100 both matter), content mix (calls only, or calls among the
ordinary Hermes memories that share the `hermes` bank), read completeness (whole, or stopped by a
bound), bank health (all answer, one 503, one 404), **document generation** (pre-05 metadata,
post-05 metadata, or both in one list - which is what the live store looks like from the ticket-05
deploy onwards), and timestamp shape (ISO with an offset, naive, epoch, epoch string, date-only,
absent). That module lists which intersections are covered and which
are deliberately not. **Add a corpus there, not in your test**, and when you find a defect, ask first
which intersection hid it and add that cell.

**The store serves `document_metadata`; the retainers POST `metadata`.** They are not the
same key and a returned document has no `metadata` at all - on either bank, on the list
endpoint and on fetch-by-id. Read it through `hindsight_calls.doc_metadata`. Reading the POST
key shipped a dashboard that rendered EVERY retained field ("not retained") over a store that
held them, and every fixture here missed it because they are all built from the producers'
POST bodies. `hindsight_producer_fixtures.as_store_returns` converts one to the served shape,
`browser_harness` puts every browser-test document through it, and
`tests/test_live_document_shape.py` pins the reader directly. **A test that exercises the
reader must go through the served shape**; a POST-shaped fixture proves nothing about what the
screens show. One difference is deliberately still not modelled and is written down in
`as_store_returns`: the live LIST endpoint omits `original_text`.

**Ask the same question one level up, every round.** The matrix inherits the failure mode it exists
to prevent: it gets built from the *last* round's defects. Round 7's two findings were exactly that -
one sat in a cell the matrix declared (`truncated-*`, "a floor, not a count") while the per-cell test
opted out of the count assertion for precisely those cells, and the other sat on the axis the matrix
did not yet have. So before adding anything, ask **what does it declare and not check, and what axis
does it still not have**, and write the answer down in `fixture_matrix.py` even where you decide not
to build it. Prefer assertions that state a property ("the count carries a qualifier", "the year on
screen is the year in the store") over greps for the last defect's sentence - the greps are what rot.

## One placement, and the Schedules that use it (tickets 09 + 11)

`place_call.py` is the WHOLE of placing an outbound Call from this service - validation, the 409
for an Agent that cannot run outbound, the dial, and the grading of the bridge's answer, all in
`place_from_request`, refusing with a `PlaceRejected` that carries both the HTTP status and the
sentence the screen shows. `POST /api/calls/place` is an HTTP wrapper around it. **The scheduler
calls the same function**, which is the ticket-11 rule: a scheduled Call travels the identical
path, not an equivalent one. Anything you add to the route rather than to that function is a
manual-only behaviour. Two tests go red for it, and they are the first ones to run after touching
either side: `test_scheduler_fire.py::test_the_scheduled_dial_is_byte_identical_to_the_manual_one`
(the request that reaches the bridge, compared) and `::test_both_paths_go_through_the_one_placement`
(the one function replaced, both callers watched).

`schedules.py` is the memory (one YAML file per Schedule under `$VOICE_CONFIG_DIR/schedules/`,
plus the `O_CREAT|O_EXCL` `.claim` that makes firing happen once) and `scheduler.py` is the loop,
started by the app's lifespan. **`TestClient(app)` does not run the lifespan and a plain
`create_app()` has no scheduler running** - which is why the API tests can drive `tick()` by hand,
and why the browser `Stack` (real uvicorn) DOES fire schedules. Both are deliberate; a test that
needs the loop uses `with TestClient(app) as client:`.

Reasons on a failed Schedule are the manual path's own words, because both come from the same
`PlaceRejected`. Do not paraphrase them into the screen: `test_the_reason_a_fired_schedule_failed_is_what_the_screen_would_say`
compares them verbatim.

## `static/` is committed, and it is ONE bundle with every screen in it

`ui/` builds to `static/`, which is committed and served. That bundle is a
single JavaScript file containing Calls, the call page, Agents, the Agent page,
the wizard, New call, Settings and Schedule together, and **no Python suite builds it** - so a `static/` merge
conflict resolved by picking a side ships a dashboard missing an entire feature
with every test still green. It has come within one resolution of happening
three times.

**The protocol when `static/` conflicts: delete BOTH sides of
`static/assets/`, run `npm run build` in `ui/`, and commit exactly what that
produced.** Never hand-resolve the `src`/`href` lines in `static/index.html` -
the build regenerates them. Afterwards `static/assets/` holds exactly one `.js`
and one `.css`; more than that means something was hand-resolved.

`tests/test_static_assets.py` is the guard, and it reads the COMMITTED tree
rather than building one: referenced assets exist, nothing is unreferenced, and
the bundle carries a marker from every screen. **A new screen must be added to
its `SCREEN_MARKERS`** - a fourth test cross-checks that list against the
`screen === '…'` branches in `App.tsx`, so a screen cannot fall outside the
guard quietly.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
