# VoiceMaster

Give your [Hermes Agent](https://github.com/NousResearch/hermes-agent) personas a phone number.
VoiceMaster answers and places real phone calls as your Hermes profiles, and gives you a
dashboard to watch, schedule, replay and review them.

It is an independent project for Hermes Agent owners, not an official NousResearch project.

## What it does

You normally do not open the dashboard to make a call. You tell Hermes "get agent X to call
this person", and Hermes does it through the `make-phone-call` skill. The dashboard is where you
see what happened, listen back, and take over by hand.

| Screen | What it is for |
|---|---|
| **Calls** | Every call, searchable. Each one opens at its own URL with its stereo recording, summary, transcript and outcome. |
| **Schedule** | Calls that have not happened yet, and what became of the ones that came due. |
| **Agents** | Who can make and answer calls, and which Agent answers each Outlet. Each Agent's voice and tools are on its own page. Creating one creates a Hermes profile. |
| **Settings** | The proven defaults, speed dial, and every provider and voice with its evidence. |

**New call** is one form for both: call now, or schedule the call for later. You can type the
call's Mission, or ask the Agent to write it from a short line or a voice note.

## The model

Four words carry the design. [`CONTEXT.md`](CONTEXT.md) defines them properly.

- An **Agent** is a Hermes profile that can be reached on an Outlet. Its soul, tools, model and
  memory are the profile's own; VoiceMaster adds the voice and makes it callable.
- An **Outlet** is a channel calls travel on: a phone number (Twilio), and optionally Nextcloud
  Talk. One Agent sits on each, per direction.
- A **Mission** is what one outbound Call is for. Inbound calls have no Mission.
- A **Call** is one conversation, with a verbatim transcript, a stereo recording and a summary
  written by the Agent that was on it.

## How it fits together

```
 caller's phone ──► Twilio ──► your HTTPS tunnel ──► phone bridge (services/voice) ──► OpenAI Realtime
                                                         │  tools, memory, summaries
                                                         ▼
                                   Hermes gateway (one per profile, via hermes/supervisor)
                                                         ▲
 you ──► dashboard (services/voice-control) ─────────────┘  place calls, schedule, review
```

- `services/voice` is the phone bridge: Twilio Media Streams in, a voice lane out. The default
  lane is OpenAI Realtime. An Agent can instead use a cascade (speech-to-text, a model,
  text-to-speech) or the **direct Hermes lane**, where the Agent's own Hermes profile does the
  thinking between ElevenLabs Scribe and ElevenLabs TTS.
- `services/voice-control` is the dashboard and the scheduler.
- `services/talk-voice-bridge` is the optional Nextcloud Talk Outlet.
- `services/voicecore` is the package the three share.
- `hermes/` holds the Hermes-side add-ons: the profile supervisor, the transcription route, the
  two skills and the Talk plugin. See [`hermes/README.md`](hermes/README.md).

## Status

VoiceMaster runs one owner's phone line every day. What that proves, and what it does not:

| Part | Status |
|---|---|
| Phone Outlet, OpenAI Realtime lane, inbound and outbound | In daily use |
| Direct Hermes lane on the phone Outlet | Proven by real inbound and outbound calls |
| Scheduling, Missions, recordings, summaries | In daily use |
| SQLite call archive | New in the public release; the owner's install uses Hindsight |
| Nextcloud Talk Outlet | Works for the owner; setup is involved and it is experimental |
| This quick start, from a clean machine | Not yet proven end to end by a real call. Please report what breaks. |

## Quick start (phone only)

You need:

- a running Hermes Agent with its API server on (`API_SERVER_ENABLED=true`), on the same
  Linux host;
- Docker Engine with Compose v2;
- an OpenAI API key (Realtime);
- a Twilio account and a voice-capable number;
- a public HTTPS URL that reaches port 3336 on the host (cloudflared, ngrok, Tailscale Funnel
  or a reverse proxy).

Then:

```bash
git clone https://github.com/BohnBawerick/VoiceMaster.git && cd VoiceMaster
cp .env.example .env               # fill in every REQUIRED value
mkdir -p data/voice-config data/events data/recordings data/talk-state
chmod 0777 data/events data/recordings data/talk-state
hermes/install.sh --help           # install the Hermes add-ons (see hermes/README.md)
docker compose up -d --build
```

1. In the Twilio console, set the number's voice webhook to
   `https://<VOICE_PUBLIC_HOST>/voice/webhook` (HTTP POST). Read [`docs/twilio.md`](docs/twilio.md)
   before the first call: regional signing tokens are the usual reason inbound fails.
2. Open `http://<host>:3737`, sign in with `VOICE_DASHBOARD_USER` / `VOICE_DASHBOARD_PASSWORD`,
   create an Agent and assign it to the phone Outlet.
3. Place a call to your own number from **New call**. The phone should ring, and the call
   should appear on **Calls** with its transcript and recording.
4. Call the Twilio number from a number in `VOICE_INBOUND_ALLOWED_CALLERS`.
5. Ask Hermes to call you. That exercises the `make-phone-call` skill.

A hand-signed test request cannot prove the webhook signing secret. Only a real call does.

## Safe defaults, and changing them

`.env.example` ships with the careful settings. Each is one variable:

| Setting | Example default | Why |
|---|---|---|
| Dashboard login (`VOICE_DASHBOARD_USER`/`_PASSWORD`) | On | The dashboard places real calls and plays recordings. Turn it off only behind a private network. |
| Outbound allow-list (`VOICE_OUTBOUND_ALLOWED_NUMBERS`) | The owner's number only | Empty means any number. A leaked gateway token could then run up your Twilio bill. |
| AI disclosure (`VOICE_OUTBOUND_AI_DISCLOSURE`) | On | Many places require callers to say a call is automated. In the US the FCC treats AI voices as "artificial" under the TCPA. |
| Inbound callers (`VOICE_INBOUND_ALLOWED_CALLERS`) | The owner's number | Empty rejects every caller. An inbound Agent can use your Hermes tools. |

**You are responsible for complying with the calling, recording and consent laws where you and
the people you call are.** VoiceMaster records every call by default
(`VOICE_RECORDING_ENABLED=false` turns that off).

## Call archive

With no configuration, calls are archived to a SQLite file in `data/events/calls.sqlite3`. To
use a [Hindsight](https://github.com/vectorize-io/hindsight) memory bank instead, set
`HINDSIGHT_URL` (and create the bank first). See [`docs/configuration.md`](docs/configuration.md).

## Documentation

| Path | What |
|---|---|
| [`CONTEXT.md`](CONTEXT.md) | The glossary. Read it before changing anything. |
| [`docs/configuration.md`](docs/configuration.md) | Every setting that is not obvious from `.env.example`, the archive, the voice lanes. |
| [`docs/twilio.md`](docs/twilio.md) | Twilio setup and the traps. |
| [`docs/nextcloud-talk.md`](docs/nextcloud-talk.md) | The optional Talk Outlet. |
| [`hermes/README.md`](hermes/README.md) | The Hermes add-ons and how to install them. |
| [`docs/decisions.md`](docs/decisions.md), [`docs/adr/`](docs/adr/) | Why it is built the way it is. |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Development setup and the rules the code keeps. |
| [`SECURITY.md`](SECURITY.md) | How to report a vulnerability. |

## License

MIT. See [`LICENSE`](LICENSE).
