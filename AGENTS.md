# AGENTS.md

Guidance for coding agents (and people) working in this repository.

Read these first, in this order:

1. **`CONTEXT.md`** - the glossary. Four terms carry the whole design.
2. **`docs/decisions.md`** - decisions VC1 onward.
3. **`docs/adr/`** - architecture decision records.
4. **`docs/configuration.md`** - how the services connect, the archive, the knobs.

If this checkout has an `_archive/` directory, it is void history from an abandoned earlier
build. Never build from it.

## What this is

VoiceMaster is the interface layer for phone calls made by Hermes Agent personas. Hermes is the
usual driver; this is where calls are watched, reviewed and driven by hand.

An **Agent is a Hermes profile** (`docs/adr/0001`). It is not a YAML file, not a voice config, and
not a persona this app invents. Creating one creates a Hermes gateway process.

`services/` holds the phone bridge (`voice`), the Talk bridge (`talk-voice-bridge`), the
dashboard (`voice-control`) and the shared package (`voicecore`). `hermes/` holds the Hermes-side
add-ons: the profile supervisor, the transcription route, the Talk plugin and two skills.

**Profile discovery is dynamic** (VC15): the supervisor in `hermes/` starts a gateway per profile
and writes `gateways/gateways.json`, so creating an Agent does not restart anything that is
answering the phone.

An install's own deployment (hostnames, stack files, deploy notes, the ledger of what is live)
does not belong in this repository. Keep it in that install's own private repository.

## General rules

1. Ask, don't assume. If something is unclear, ask before writing a line. When running unattended,
   pick the most reasonable interpretation, proceed, and record the assumption rather than
   blocking.
2. Simplest solution for simple problems, better solutions for harder ones. Don't over-engineer or
   add flexibility that isn't needed yet.
3. Don't touch unrelated code, but do surface bad code and design smells so they can be addressed
   separately.
4. Flag uncertainty explicitly. Where useful, run a small, localised, low-risk experiment and bring
   the hypothesis and result back to discuss.
5. Structural suggestions are always welcome.

## The rules that keep this from breaking

- **A live phone line stays working** (VC22). Change the path a real number answers on only when
  the new path demonstrably answers a real call.
- **`red -> green` is not evidence a test binds the fix.** Four separate times a fixture or
  monkeypatch agreed with the buggy code instead of with reality. **Sabotage each fix** (remove it,
  expect red) before trusting its test.
- **A hand-signed probe cannot validate a signing secret.** It passes regardless. Only a real call
  proves inbound.
- **Twilio signs with the region's auth token.** `TWILIO_SIGNING_TOKENS` (inbound) must stay
  decoupled from `TWILIO_AUTH_TOKEN` (outbound REST login). See `docs/twilio.md`.
- **Both dialing postures stay supported** (VC26). An empty `VOICE_OUTBOUND_ALLOWED_NUMBERS` is
  allow-any, and some installs run that way on purpose, with the gateway bearer plus the
  callee-side tool sandbox as the guard. The public example sets an owner-only list. Do not make
  either one impossible.
- **Placing a call is one-shot** (ticket 09). `POST /api/calls/place` names the Agent on the
  request; `/voice/outbound` binds that snapshot to the call_id. It does not write
  `active.yaml`. **There is no dry-run anywhere in this product**: a real call is the only way to
  find out whether a configuration works.
- **There is ONE placement, and a scheduled Call uses it** (tickets 09 + 11).
  `place_call.place_from_request` validates, refuses, dials and grades the answer;
  `POST /api/calls/place` is an HTTP wrapper around it and the scheduler calls it directly.
  Anything added to the route instead of to that function is a manual-only behaviour, which is
  the divergence ticket 11 exists to prevent - and two tests go red for it.
- **There is ONE site and it is served at `/`.** `services/voice-control/static/` is the built
  React bundle and the only frontend. The dashboard declares the cascade capability of BOTH
  bridges in `app.py` (`profiles.declare_cascade_host`).
- **`static/` is committed and is ONE bundle holding every screen.** A conflict there is
  resolved by deleting BOTH sides of `static/assets/` and rebuilding, never by picking a side -
  picking one ships a dashboard missing a whole feature with every suite green.
  `services/voice-control/tests/test_static_assets.py` guards it and lists the per-screen
  markers a new screen must be added to. CI rebuilds the bundle and fails if it differs.
- **Dashboard login is optional, and both states are supported** (VC13, VC26). Basic auth turns on
  when `VOICE_DASHBOARD_USER` and `VOICE_DASHBOARD_PASSWORD` are both set; the public example
  sets them. Some installs run without login behind a private network on purpose; do not remove
  that option.
- **Nothing is removed from the options, only reorganised** (VC7). Cascade and every provider stay
  selectable under Advanced, with honest labels about what is proven.

## The shared package

`services/voicecore/` holds `eventlog.py`, `hindsight.py`, `call_store.py`, `call_record.py`,
`summary.py`, `profiles.py`, `probes.py`, `outbound_request.py`, `dial_request.py`,
`hermes_gateway.py`, `hermes_voice.py`, `mission.py` and the cascade modules. Every service
imports them (`from voicecore import profiles`); each image pip-installs the package editable
from the `services/` build context. There is one copy of each module (VC17). Edit
`services/voicecore/` and every service gets it.

Off-call traffic to an Agent's Hermes gateway (per-call summary and Mission authoring)
goes through `voicecore/hermes_gateway.py` only. Do not add a second HTTP client in
`summary.py`; a bare body there lets the Agent fire tools while summarising a finished
call. ON-call traffic (the direct lane, below) goes through `voicecore/hermes_voice.py` only,
and it is a separate module on purpose: that client has to let the Agent act, so it must never
become a flag on the listen-only guard.

**There is ONE profile resolver**, `hermes_gateway.gateway_url_for_profile`: the
`HERMES_PROFILE_GATEWAY_URLS` map, then `default`, then the `gateways/gateways.json` the Hermes
supervisor writes under `VOICE_CONFIG_DIR`. Only a `status: ok` entry routes; anything else
resolves to nothing and never borrows the default backend. Both bridges delegate to it. A local
copy is how a profile became reachable for Mission authoring and unreachable on a call.

Still hand-copied, deliberately: `tests/test_s15b_fixture_guard.py` and the s15b-A block of
`tests/profile_helpers.py`, which each suite keeps its own copy of. That guard pins itself.

## Common operations

```bash
docker compose up -d --build                  # phone bridge + dashboard (README.md)
curl http://127.0.0.1:3737/healthz
cd services/voice-control && .venv/bin/python -m pytest tests/ -q
```

Development setup for each suite is in `CONTRIBUTING.md`. The Hermes add-ons' tests are in
`hermes/README.md`.

## The outlet axis (ticket 16)

`active.yaml` is per-Outlet: `outlets: {phone: {inbound, outbound}, talk: {inbound, outbound}}`,
modeled in `voicecore/profiles.py` (`OUTLETS`, `read_active_pointer`,
`load_effective_profile(direction, outlet=...)`). That is the ONLY shape - ticket 17 deleted the
pre-16 flat one, and a top-level `inbound`/`outbound` key now raises rather than being read past,
on disk and on `PUT /api/active` alike. Each bridge resolves its own outlet (phone bridge →
`phone`, talk bridge → `talk`), and a broken slot fails loud for that outlet only. The dashboard
surfaces broken slots: `GET /api/active` warns for structural faults, for slots that went stale
after storage (agent disabled, invalid, or deleted out of band), naming the field path, and for a
stray file that makes the whole `agents/` directory unreadable - unparseable, not a map, or a
duplicate `id:` - which stops every Outlet resolving its assigned Agent at once (after ticket 08
that means each Outlet answers from its snapshot or refuses, so the dashboard warning may be the
only signal). That check resolves an Agent by the `id:` inside the document, the way the bridges
do, never by filename, and every filename it prints is the file the Agent really came from. The
dashboard endpoints that read or write an existing Agent (`GET`/`PUT`/`DELETE /api/agents/{id}`,
`PUT /api/agents/{id}/voice`) resolve the same way - see `services/voice-control/CLAUDE.md`.

The Agents screen at `/agents` (ticket 02) is the ONLY surface for this: it shows each Outlet's
Agent per direction, marks a dead slot as dead, and writes assignments **one Outlet and one
direction at a time**. Do not teach a new screen the flat key.

## The call archive (tickets 05 and VC25)

Every call is archived as one document, to the backend `voicecore/call_store.backend` picks: the
SQLite file beside the event log by default, or a Hindsight bank when `HINDSIGHT_URL` is set
(`VOICE_ARCHIVE` forces either). The document is the same shape in both, and the dashboard reads
whichever is active through `services/voice-control/hindsight_calls.py`. An install that sets
`HINDSIGHT_URL` must keep getting Hindsight with no other change.

Every retainer builds its metadata through the ONE builder in `voicecore/call_record.py`, which
writes the Outlet, Agent, Mission, outcome and duration - and **omits any field it was not
given**. An absent key is the only way a document says "not recorded"; there is no `""`, no
`"unknown"`, no `0`, and the Outlet is never back-filled from `platform`. The Outlet recorded is
the constant the bridge resolved its Agent with (ticket 16), so it is a fact, not an inference
from the transport.

Retention is fire-and-forget and must stay that way: `retain_call` dispatches and returns without
awaiting the store, and nothing in that path may raise into the teardown `finally:` that ends the
call. When the detached write settles, its result is appended to the event log as a `retain`
record - that is where a failed archive write is visible, along with a WARNING in the container
log. The Calls screen cannot show it: a call that failed to be archived is not in the archive.

Hindsight answers 200-with-an-empty-list (not 404) for a bank it does not have, so a bank that
was never created looks like an empty archive.

## The last-known-good fallback (ticket 08)

A broken slot no longer goes dead: the Outlet answers with the profile that most recently
**completed a call** on it, keyed (outlet, direction) and recorded at teardown by the bridge that
owns the Outlet (`voicecore/lkg.py`). The fallback sits ONLY at the bridges' answering call sites
(`media_stream`, `CallSession.start`, both pre-dial gates); `profiles.py` and `VOICE_AGENT`
precedence are untouched. **A fallback answer is impossible to perform quietly**: each one
appends a `fallback` record to `<events-dir>/fallback_events.jsonl` (naming the Outlet, the
broken assignment and the snapshot) plus a WARNING line, and the pointer file is never touched -
the dashboard keeps showing the slot broken in red while the fallback answers. Snapshots are
`<events-dir>/lkg-<outlet>-<direction>.json` (persistent events volume, one writer each, atomic
replace); a pointer that never served a call is never recorded.

## The per-call summary (ticket 06)

The Agent that was **on** the call writes it, through that Agent's own Hermes gateway
(`hermes_profile` -> gateway URL, the same seam the in-call `hermes_agent` tool uses). There is
no global summariser and no second model. `voicecore/summary.py` holds the rules;
`call_record.apply_summary` writes the two fields; each of the four lanes passes one additive
`summariser=` to `retain_call`. The turn itself is `hermes_gateway.ask_chat` (listen-only,
`tool_choice: none`) so summarising a finished call cannot make the Agent act.

**It runs on the detached retain task, before the write** - never on the call path - and the
wait is bounded and total. A summariser that fails, hangs, errors or answers nothing costs the
document its `summary` key and nothing else; the transcript, the recording reference and every
ticket-05 field are still written complete.

**Nothing is invented.** A call is only summarised when both parties spoke, **the other party
said at least `MIN_CALLER_CHARS`**, and together they said enough to describe
(`summary.is_summarisable`); the guard runs BEFORE the gateway, so an unanswered call or one
hung up on the greeting never reaches a model that would happily write "the caller did not
speak". The caller's own floor is the load-bearing one: a combined floor is already cleared by
a real Agent greeting, so it let a greeting plus "oh" through - test any change to this guard
with a greeting copied from a real call, never one trimmed to sit under the threshold. An absent
summary therefore says WHICH absence it is, in `summary_state`: `nothing_to_summarise` (no
conversation to describe), `unavailable` (the Agent was asked and could not answer), or no key
at all (nobody was asked - the feature is off, or the call predates the ticket). An empty
summary is not producible. There is deliberately **no "still coming" state**: the summary is
settled before the document is written, so a Call is either absent from the archive or complete
in it, and the Calls screen has no spinner.

Knobs: `VOICE_SUMMARY_ENABLED` (default true) and `VOICE_SUMMARY_TIMEOUT_S` (default 30 - how
long the ALREADY-detached archive write waits before writing the call without a summary).

## Call recording (ticket 07)

Every call is captured to one stereo Opus file (caller left, Agent right) on a volume, and
played back inline on the Calls screen. `voicecore/recording.py` writes,
`voicecore/recording_store.py` reads; both sides import the same module so the layout cannot drift.

**The rule that shapes the module:** the call is the product, the recording is a by-product. The
live path only ever does a bounded, non-blocking, exception-free enqueue; every decode, pipe and
syscall runs on a writer thread; a full queue DROPS frames rather than waiting; teardown closes
the recording off the event loop (`recording.finish_async`). If you tap call audio for anything
else, tap it at the same four seams (phone realtime, Talk realtime, and `cascade_live` for both
cascade lanes) and keep that shape.

The Call's pointer to its audio is one additive `recording` field on `call_record.build_metadata`
(ticket 05's builder). It is a pointer, not proof: the dashboard resolves playability against the
volume, so a reference that outlives its file shows no player.

It needs `ffmpeg` in the bridge images (both Dockerfiles install it) and a writable recordings
volume. `VOICE_RECORDING_ENABLED=false` removes capture from the process with no rebuild.

## Mission authoring (ticket 10)

A Mission is still a string on the Call. On `/place` the operator can type it, or ask **that
Call's Agent** to write it (short line + Elaborate, or Record in the browser). Both assists go
through the Agent's own Hermes gateway - the same `hermes_profile` -> URL seam as ticket 06 -
and land in the editable field; they never dial. A failure leaves the typed text intact
(failure bodies have no `mission` key). Capture is `getUserMedia` + `MediaRecorder`; the Agent
is asked listen-only (`tool_choice: none`, no speech path). Dictation needs the transcription
route in `hermes/gateway_overlays`.

## Scheduling (ticket 11)

A **Schedule is a Call that has not happened yet**, and when its time comes it is placed by
`place_call.place_from_request` - the same function the button uses, not a parallel path
(`services/voice-control/scheduler.py`, `schedules.py`, screen at `/schedule`). It is still a
one-shot, so it must stay on the guarded side: a scheduled Call never writes `active.yaml` and
never becomes the Outlet's last-known-good.

**Firing exactly once rests on one atomic operation**: `schedules/<id>.claim`, created with
`O_CREAT|O_EXCL` under `$VOICE_CONFIG_DIR`. Whoever creates it owns that Schedule's ending, and
firing, cancelling and marking-it-missed all compete for it - which is how a cancel that arrives
at the due instant is decided rather than raced (the loser gets a 409, never a 200 that would
claim a ringing phone was stopped). A claim is never removed, so a Schedule is claimable once in
its life. **The scheduler holds no memory of what it fired**; that is why a restart is an
ordinary tick and not a special case. The one thing the filesystem cannot decide - a claim
written, then the process dies - is settled `failed` saying exactly that, never re-dialled.

**One attempt only** (VC14, open item O2). Nothing retries: a refused dial, an unreachable
bridge, a missed window and an interrupted claim all end `failed` with an honest sentence. A call
that was placed and then went unanswered is `placed`; whether anyone picked up is the **Call's**
outcome on the Calls screen, not the Schedule's.

**A duration knob that cannot work refuses the boot** (`scheduler.validate_knobs`, called from the
lifespan): zero, negative or non-numeric `VOICE_SCHEDULE_GRACE_S` / `_STALE_CLAIM_S` / `_TICK_S`
exits at startup naming the variable, rather than falling back to the default and looking
configured. Unset is always the default.

**Times are resolved once, at creation**: the instant is stored, with the zone and the wall clock
beside it. The spring-forward gap is refused; the autumn-back overlap takes the first of the two
and flags it. **A zone without daylight saving proves nothing here**, so never test this
against one alone - `tests/test_schedule_time.py` uses New York and London and says why.

## The direct Hermes lane (ticket 18, VC24)

An Agent can be Hermes itself on a call: ElevenLabs Scribe hears (Deepgram is the other choice),
the Agent's own `hermes_profile` thinks, ElevenLabs speaks, with no Realtime model in between. It
is a cascade Agent whose llm stage is the registry provider `hermes-agent`, chosen as an **agent
type** on the Agent's Voice tab (`/agents/<id>/voice`). It is not a default. It is **proven on
the phone Outlet**: real inbound and outbound calls have carried it. No Talk call has carried it
yet. `settings_catalog.py` (`DIRECT_LANE_EVIDENCE`) says the same. See
`docs/adr/0002-hermes-as-the-llm-stage.md`.

- **`profiles.CASCADE_CAPABILITY` names an Outlet and a direction.** Each bridge declares its
  own; the dashboard declares both. **Inbound cascade activates for the direct lane only** - an
  outside-vendor cascade has no inbound prompt, caller identity or tool policy and stays
  outbound-only. On Talk the lane is for the **owner**; a guest keeps the Realtime lane and the
  approval loop, and `trust` is compared exactly.
- **`guardrails.on_call_tools` is sent as `tool_choice` on every Hermes turn**: `none` when off,
  `auto` when on, both Outlets, both directions, retries included. Hermes enforces it; an absent
  field means full tools and `tools: []` enforces nothing. `HermesConversation` takes the value
  as a required argument and refuses anything but `none`/`auto`; it comes from `cascade_config`,
  so both bridges get it. A missing setting defaults to on for Hermes Direct only; explicit
  false and malformed values stay off. A tools-off direct Agent is valid outbound. The dialing
  endpoints' bearer is **fail-closed**: an unset `HERMES_GATEWAY_TOKEN` refuses every caller.
- **A dead model is never read aloud.** The client streams `/v1/chat/completions` (the one
  endpoint that reports `hermes.failed`) and holds the last sentence until the stream ends
  cleanly. Do not move it to `/v1/responses`, which completes a failed turn as ordinary text.
- **Hermes holds the history**, keyed `X-Hermes-Session-Id: voice-<call id>`. Nothing is resent.
  A barge before the reply starts is soft (the agent turn may be mid-action); over the reply it
  is hard, the stream is closed so upstream interrupts the agent, and the next turn says how much
  was heard.
- **The pickup check** (`hermes_voice.probe`, 1 s, one deadline) sits at the same answering call
  sites as ticket 08. Gateway down or profile unroutable: that one call is answered on the
  Realtime lane with the bridge's own defaults, a `fallback` record with `kind: lane` is
  appended, nothing becomes last-known-good, and the pointer is untouched. Outbound is refused
  instead: a Mission is never handed to a different being.
- `VOICE_HERMES_DIRECT_ENABLED=false` removes the lane with no rebuild. Set it on all three
  services.
- The two narrow writes the Agent page makes (`PUT .../voice`, `PUT .../hermes-profile`) refuse a
  change that would break a slot the Agent holds. The whole-document `PUT /api/agents/{id}` is
  deliberately still unguarded - see `services/voice-control/CLAUDE.md`.

## Cascade turn-taking (ticket 21)

The engine (`voicecore/cascade_live.py`, both bridges) owns turn-taking; the rules below are each
pinned by a sabotage-checked test in `services/voice/tests/test_turn_taking.py`, which runs on a
Twilio simulator that echoes marks at playout time.

- **`_playing` is cleared only by the mark of the burst now playing.** Twilio echoes a mark when
  its audio has played, which is after the next burst may have started. Clearing on any mark
  switched barge-in off for every sentence after the first.
- Barge-in fires from **every** detector state while the agent is audible; nothing the agent says
  starts while the caller is talking (`_hold_for_caller`); the direct lane's filler waits for
  Hermes tool progress or `HERMES_FILLER_WAIT_S`, and a filler cut short is `clear`ed.
- A caller who speaks again before any of the answer was heard, and before the turn started a
  tool, is **merged** into one turn (the first Hermes stream is closed). Never merge a turn that
  has acted.
- **End of turn comes from the STT provider**, not a model: Scribe writes `...`/`-` on speech that
  trailed off (`elevenlabs_live.turn_verdict`). A local end-of-turn model was measured at
  435-660 ms per verdict on a small NAS CPU and removed.
- One TTS socket per reply (`elevenlabs_live.TTSStream`, `eleven_flash_v2_5`), opened while Hermes
  thinks. Suites fake it with `tests/tts_fake.py`, which replays the old HTTP mocks.
- **Scribe closes its session on a commit over less than 0.3 s of audio**, so every commit is
  padded with silence (`MIN_COMMIT_S`). An STT session that ends unasked reconnects once, then
  is `lost`: an `stt` event-log record, and the call never becomes a last-known-good.

## Secrets

Never commit a secret, a real phone number, a recording or a transcript. `.env` is gitignored;
`.env.example` holds names and placeholders only. CI runs gitleaks over the full history on every
push. Tests use fictional numbers (`+61491570156`, `+61855501234`, `+1555010xxxx`).

## Agent skills

### Issue tracker

GitHub Issues on this repository. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default roles (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`,
`wontfix`), as GitHub labels. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` is the glossary, `docs/decisions.md` is the decision log, `docs/adr/`
holds ADRs. See `docs/agents/domain.md`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
