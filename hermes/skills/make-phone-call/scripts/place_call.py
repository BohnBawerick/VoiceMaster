#!/usr/bin/env python3
"""Trigger an autonomous OUTBOUND call via a VoiceMaster bridge (Nextcloud Talk or Twilio).

Bridge URLs come from the environment:
  TALK_VOICE_SIDECAR_URL  the Talk voice bridge   (default http://localhost:3338)
  MODE_C_SIDECAR_URL      the Twilio phone bridge (default http://localhost:3336)

Two-zone security model:
  - TRIGGER zone (here): runs inside the trusted, owner-authenticated Hermes gateway. The agent
    composes the mission brief - the ONLY content that crosses to the untrusted call - and this
    script POSTs it to the bridge (localhost when they share a host or network namespace).
  - CALL zone (the bridge): opens a SANDBOXED OpenAI Realtime session with ZERO tools, seeded
    only with the brief. A callee therefore has no channel to reach the owner's data or Hermes.

Owner-only by policy (SKILL.md gates it) AND by the bridge bearer token (only the gateway,
which the owner drives, holds it). Uses only the Python stdlib so it adds no gateway dependency.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

TALK_URL = os.environ.get("TALK_VOICE_SIDECAR_URL", "http://localhost:3338")
TWILIO_URL = os.environ.get("MODE_C_SIDECAR_URL", "http://localhost:3336")


def _token() -> str:
    # The bridge validates Authorization: Bearer <gateway token>. Inside the gateway process
    # that value is API_SERVER_KEY; the bridges receive the same value as HERMES_GATEWAY_TOKEN.
    return os.environ.get("HERMES_GATEWAY_TOKEN") or os.environ.get("API_SERVER_KEY", "")


def _post(url: str, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_token()}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode()


def main() -> None:
    ap = argparse.ArgumentParser(description="Place an autonomous outbound call.")
    ap.add_argument("--brief", required=True,
                    help="The full mission brief - the ONLY thing the on-call AI will know or act on.")
    ap.add_argument("--target", help="Talk userid to call; resolved to a 1:1 room.")
    ap.add_argument("--token", help="Talk room token to call in; skips resolution.")
    ap.add_argument("--number", help="E.164 phone number to call (Twilio phone bridge).")
    ap.add_argument("--report-channel", default="talk", choices=["talk", "telegram"],
                    help="Where to deliver the transcript afterwards (the channel you asked from).")
    ap.add_argument("--report-address", default="",
                    help="Talk room token or Telegram chat id for the transcript delivery.")
    ap.add_argument("--target-display", default="", help="Friendly name of who we're calling (report only).")
    a = ap.parse_args()

    common = {"brief": a.brief, "report_channel": a.report_channel,
              "report_address": a.report_address, "target_display": a.target_display}

    if a.number:
        url = f"{TWILIO_URL}/voice/outbound"
        payload = {**common, "to": a.number}
    else:
        if not (a.target or a.token):
            print("ERROR: provide one of --target / --token (Talk) or --number (Twilio)", file=sys.stderr)
            sys.exit(2)
        url = f"{TALK_URL}/call/outbound"
        payload = {**common, **({"token": a.token} if a.token else {"target": a.target})}

    try:
        status, body = _post(url, payload)
    except urllib.error.HTTPError as e:
        print(f"CALL FAILED (HTTP {e.code}): {e.read().decode()[:300]}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"CALL FAILED: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"HTTP {status}: {body}")
    sys.exit(0 if status == 200 else 1)


if __name__ == "__main__":
    main()
