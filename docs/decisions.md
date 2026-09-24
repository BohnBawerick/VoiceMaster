# Decisions: Voice Control (2026-08-17)

Grilled in one session on 2026-08-17. **These supersede everything.** The previous decision log
(D1 to D15) is void and is not part of this repository.

Numbered `VC*` rather than `D*` so the two sets can never be confused in a commit message or a
build log.

## What this is

- [ ] **VC1** Voice Control is the **interface layer** for phone calls made by Hermes personas,
      not the driver. The normal path is telling the Hermes orchestrator "get agent X to call
      this person"; the site is where you watch it, review it afterwards, and do it by hand when
      you want to - why: the previous build assumed the dashboard was the product and organised
      every screen around the machine's model, which is why it is unusable.

- [ ] **VC2** An **Agent is a Hermes profile**, not a voice config that references one. See
      `docs/adr/0001-agents-are-hermes-profiles.md` - why: separate beings with their own soul,
      tools and memory is a capability Hermes already ships and runs in production.

- [ ] **VC3** Calls arrive and leave on **Outlets**. There are two, the phone number and
      Nextcloud Talk, and exactly one Agent is assigned to each, visible on the site at a glance.
      Talk voice **survives** - why: it is one of the two ways to reach an agent, and which agent
      is on which outlet was the single thing the owner could never tell from the old UI.

## The model

- [ ] **VC4** A **Mission** belongs to the **Call**, never to the Agent, and only outbound Calls
      have one. Inbound Calls have no Mission **and no standing brief**: the Agent is simply
      itself, honest about what it is and what it can do, and works out what is needed once the
      caller speaks - why: the same as picking up an unknown number. A standing brief would have
      made Mission mean two different things.

- [ ] **VC5** A Mission is authored three ways: typed in full; typed as a short prompt that the
      Agent elaborates into its own Mission; or **spoken aloud** to the Agent, which listens
      without replying and writes its own Mission from what it heard - why: typing a full brief
      for every call is the friction that stops you making the call.

- [ ] **VC6** Four screens: **Agents, Calls, Schedule, Settings**. Bench, Eval and the dry-run
      apparatus are deleted - why: they existed to evaluate provider combinations, an activity
      that produced no calls and burned the time that should have gone into the product.

- [ ] **VC7** **Progressive disclosure, nothing removed.** Every provider, voice and pipeline
      including cascade stays selectable under Advanced, with honest labels marking which
      combinations are proven and which were never tested. The default path is the one that works
      - why: the owner wants the options and hates the presentation. A menu where nineteen
      untested providers look as valid as the one proven one is the actual defect.

## Data

- [ ] **VC8** **API-first.** Calls, Agents and Schedules are an HTTP contract; the UI is one
      client and the Hermes skill is another. `vc.py` is rewritten against it, including a real
      schedule verb and one-shot firing that does not mutate a global pointer - why: the current
      global "active pointer" that Hermes must set before firing is racy by construction and is
      the prime suspect for the dead inbound number.

- [ ] **VC9** **Hindsight is the call store**, not a copy of one. A dedicated `voice` bank holds
      one document per Call: verbatim transcript as `original_text`, with Agent, Outlet, Mission,
      outcome and summary as metadata. Search is Hindsight's own recall. **No second database, no
      new container, no duplication** - why: verified live on 2026-08-17, Hindsight already holds
      every transcript, returns them verbatim, pages documents and does semantic search. A
      dedicated bank keeps voice transcripts from diluting the main `hermes` bank.
      Amended by VC25: Hindsight is now one of two archive backends.

- [ ] **VC10** The per-call **summary is written at teardown by the Agent's own profile gateway**
      and stored in the document metadata - why: written by the being that was actually on the
      call, and instant when the Calls screen loads. Hindsight's own extraction produces durable
      facts for recall, which is a different artefact from "what happened on this call".

- [ ] **VC11** **We record the audio ourselves. Twilio is not involved.** Both legs already pass
      through our process on both Outlets (the media-stream websocket for the phone, the
      PulseAudio taps for Talk). One file per call, **stereo Opus**, caller on one side and agent
      on the other, stored on a volume and played in the page with seeking - why: no
      per-minute recording fee, nothing stored on Twilio's servers, and the two legs stay
      separable for re-transcription without costing a second file.

- [ ] **VC12** **AI disclosure is a toggle**, not a hardcoded behaviour. Australian recording law
      is not a blocker for now because testing runs against the owner's own numbers - why: owner
      decision, taken knowingly. Revisit before calling anyone who has not consented.

- [ ] **VC13** **No authentication.** Tailnet-only exposure is the whole perimeter; no basic auth,
      no login. Already the status quo since 2026-08-01, reaffirmed 2026-08-17 **in full knowledge
      that the rebuild adds recordings of personal calls** - why: owner decision, single user, own
      network, and the auth popup was in the way. Recorded here so nobody "fixes" it later without
      asking. Amended by VC26: the public example turns login on; the code keeps both.

- [ ] **VC14** **Scheduling lives in this app**, with its own scheduler. One attempt at the
      appointed time, honest status, no retries or escalation yet. Hermes can create a schedule
      but is never responsible for remembering it - why: schedules are this app's data, it keeps
      working when Hermes restarts, and a scheduled call travels the identical code path as a
      manual one. Retry policy is where scheduling projects drown; decide it after watching some
      fail.

- [ ] **VC15** **Profile discovery becomes dynamic, in the Hermes repo.** `hermes-supervisor.sh`
      globs `~/.hermes/profiles/*/` instead of reading `HERMES_PROFILES`, and the bridges read
      gateway URLs from a file this app writes instead of `HERMES_PROFILE_GATEWAY_URLS` - why:
      otherwise creating an Agent requires redeploying the stack, which bounces the live phone line, and
      "create the agent real quick" becomes a deploy.

## Build

- [ ] **VC16** The front end is **rebuilt from scratch** in **React + Vite + TypeScript**, built
      to static files that FastAPI serves exactly as it serves the current ones - why: a wizard
      with conditional branches, an audio player with seeking, search-as-you-type and a schedule
      view is past what hand-rolled DOM code handles without becoming the mess being escaped.
      Restructuring the existing templates cannot fix an information architecture that needs
      inverting.

- [ ] **VC17** The **byte-identical twins are collapsed into one shared package** installed into
      every image. The duplication of `eventlog.py`, `hindsight.py`, `profiles.py` and the cascade
      modules ends, and the anti-drift tests go with it - why: the rule was never a design choice,
      it was a workaround for having no shared package, and it produced two separate entries in
      the old gotchas index including images silently shipping stale copies.

- [ ] **VC18** The three existing agent YAMLs (`sample-realtime`, `girlfriend-caller`,
      `supplier-caller`) are **deleted, not migrated** - why: they were worked examples written to
      satisfy a build criterion, not agents in use. Everything starts again as real profiles.

- [ ] **VC19** A **speed dial** list of numbers, with the owner's own number in it by default -
      why: the first weeks of use are testing against the owner's own phone, and typing a number every
      time is friction.

- [ ] **VC20** The product is named **Voice Control**. The repo directory stays `VoiceMaster` for
      now - why: renaming the directory is a separate mechanical job with cross-repo references
      (deployment docs and registry rows). Amended 2026-09-24: the public release is named
      VoiceMaster, the name the repository already had.

- [ ] **VC21** The **publishable-plugin ambition is parked**. Build the tool, make it work against
      the Hermes that exists. Revisit later; research other plugins when there is a reason to -
      why: it is the kind of ambition that adds accounts and abstraction to a single-user tool
      that does not work yet. Revisited 2026-09-24: the tool works, and it is published as
      VoiceMaster under MIT, with the Hermes-side pieces in `hermes/`.

- [ ] **VC22** **The live phone line keeps working throughout.** The current system stays up while
      its replacement is built, and inbound only moves when the new path demonstrably answers a
      real call - why: the same reasoning that protected the number during the cascade build, and
      more so now, because far more is changing.

- [ ] **VC23** **Every pre-2026-08-17 design and decision document is void** and archived outside
      this repository, with the operational facts extracted first - why: stale
      plans get picked up and resumed, by agents especially. Git history is the archive; the
      directory exists only so a live incident can be diagnosed without archaeology.

- [ ] **VC24** **An Agent can be Hermes itself on a call, with no OpenAI Realtime model in
      between** (2026-09-20). Deepgram hears, the Agent's own `hermes_profile` thinks and acts,
      ElevenLabs speaks: a cascade Agent whose llm stage is the registry provider `hermes-agent`.
      It is an Agent type, chosen in Settings, not a new default; who answers an Outlet is still
      the Outlet assignment - why: the owner could choose what they were talking to but could
      never talk to Hermes, only to a model that called Hermes as a tool. See
      `docs/adr/0002-hermes-as-the-llm-stage.md`.
      The seven calls the owner made on 2026-09-20, which this build follows:
      one deploy for the whole rebuild plus this lane; it answers inbound on the phone **and** on
      Talk, owner only on Talk; a slow turn reuses the tool path's timings (a line at the
      debounce, reassurance every 10 s, an apology at 60 s); a call gets a faster model through a
      Hermes `model_routes` alias named by `knobs.model`; a listed caller gets Hermes's full
      tools with no confirm-first instruction; if the profile's gateway is down at pickup the
      call is answered on the Realtime lane and logged loudly; each Call is its own Hermes
      session, long-term memory on.
      Three rules came out of the build and are load-bearing. **Inbound cascade is the direct
      lane only**: an outside-vendor cascade still has no inbound prompt, caller identity or
      tool policy, so it stays outbound-only. **The dialing endpoints' bearer is fail-closed**:
      with full tools behind it, an unset `HERMES_GATEWAY_TOKEN` refuses every caller where it
      used to skip the check. **The Agent's tools setting is `guardrails.on_call_tools`, and the
      direct lane sends it as `tool_choice` on every Hermes turn** (amended 2026-09-20): `none`
      when it is off, `auto` when it is on, on both Outlets and in both directions. This replaces
      the first build's rule that an outbound direct call needed the setting on because "no
      request field removes Hermes's tools". Hermes now enforces `tool_choice`, so a tools-off
      direct call is valid, and `tools: []` is never the mechanism. Summaries and Missions stay
      `tool_choice: none` through the same field.
      D3 ("the live inbound line stays realtime") is void under VC23; nothing is reversed.

## Open-source release (2026-09-24)

- [ ] **VC25** **The call archive is pluggable.** With `HINDSIGHT_URL` unset, calls are archived to
      a SQLite file beside the event log; with it set, to the Hindsight `voice` bank as VC9
      describes; `VOICE_ARCHIVE` forces either. The dashboard reads whichever is active, and the
      document shape is the same in both - why: requiring a Hindsight server made the quick start
      impossible for anyone without one, while the original install keeps Hindsight with no
      configuration change because it already sets `HINDSIGHT_URL`.

- [ ] **VC26** **The public example is the careful one; the code keeps every behaviour.**
      `.env.example` turns dashboard login on, allows outbound calls only to the owner's number
      and turns the AI disclosure on. Allow-any dialing (empty `VOICE_OUTBOUND_ALLOWED_NUMBERS`),
      no login (VC13) and disclosure off (VC12) remain one variable away - why: those were
      single-owner, own-network choices, and the first thing a stranger's install would get
      wrong. The original install sets its own values explicitly.

- [ ] **VC27** **One public repository, one private deploy repository.** Code, docs and the
      Hermes add-ons are public; an install's hostnames, stack files, deploy notes and ledger live
      in that install's own private repository, and no secret value lives in either - why: the
      public tree must be safe to read in full, and the owner's exact setup must survive the
      split unchanged.

## Open

- **O1** Nextcloud Talk audio capture is expected to work through the existing PulseAudio taps
  (`talk_speaker.monitor` and `talk_mic_sink`) but has not been proven end to end.
- **O2** Retry and escalation policy for failed scheduled calls - deliberately deferred (VC14).
- **O3** Repo directory rename from `VoiceMaster` to match the product name (VC20).
- **O4** Recording consent for third parties, if calls ever go beyond the owner's own numbers
  (VC12).
- **O5** Closed 2026-09-24 for the phone Outlet. The direct Hermes lane (VC24) has carried real
  inbound and outbound phone calls, and an inbound call on 2026-09-24 (540 s, ElevenLabs Scribe,
  Hermes, ElevenLabs) passed by ear. Settings labels it proven. No Talk call has carried it yet,
  so its Talk acceptance is still open.
- **O6** Hermes at the pinned upstream commit does not honour `tool_choice` on a request. The
  listen-only guard on per-call summaries and Mission authoring (`hermes_gateway.ask_chat`) sends
  `tool_choice: none` and may therefore be inert. Read from source, not tested live. It needs a
  Hermes change, offered upstream alongside the add-ons in `hermes/`.
