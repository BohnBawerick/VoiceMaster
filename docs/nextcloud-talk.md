# Nextcloud Talk Outlet (experimental)

The second Outlet: an Agent that joins Nextcloud Talk voice calls. It works for its original
owner, but the setup is involved and it has not been tried on anyone else's server. Expect to
read code.

## What it needs

- A Nextcloud server with Talk, and a dedicated Nextcloud user for the Agent (its login and an
  app password).
- The Talk platform plugin for Hermes (`hermes/plugins/nextcloud_talk`), which carries the chat
  side: noticing a call, owner and guest trust, and the approval relay. See
  [`hermes/README.md`](../hermes/README.md).
- The Talk bridge container: `docker compose --profile talk up -d --build`. It runs a headed
  Chromium under Xvfb with PulseAudio taps to join the call as that user.

Settings, in `.env`:

| Setting | What |
|---|---|
| `NEXTCLOUD_BASE_URL` | `https://nextcloud.example.com` |
| `NEXTCLOUD_TALK_USER` | The Agent's Nextcloud user name |
| `NEXTCLOUD_VOICE_APP_PASSWORD` | An app password for that user (API calls) |
| `NEXTCLOUD_VOICE_LOGIN_PASSWORD` | Its login password (the browser session) |
| `NEXTCLOUD_TALK_HOME_CONVERSATION` | The conversation token the Agent treats as home |
| `TALK_VOICE_*` | Timeouts and VAD tuning; see `services/talk-voice-bridge/config.py` |

## The traps

- **Headless Chromium hears silence.** Inbound Talk audio needs a headed browser, which is why
  the image runs Xvfb.
- **An access gateway in front of Nextcloud breaks calling.** If a login wall (an SSO proxy, a
  zero-trust access policy) sits in front of the server, the bridge's browser is redirected to it
  and never joins. Exempt the bridge's source address.
- On Talk, the direct Hermes lane is for the owner only; a guest gets the Realtime lane and the
  approval loop.
