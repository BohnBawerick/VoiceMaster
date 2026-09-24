# Configuration

`.env.example` lists every setting a normal install touches, with a comment on each. This page
covers what a comment cannot: how the pieces connect, the call archive, and the knobs that are
there for when something misbehaves.

## Connecting to Hermes

VoiceMaster talks to Hermes Agent's OpenAI-compatible API server. Each Agent is a Hermes
profile, and each profile runs its own gateway.

- `HERMES_GATEWAY_URL` is the default profile's gateway. `HERMES_GATEWAY_TOKEN` is the bearer
  it expects, and also the bearer the phone bridge requires before it dials.
- Profiles are found in this order, first match wins (`voicecore/hermes_gateway.py`,
  `gateway_url_for_profile`): the `HERMES_PROFILE_GATEWAY_URLS` map (`name=url,name=url`), then
  `default`, then `gateways/gateways.json` under `VOICE_CONFIG_DIR`, which the profile
  supervisor in `hermes/` writes. Only an entry with `status: ok` routes; a profile that does not
  resolve is refused, never sent to the default profile.
- With the supervisor running, creating an Agent on the dashboard creates the profile directory,
  the supervisor starts its gateway within a scan interval, and the Agent is callable. No restart.
  Point the supervisor's `HERMES_GATEWAY_REGISTRY` at `data/voice-config/gateways/gateways.json`.
- Mission dictation (speaking a Mission to the Agent) needs the transcription route from
  `hermes/gateway_overlays`.

## The call archive

Every finished call is written, once, as one document: the verbatim transcript plus metadata
(Agent, Outlet, Mission, outcome, duration, summary, recording reference). The write happens off
the call path and can never break a call; its result is appended to the event log as a `retain`
record, which is where a failed write shows up.

| Backend | Chosen when | Needs |
|---|---|---|
| SQLite | `HINDSIGHT_URL` is unset (the default) | Nothing. The file is `calls.sqlite3` beside the event log, or `VOICE_ARCHIVE_PATH`. |
| Hindsight | `HINDSIGHT_URL` is set | A [Hindsight](https://github.com/vectorize-io/hindsight) server and a bank (`HINDSIGHT_BANK`, default `voice`), created before the first call. |

`VOICE_ARCHIVE=sqlite` or `VOICE_ARCHIVE=hindsight` forces one; any other value is refused.
`VOICE_RETAIN_ENABLED=false` turns archiving off.

Create a Hindsight bank before pointing VoiceMaster at it:

```bash
curl -X PUT "$HINDSIGHT_URL/v1/default/banks/voice" -H 'Content-Type: application/json' -d '{}'
```

Hindsight answers 200 with an empty list for a bank it does not have, so a missing bank looks
like "no calls yet", not like an error. Hindsight has no authentication of its own; keep it on a
private network.

## Voice lanes

Each Agent picks a lane on its Voice tab.

| Lane | Hears | Thinks | Speaks | Needs |
|---|---|---|---|---|
| Realtime (default) | OpenAI Realtime | OpenAI Realtime, calling Hermes as a tool | OpenAI Realtime | `OPENAI_API_KEY` |
| Direct Hermes | ElevenLabs Scribe or Deepgram | the Agent's own Hermes profile | ElevenLabs | `ELEVENLABS_API_KEY`, optionally `DEEPGRAM_API_KEY` |
| Cascade (outbound only) | a speech-to-text provider | an outside model (OpenRouter, Gemini, NVIDIA and others) | a text-to-speech provider | the provider keys |

The provider list, with which combinations are proven, is `services/voice-config/providers.yaml`
and the Settings screen. `VOICE_HERMES_DIRECT_ENABLED=false` removes the direct lane without a
rebuild.

The direct lane sends each Agent's tools setting to Hermes as `tool_choice` (`none` or `auto`)
on every turn. Stock Hermes Agent may not honour `tool_choice` yet; see `hermes/README.md`.

## Shared directories

| Container path | Compose source | Written by | What |
|---|---|---|---|
| `/app/voice-config` | `data/voice-config` | dashboard (bridges read) | `agents/`, `active.yaml` (which Agent answers each Outlet), `schedules/`, `gateways/gateways.json` |
| `/app/events` | `data/events` | everyone | event log, fallback log, last-known-good snapshots, SQLite archive |
| `/app/recordings` | `data/recordings` | bridges (dashboard reads) | one stereo Opus file per call |
| `/hermes-profiles` | `HERMES_PROFILES_HOST_DIR` | dashboard | Hermes profiles, when you create an Agent |

## Kill switches

None of these needs a rebuild; restart the service after changing `.env`.

| Setting | Off means |
|---|---|
| `VOICE_RECORDING_ENABLED` | No audio is captured. Calls and transcripts are unaffected. |
| `VOICE_SUMMARY_ENABLED` | No per-call summary is requested. |
| `VOICE_SUMMARY_TIMEOUT_S` | How long the archive write waits for the summary (default 30). |
| `VOICE_SCHEDULER_ENABLED` | Schedules are kept but never fire. |
| `VOICE_HERMES_DIRECT_ENABLED` | The direct Hermes lane is unavailable. |
| `VOICE_RETAIN_ENABLED` | Calls are not archived. |

Scheduler timing (`VOICE_SCHEDULE_GRACE_S`, `VOICE_SCHEDULE_STALE_CLAIM_S`,
`VOICE_SCHEDULE_TICK_S`) refuses to boot on a zero, negative or non-numeric value rather than
falling back to a default.

## When an Outlet's Agent breaks

If the Agent assigned to an Outlet becomes invalid (deleted, disabled, a bad file), the Outlet
answers with the Agent that most recently completed a call on it, and says so: a `fallback`
record in the event log's directory, a warning in the container log, and the slot shown in red
on the Agents screen. Fix the assignment on the Agents screen.
