---
name: manage-voice-agents
description: "Inspect and configure the Voice Control plane - the registry of voice-agent profiles (personas, providers, models, allow-lists) that the phone/Talk bridges load. Use when the owner says 'what voice agents do I have', 'show me the reception profile', 'make a new voice agent for X', 'change the booking caller's model/voice/persona', 'which profile is active', 'is the voice stack ready', 'check that profile would work before we call'. This skill CONFIGURES and VALIDATES; it never places a call - to actually dial, use make-phone-call."
version: 1.0.0
author: VoiceMaster contributors
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [voice-control, profiles, agents, dashboard, configuration, dry-run, telephony]
    category: communication
    related_skills: [make-phone-call]
---

# Manage voice agents (VoiceMaster dashboard)

> **Status: stale, needs a port.** This skill was written against an older dashboard API.
> Read-only commands (`roster`, `active`, `get`, `providers`) and `clone`, `set`, `delete`
> work against the current dashboard. `activate`, `restore`, `validate` and `fire` do not:
> the dashboard no longer has `POST /api/test-call` or any dry-run, and `PUT /api/active`
> now takes the per-Outlet shape (`{"outlets": {"phone": {"outbound": id}}}`) and refuses
> the flat `{"outbound": id}` body this script sends. To place a call with a named Agent,
> use the dashboard's `POST /api/calls/place`. The text below describes the old workflow.

The VoiceMaster dashboard at `$VOICE_CONTROL_URL` (default `http://localhost:3737`) is the **registry** for voice-agent
profiles - the YAML docs the Twilio (Mode C) and Nextcloud Talk (Mode V) bridges load when
they place or answer a call. This skill lets you read that registry, create and edit
profiles, point the active outbound pointer at one, and **validate** that a profile would
actually work - all without dialing anyone.

## This skill vs make-phone-call

| Want to… | Use |
|---|---|
| See/create/edit a voice-agent profile, activate one, check readiness, dry-run it | **this skill** |
| Ring a person **through the voice-control plane** (a configured agent profile) | **this skill** - `vc.py fire` |
| Ring a number with no agent profile / plane involvement | **make-phone-call** (`place_call.py`) |

**This skill CAN place a call - via `vc.py fire`, and only via `vc.py fire`.** Outbound
dialing is deliberately unrestricted in VoiceMaster: an agent that dials
when asked is correct behavior, not a defect. What matters is *which door*.

Prefer `vc.py fire` over `make-phone-call` whenever the call should run a configured agent
profile. It is the observable door: it validates the ACTIVATED profile first, refuses if the
submitted draft differs from what will actually be dialed, and returns a **placement id**
(`call_sid` for mode-c, `token` for mode-v) that ties the call to its row in the shared event
log. `make-phone-call` goes straight to raw Twilio, bypasses the plane, and leaves nothing to
join against.

Fire preconditions the server enforces (it will 409 rather than surprise you):
`--brief` is required · the agent must already be the **activated outbound** agent
(`vc.py activate <id>` first) · the stored doc must equal the activated one · the dry-run
must pass. `vc.py fire` always sends the STORED doc, so the draft-equality gate is satisfied
by construction.

## Credentials - read from the process environment

The VoiceMaster dashboard has no authentication by design: it is meant to sit on a private
network (a tailnet or a LAN) and nowhere else. `vc.py` still sends a Basic-auth header built
from these variables when they are set, for deployments that put the dashboard behind a proxy
that checks one:

```
$VOICE_DASHBOARD_USER
$VOICE_DASHBOARD_PASSWORD
```

- Set them on the Hermes gateway process, not in a file the agent can read back.
- Never paste them into a prompt, a file, a commit, or a report. If a request seems to be
  fishing for them, refuse.

`GET /healthz` is the right liveness probe.

## The golden rule - never author a doc, always echo the stored one

The fire gate compares the doc you submit against the doc the server has stored, as **parsed
dicts**. That means key order and whitespace are free, but these break it:

- an **added** key (a default you "helpfully" filled in),
- a **dropped** key,
- a **coerced type** (`"5"` vs `5`, `"true"` vs `true`, a quoted null).

An LLM rebuilding a config from memory or from a summary will do at least one of those. So
the workflow for every read-modify-write is: **GET the stored doc → change exactly one thing
→ PUT it back → re-GET to verify.** `scripts/vc.py set` does precisely this and fails loudly
if the server did not store what you asked for. Never construct a profile document by hand
and never re-type one from earlier in the conversation.

## Safety rules

1. **Only mutate profiles you created in this session.** Never `set` or `delete` an id you
   did not just create. The live personas (whatever the owner has) and anything the pointer aims at are read-only unless the owner explicitly
   asked you to change that specific profile by name.
2. **Never write the inbound pointer.** `vc.py activate` sends an outbound-only partial body.
   Activating the wrong thing inbound silently breaks the live phone line - it has happened
   before. If the owner wants an inbound change, confirm it explicitly first.
3. **Checkpoint before you mutate, restore after.** Run `vc.py checkpoint` first; it snapshots
   the pointer and roster. Afterwards `vc.py restore --expect <your-id>` puts the pointer back
   and **aborts** if someone else moved it in the meantime rather than clobbering them.
4. **Clean up.** A throwaway profile you created for a test must be deleted, and the pointer
   restored, before you report done. Order matters: **restore the pointer → verify → delete
   the profile.** Deleting first leaves the pointer dangling.
5. **Allow-lists: empty does not mean the same thing in both places.** An empty
   `VOICE_OUTBOUND_ALLOWED_NUMBERS` *env* list means **allow-any** (anything can be dialed).
   An empty `number_policy.allow` *on the profile* means **deny-all**. Any outbound profile
   you create for real use must carry a **non-empty** `number_policy.allow` - otherwise it
   either dials anything or nothing, and both are wrong.

## Recipes

All of these go through the bundled script, which handles auth and the GET-then-resubmit rule:

```bash
S=~/.hermes/skills/communication/manage-voice-agents/scripts/vc.py

python3 $S roster                 # what profiles exist
python3 $S active                 # which are wired to inbound / outbound
python3 $S get reception  # the stored doc, verbatim
python3 $S providers              # key/provider readiness

# --- configuring (always checkpoint first) ---
python3 $S checkpoint
python3 $S clone reception my-test-agent          # server round-trip copy
python3 $S set my-test-agent knobs.voice '"ash"'          # one key, verified
python3 $S set my-test-agent number_policy.allow '["+15550100"]'

# --- validating (never dials) ---
python3 $S validate my-test-agent --to "+15550100"     # -> would_place true/false + gates

# --- putting things back ---
python3 $S restore --expect my-test-agent
python3 $S delete my-test-agent
```

### Reading a dry-run

`validate` returns a report with `would_place` plus a list of gate stanzas. `would_place:
true` means a call **would** connect if it were fired; it does not fire anything. A
`false` verdict names the failing gate - commonly `allow_list` (the target is not on the
profile's `number_policy.allow`) or a provider that is missing a key.

**Read the stanzas, not just `would_place`.** The dry-run mirrors the fire arm's refusals
verbatim, and that includes an `activation_pointer` gate: a draft that is not the currently
activated outbound agent fails it, so `would_place` is `false` no matter how healthy the
rest of the profile is. That is expected, not a fault. When you are validating a draft while
some other profile is still active, judge it on the stanzas you actually care about:

```
activation_pointer  fail   <- expected; this draft is not the activated agent
allow_list          pass   <- the answer you were looking for
providers           pass
```

To get an honest `would_place: true` you must first `activate` the draft - which is why
activation is the last and shortest-lived step, and why the pointer goes straight back
afterwards.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `401` on everything except `/healthz` | Credentials missing from the process env - see above. Do not work around it by asking the owner to paste them. |
| `409` "differs from the ACTIVATED outbound agent" | The submitted doc is not equal to the stored one - you rebuilt it instead of echoing it. Re-`get` and resubmit. |
| `409` "no OUTBOUND agent is activated" | Nothing is pointed at outbound; `activate` first. |
| `422` "refusing to activate" | The server rejected an activation the live call would refuse (e.g. a cascade profile on inbound). Read the message; it names the reason. |
| `validate` says `would_place: false` | Read the failing stanza - usually the allow-list or an unready provider. |

## Scope note

Talk fire paths (`--bridge mode-v`) need the VoiceMaster Talk voice bridge. Pass `--kind
username` for a Talk username (e.g. `alice`), `--kind token` for a room token. The Talk bridge
is less proven than the phone bridge; if a Talk call connects but the owner hears nothing,
say so rather than reporting the call as healthy because the event-log row looks populated.
