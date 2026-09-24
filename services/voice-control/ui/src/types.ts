export interface Call {
  call_id: string;
  when: string;
  /* "datetime", "date" (only a date was retained -- this call has no clock
     time in the store), or null when nothing was retained. */
  when_precision?: 'datetime' | 'date' | null;
  direction: 'inbound' | 'outbound' | string;
  who: string;
  caller?: string;
  target?: string;
  agent?: string;
  outlet?: string;
  mission?: string;
  outcome?: string;
  summary?: string;
  /* Ticket 06: WHY there is no summary, when there is none. "written" (a summary
     is present), "nothing_to_summarise" (the call held no conversation to
     describe), "unavailable" (the Agent was asked and could not answer), or
     absent entirely (nobody was asked). The screen renders the three absences
     differently -- none of them as something that reads like a summary. */
  summary_state?: string | null;
  /* Seconds. null/undefined means nothing was recorded -- NOT a call of zero
     length. Every call retained before ticket 05 is in that state. */
  duration_s?: number | null;
  transcript?: string;
  /* Ticket 07: the Call's pointer to its own audio, as a producer wrote it. A
     pointer, not proof - `CallDetailResponse.recording` is what decides whether a
     player is shown, because it asks the recordings volume. */
  recording_ref?: string | null;
}

/* What the screen may offer in the Agent / Outlet filters. Built by the API from
 * the calls it actually read, so an offered filter always has calls behind it.
 * UNKNOWN_FILTER is the bucket of calls where the field was not retained; it must
 * match hindsight_calls.UNKNOWN_FILTER on the server. */
export const UNKNOWN_FILTER = '(not retained)';

export interface CallFilters {
  agent: string | null;
  outlet: string | null;
}

export interface CallsResponse {
  calls: Call[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
  unreachable: boolean;
  error?: string | null;
  /* Some bank could not be read (or was cut short). The calls that WERE
   * fetched are in `calls`; `warning` names what is missing and why. */
  partial?: boolean;
  warning?: string | null;
  agents?: string[];
  outlets?: string[];
  filters?: CallFilters;
}

export interface CallDetailResponse {
  call: Call | null;
  unreachable: boolean;
  error?: string | null;
  partial?: boolean;
  recording?: CallRecording | null;
}

/* Ticket 07: what the API resolved about this Call's audio, against the volume.
 * `available` false with `status` "failed" is a call whose capture broke - worth a
 * sentence. `available` false with no status is a call that simply has no recording
 * (an older call, or one that was never captured), which shows nothing at all. */
export interface CallRecording {
  available: boolean;
  status?: 'ok' | 'failed' | null;
  error?: string | null;
  url?: string | null;
  duration_s?: number | null;
  size_bytes?: number | null;
  dropped_frames?: number;
}

/* ---------------------------------------------------------------------------
 * Agents screen (ticket 02, on the Outlet axis ticket 16 landed).
 *
 * These mirror the API exactly. Nothing here re-derives an assignment from a
 * call direction: `ActiveResponse.outlets` IS the per-Outlet truth the bridges
 * resolve, and `slot_warnings` names the slots that are dead.
 * ------------------------------------------------------------------------ */

export type Direction = 'inbound' | 'outbound';

/* One row of GET /api/agents. */
export interface Agent {
  id: string;
  description: string;
  hermes_profile: string | null;
  enabled: boolean;
  pipeline: string | null;
  direction: string | null;
  /* s14: the Agent's own voice settings, so the Settings editor can seed from
     THIS Agent instead of the proven default. Empty maps = nothing set. */
  providers: Record<string, string>;
  /* Knobs are not all strings: keyterms is a list, smart_format and numerals are
     booleans, temperature and speed are numbers. Read them through a narrowing helper. */
  knobs: Record<string, unknown>;
  guardrails?: { on_call_tools?: boolean } | null;
  /* VC24: what the person is talking to ('hermes-direct' | 'realtime' | 'custom'),
     and whether the Hermes profile this Agent drives can be reached right now. */
  agent_type: string;
  hermes_routable: boolean;
  /* outlets[outlet][direction] === true when THIS agent holds that slot. There
     is no flattened view any more (ticket 17): a per-direction flag would have
     to pick an Outlet to mirror, and that guess is what made the old screen
     dangerous. */
  outlets: Record<string, Record<Direction, boolean>>;
  valid: boolean;
  errors: string[];
}

/* GET /api/active and PUT /api/active both answer with this. */
export interface ActiveResponse {
  /* outlets[outlet][direction] = the agent id assigned there, or null for
     "no Agent selected", which means the default Hermes answers. */
  outlets: Record<string, Record<Direction, string | null>>;
  /* profiles.OUTLETS, in model order. The screen renders these, so a third
     Outlet appears without a frontend change. */
  outlet_order: string[];
  /* Every warning, flat. A slot-attributable one also appears in
     `slot_warnings`; the rest are page-level (a whole unreadable file). */
  warnings: string[];
  /* slot_warnings[outlet][direction] = why that slot is dead, or null. */
  slot_warnings: Record<string, Record<Direction, string | null>>;
  /* When set, VOICE_AGENT overrides the pointer on every Outlet at runtime and
     what is stored here is inert. */
  voice_agent_env: string | null;
}

export interface AgentsScreenData {
  agents: Agent[];
  active: ActiveResponse;
}

/* ---------------------------------------------------------------------------
 * The Agent creation wizard (ticket 13).
 *
 * Creating an Agent creates a BEING: a real Hermes profile directory (ADR
 * 0001), not a document in this app. `HermesState.available` is therefore a
 * first-class answer -- on a deploy where the profiles directory is not
 * mounted into this service there is no honest way to create one, and the
 * screen has to say which compose line is missing rather than write a profile
 * nothing will run.
 * ------------------------------------------------------------------------ */

export interface HermesProfileRow {
  name: string;
  complete: boolean;
  incomplete: boolean;
  /* From `gateways.json`, which the Hermes side writes. null means nothing has
     told us which gateways are running -- never "no profiles exist". */
  gateway_status: string | null;
  gateway_url: string | null;
  gateway_error: string | null;
}

export interface HermesState {
  available: boolean;
  profiles_dir: string | null;
  reason: string | null;
  deploy_hint: string | null;
  profiles: HermesProfileRow[];
  registry_path: string;
  memory_modes: string[];
  tool_profiles: string[];
  /* What the wizard must preselect. Served, not hardcoded in the screen, so
     the preselection and the thing the backend calls proven cannot diverge. */
  proven: { pipeline: string; realtime_provider: string };
  /* The realtime-lane voice options, served from the ONE source
     (settings_catalog), so the wizard and the Settings page offer the same
     list. Proven first, alternatives after. */
  realtime_voices: LabeledVoice[];
  /* VC24: what the profile picker offers. `default` first, then every profile
     directory. Served even when creation is unavailable. */
  selectable: SelectableProfile[];
}

export interface SelectableProfile {
  name: string;
  complete: boolean;
  gateway_status: string | null;
  gateway_url: string | null;
  routable: boolean;
}

/* VC24: a preset over pipeline/providers. It invents no new field on an Agent. */
export interface AgentType {
  id: string;
  name: string;
  summary: string;
  pipeline: string;
  providers: Record<string, string>;
  /* Ticket 21: the ears and the voice this type may have, the default first. */
  stt_options?: string[];
  tts_options?: string[];
  proven: boolean;
  evidence: string;
}

/* What "inherit from this one" would take. Never carries a credential: the
   backend does not read a profile's .env to build it. */
export interface Inheritable {
  profile: string;
  exists: boolean;
  identity_name: string | null;
  model: string | null;
  model_provider: string | null;
  tools_profile: string | null;
  mcp_servers: string[];
  memory_shared: boolean;
  telegram_enabled: boolean;
  soul: string;
  skills: string[];
}

export interface ProviderRow {
  id: string;
  role: 'realtime' | 'llm' | 'stt' | 'tts' | string;
  display_name: string;
  status: string;
  default_knobs: Record<string, unknown>;
  /* Ticket 14: the honest classification, served by the backend. `proven` is a
     real-call fact; `wired` says a cascade client exists (which is NOT the same
     thing); `evidence` is the reason rendered next to the label. */
  proven: boolean;
  wired: boolean;
  evidence: string;
}

export interface VoiceCatalog {
  source: 'curated' | 'account' | 'unavailable' | string;
  default: string | null;
  voices: { id: string; name: string }[] | null;
  detail?: string;
}

/* The wizard's one request body. Everything optional is a question that was
   not answered, and the backend fills it the same way whether the operator
   skipped the step or the step was never shown. */
export interface CreateAgentRequest {
  name: string;
  description?: string;
  inherit_from?: string | null;
  inherit_skills?: boolean;
  identity_name?: string;
  soul?: string;
  model?: string;
  model_provider?: string;
  tools_profile?: string;
  memory?: string;
  telegram_connect?: boolean;
  telegram_bot_token?: string;
  telegram_dm_policy?: string;
  pipeline?: string;
  providers?: Record<string, string>;
  knobs?: Record<string, string>;
  persona?: string;
}

export interface CreateAgentResponse {
  agent: Record<string, unknown>;
  profile: {
    name: string;
    path: string;
    inherited_from: string | null;
    telegram_connected: boolean;
  };
}

/* ---------------------------------------------------------------------------
 * Place a call (ticket 09). One-shot: Agent + number + Mission + disclose.
 * Firing does not mutate any Outlet assignment.
 * ------------------------------------------------------------------------ */

export interface SpeedDialEntry {
  label: string;
  number: string;
  owner: boolean;
}

export interface SpeedDial {
  owner: string | null;
  numbers: SpeedDialEntry[];
}

export interface PlaceCallRequest {
  agent: string;
  to: string;
  mission: string;
  disclose: boolean;
  target_display?: string;
}

export interface PlaceCallResponse {
  placed: boolean;
  call_sid?: string | null;
  call_id?: string | null;
  agent: string;
  to: string;
  mission: string;
  disclose: boolean;
}

/* Ticket 10: the Agent writes a Mission. The screen puts this in the
 * editable field; it is never placed unseen. */
export interface AuthoredMission {
  mission: string;
  agent: string;
  hermes_profile: string;
}

/* ---------------------------------------------------------------------------
 * Settings screen (ticket 14).
 *
 * One consolidated page: providers, voices, Outlet assignment and speed dial.
 * The proven/untested labels are SERVED by the backend (settings_catalog.py)
 * so the screen can never invent a more flattering label than the evidence
 * supports - the API and the page disagreeing is the exact drift this shape
 * exists to prevent.
 * ------------------------------------------------------------------------ */

export interface LabeledPipeline {
  id: string;
  proven: boolean;
  evidence: string;
}

export interface LabeledVoice {
  id: string;
  proven: boolean;
  evidence: string;
}

/* Speed dial is ticket 09's (PR #18) - the Place-a-call screen dials from it.
 * The Settings card reads the SAME shape, so the two pages cannot disagree
 * about the list a call path depends on. */
export interface SettingsSpeedDial extends SpeedDial {
  /* When set, the speed-dial file could not be read; the card degrades with
     this error instead of the whole Settings page failing. */
  error?: string;
}

export interface SettingsData {
  proven: { pipeline: string; realtime_provider: string; voice: string };
  pipelines: LabeledPipeline[];
  agent_types: AgentType[];
  providers: ProviderRow[];
  realtime_voices: LabeledVoice[];
  speed_dial: SettingsSpeedDial;
}

export interface UpdateVoiceRequest {
  pipeline?: string;
  guardrails?: { on_call_tools: boolean };
  /* Values are provider ids; an explicit null DELETES that provider key (the
     cascade lane must drop `realtime`, which is what `realtime: null` does). */
  providers?: Record<string, string | null>;
  /* An explicit null DELETES that knob, the same rule `providers` follows. */
  knobs?: Record<string, string | boolean | string[] | null>;
}

export interface UpdateHermesProfileResponse {
  agent: Record<string, unknown>;
  routable: boolean;
  gateway_url: string | null;
}

export interface UpdateVoiceResponse {
  agent: Record<string, unknown>;
}

/* ---------------------------------------------------------------------------
 * Schedules (ticket 11). A Schedule is a Call that has not happened yet.
 *
 * `due_at` is the instant it fires (UTC). `local_time` + `timezone` are the
 * wall clock it was written in, kept so the screen can show the promise that
 * was made rather than a time re-derived from the instant.
 * ------------------------------------------------------------------------ */

export type ScheduleStatus = 'pending' | 'placed' | 'failed' | 'cancelled' | string;

export interface Schedule {
  id: string;
  status: ScheduleStatus;
  created_at: string;
  due_at: string;
  timezone: string;
  local_time: string;
  utc_offset: string;
  /* Present only when that wall clock happened twice that day (daylight saving
     ending). The earlier of the two was taken. */
  ambiguous_local_time?: boolean;
  agent: string;
  to: string;
  mission: string;
  disclose: boolean;
  target_display?: string;
  label?: string;
  /* Settled Schedules only. `reason` is the honest sentence for a failure or a
     cancellation; `call_id` is the Call the archive holds. */
  settled_at?: string;
  reason?: string;
  failure_status?: number;
  call_id?: string | null;
  call_sid?: string | null;
}

export interface SchedulesResponse {
  schedules: Schedule[];
  now: string;
  /* The zone the service reads a bare local time in; null when it has none
     configured, in which case a Schedule must name its own. */
  timezone: string | null;
  /* How late a Call may still be placed before it is a failure instead. */
  grace_s: number;
}

export interface CreateScheduleRequest {
  agent: string;
  to: string;
  mission: string;
  disclose: boolean;
  /* Either a local wall time (with `tz`) or an instant carrying an offset. */
  at: string;
  tz?: string;
  target_display?: string;
  label?: string;
}
