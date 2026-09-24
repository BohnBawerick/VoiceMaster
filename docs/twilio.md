# Twilio setup

The phone Outlet is a Twilio number whose calls stream to the phone bridge
(`services/voice`, port 3336) over Twilio Media Streams.

## Steps

1. Buy a voice-capable number. Put it in `TWILIO_FROM_NUMBER` (E.164, e.g. `+15550100000`).
2. Put the account SID and the account's **primary** auth token in `TWILIO_ACCOUNT_SID` and
   `TWILIO_AUTH_TOKEN`. The bridge uses them to place outbound calls through the REST API.
3. Give the bridge a public HTTPS host: a tunnel (cloudflared, ngrok, Tailscale Funnel) or a
   reverse proxy that forwards to `127.0.0.1:3336`, including WebSocket upgrades. Put the host
   name, without scheme or path, in `VOICE_PUBLIC_HOST`.
4. In the Twilio console, set the number's **A call comes in** webhook to
   `https://<VOICE_PUBLIC_HOST>/voice/webhook`, HTTP POST.
5. Put the numbers allowed to call in `VOICE_INBOUND_ALLOWED_CALLERS`. Empty rejects everyone.
6. Place a real call each way. Nothing else proves the setup.

## The traps

**Twilio signs a webhook with the auth token of the region that handled the call.** A number
handled outside the default US1 region (Ireland, Singapore, and so on) arrives signed with that
region's token, which is a different secret from the account's primary token. It is a signing key
only: it cannot log in to the REST API. So the two jobs are two settings:

| Setting | Job | Direction |
|---|---|---|
| `TWILIO_AUTH_TOKEN` | REST login, places outbound calls | outbound; must stay the primary token |
| `TWILIO_SIGNING_TOKENS` | comma-separated tokens accepted on an inbound webhook | inbound |

Unset, `TWILIO_SIGNING_TOKENS` is `TWILIO_AUTH_TOKEN` alone, which is right for a US1 number.
"Fixing" inbound by putting the regional token in `TWILIO_AUTH_TOKEN` breaks every outbound
call. The fingerprint of this problem: a request you signed yourself gets 200, and a real call to
the number gets 403 minutes later.

**A hand-signed probe proves nothing about the signing secret.** You signed it with a token you
chose, so it passes whatever Twilio actually uses. Only a real call proves inbound.

**`VOICE_PUBLIC_HOST` must be set behind any proxy or tunnel.** Twilio signs the public HTTPS URL
it called. Behind a proxy the bridge sees its internal `http://` origin, so without this every
inbound webhook fails validation with 403. It is also the host Twilio dials back for the media
stream.

**Bot protection on your proxy can block Twilio.** Some CDN and tunnel products challenge or
403 non-browser user agents before the request reaches the bridge. If Twilio's debugger shows
403s and the bridge log shows nothing, look there first.

**An empty Twilio call log is a region clue.** Calls handled in another region are listed under
that region in the console.
