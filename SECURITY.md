# Security policy

VoiceMaster places real phone calls, holds call recordings and transcripts, and can reach an
owner's Hermes tools. Please report security problems privately.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: the **Report a vulnerability** button on this
repository's **Security** tab. Do not open a public issue, and do not include real phone
numbers, recordings or credentials in the report.

You should get an answer within a week. Fixes are released as soon as they are ready, and the
advisory credits the reporter unless they ask otherwise.

## In scope

- The dashboard (`services/voice-control`): authentication, access to recordings and
  transcripts, the Agent and profile endpoints, anything that places a call.
- The gateway bearer (`HERMES_GATEWAY_TOKEN`) on `POST /voice/outbound`, and the paths that
  must refuse without it.
- Twilio webhook signature validation on the phone bridge.
- Outbound dialing controls: the allow-list, the one-shot placement, the scheduler.
- The inbound caller allow-list and the tool policy an inbound or outbound Agent runs with.
- The Hermes add-ons in `hermes/`.

## Out of scope

- A dashboard deliberately run without login on a network you control. Login is off only when
  `VOICE_DASHBOARD_USER` or `VOICE_DASHBOARD_PASSWORD` is unset.
- `VOICE_OUTBOUND_ALLOWED_NUMBERS` deliberately left empty (allow any number).
- Vulnerabilities in Hermes Agent, Twilio, OpenAI or other providers themselves. Report those
  upstream.

## Running it safely

- Keep login on unless the dashboard is reachable only from a private network.
- Keep `.env` out of git, readable only by the account that runs Docker.
- Use a long random `HERMES_GATEWAY_TOKEN`.
- Expose only the phone bridge (port 3336) through your public tunnel, never the dashboard.
