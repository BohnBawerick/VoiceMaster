#!/usr/bin/env python3
"""Drive the VoiceMaster dashboard (VOICE_CONTROL_URL, default localhost:3737) safely.

This script is the ONLY sanctioned way for Hermes to mutate voice-agent profiles.
It exists to make three rules structural rather than a matter of discipline:

1. **It dials through exactly ONE audited door.** Only ``fire()`` may send the
   ``fire`` key; every other path still raises if it appears. Dialing is a normal,
   permitted capability on this system (outbound is deliberately unrestricted -
 , so the goal is not to prevent a call but to make sure a call can only
   happen on a path that validates the activated profile first and returns a
   placement id that can be joined to the event log. The unaudited alternative
   (``make-phone-call`` → raw Twilio) bypasses the plane entirely and is what
 's b5 probe caught; prefer this door so the dial is observable.
2. **Docs are never authored locally.** Every write starts from the doc the server
   already stores (GET -> mutate one key -> PUT -> re-GET to verify). The fire gate
   compares parsed dicts, so a hand-built doc that "looks right" but carries an
   extra default or a coerced type silently becomes un-fireable.
3. **The inbound line is never touched.** Activation sends an outbound-only partial
   body, and ``restore`` refuses to write a pointer it does not recognise.

Credentials come from the process environment (VOICE_DASHBOARD_USER / _PASSWORD).
Keep them out of files the agent can read back.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("VOICE_CONTROL_URL", "http://localhost:3737")
CHECKPOINT = os.path.join(
    os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"), ".vc-checkpoint.json")
DIRECTIONS = ("inbound", "outbound")


class VCError(RuntimeError):
    pass


# --------------------------------------------------------------------------- http


def _auth_header() -> str:
    user = os.environ.get("VOICE_DASHBOARD_USER")
    password = os.environ.get("VOICE_DASHBOARD_PASSWORD")
    if not user or not password:
        raise VCError(
            "VOICE_DASHBOARD_USER / VOICE_DASHBOARD_PASSWORD are not in this process's "
            "environment. Set them on the Hermes gateway process; read them from the "
            "process env, and never paste them into a prompt or a file."
        )
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _request(method: str, path: str, body: dict | None = None,
             allow_fire: bool = False) -> tuple[int, object]:
    if isinstance(body, dict) and "fire" in body and not allow_fire:
        # The guard survives on every path except fire(): a stray `fire` key
        # arriving through clone/set/activate/validate is a bug, not an intent.
        raise VCError(
            "refusing to send a request carrying 'fire' from a non-dial path - "
            "only the `fire` subcommand may place a call."
        )
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Authorization", _auth_header())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode()
        try:
            return exc.code, json.loads(payload or "null")
        except json.JSONDecodeError:
            return exc.code, payload


def _ok(code: int, payload: object, what: str) -> object:
    if code >= 400:
        raise VCError(f"{what} failed (HTTP {code}): {json.dumps(payload, indent=2)}")
    return payload


# ------------------------------------------------------------------------- reads


def get_doc(agent_id: str) -> dict:
    """The stored doc, exactly as the server holds it. This is the ONLY valid
    starting point for any write or validation - never reconstruct it."""
    code, payload = _request("GET", f"/api/agents/{agent_id}")
    return _ok(code, payload, f"GET agent {agent_id!r}")


def get_active() -> dict:
    code, payload = _request("GET", "/api/active")
    return _ok(code, payload, "GET active pointer")


def get_roster() -> object:
    code, payload = _request("GET", "/api/agents")
    return _ok(code, payload, "GET roster")


# ------------------------------------------------------------------------ writes


def clone(src_id: str, new_id: str) -> dict:
    """Create ``new_id`` as a server round-trip copy of ``src_id``.

    The copy is taken from the STORED doc so the new profile is byte-compatible
    with what the fire gate will later load.
    """
    doc = dict(get_doc(src_id))
    doc["id"] = new_id
    code, payload = _request("POST", "/api/agents", doc)
    _ok(code, payload, f"create agent {new_id!r}")
    return get_doc(new_id)


def set_key(agent_id: str, dotted: str, value_json: str) -> dict:
    """Surgically set ONE dotted key on the stored doc, then verify by re-reading.

    Everything else in the doc is passed through untouched - this is what keeps the
    profile equal to what the fire gate loads.
    """
    try:
        value = json.loads(value_json)
    except json.JSONDecodeError:
        value = value_json  # bare strings are a convenience, not an error
    doc = get_doc(agent_id)
    cursor = doc
    parts = dotted.split(".")
    for part in parts[:-1]:
        if part not in cursor or not isinstance(cursor[part], dict):
            cursor[part] = {}
        cursor = cursor[part]
    cursor[parts[-1]] = value
    code, payload = _request("PUT", f"/api/agents/{agent_id}", doc)
    _ok(code, payload, f"update agent {agent_id!r}")
    fresh = get_doc(agent_id)
    probe = fresh
    for part in parts:
        probe = probe[part] if isinstance(probe, dict) and part in probe else None
        if probe is None:
            break
    if probe != value:
        raise VCError(
            f"write-verify failed for {dotted!r}: server stored {probe!r}, expected {value!r}"
        )
    return fresh


def delete(agent_id: str) -> None:
    code, payload = _request("DELETE", f"/api/agents/{agent_id}")
    _ok(code, payload, f"delete agent {agent_id!r}")


# ------------------------------------------------- checkpoint / activate / restore


def checkpoint() -> dict:
    """Snapshot the pointer + roster BEFORE any mutation. Always run this first."""
    active = get_active()
    state = {
        "active": {d: active.get(d) for d in DIRECTIONS},
        "roster": get_roster(),
    }
    os.makedirs(os.path.dirname(CHECKPOINT), exist_ok=True)
    with open(CHECKPOINT, "w") as handle:
        json.dump(state, handle, indent=2)
    return state


def _load_checkpoint() -> dict:
    if not os.path.exists(CHECKPOINT):
        raise VCError(f"no checkpoint at {CHECKPOINT} - run `vc.py checkpoint` first")
    with open(CHECKPOINT) as handle:
        return json.load(handle)


def activate(agent_id: str) -> dict:
    """Point OUTBOUND at ``agent_id``. Outbound-only, partial body - inbound is
    never included, so the live DID cannot be disturbed."""
    _load_checkpoint()  # refuse to activate without a way back
    code, payload = _request("PUT", "/api/active", {"outbound": agent_id})
    return _ok(code, payload, f"activate {agent_id!r} outbound")


def restore(expect: str | None = None) -> dict:
    """Put the outbound pointer back to the checkpointed value.

    Aborts rather than blind-writing if the current pointer is neither the value we
    set nor the one we captured - that means someone else moved it and restoring
    would clobber their change.
    """
    state = _load_checkpoint()
    original = state["active"].get("outbound")
    current = get_active().get("outbound")
    if current not in (original, expect):
        raise VCError(
            f"ABORT: outbound is {current!r}, expected {expect!r} or the checkpointed "
            f"{original!r}. Someone else moved the pointer - not restoring. Resolve by hand."
        )
    if current == original:
        return {"outbound": current, "restored": False, "note": "already at checkpoint"}
    code, payload = _request("PUT", "/api/active", {"outbound": original})
    result = _ok(code, payload, f"restore outbound to {original!r}")
    return {"outbound": original, "restored": True, "pointer": result}


# --------------------------------------------------------------------- validation


def validate(agent_id: str, to: str, bridge: str = "mode-c",
             kind: str | None = None) -> dict:
    """Dry-run the STORED doc against ``to``. Never fires.

    Safe (and preferred) to run while some OTHER profile is still active: the fire
    gate's doc-equality check then acts as a live tripwire against a stray fire.
    The cost is that the report's ``activation_pointer`` stanza fails in that state
    - the dry-run mirrors the fire arm's refusals verbatim - so ``would_place`` is
    False until the draft is the activated outbound agent. Judge a pre-activation
    dry-run on the individual stanzas (``allow_list``, ``providers``, ...), not on
    ``would_place``.
    """
    doc = get_doc(agent_id)
    body: dict = {"doc": doc, "to": to, "bridge": bridge}
    if bridge == "mode-v":
        # The server 422s without it - a Talk target is a username or a room token,
        # and it will not guess which.
        if kind not in ("username", "token"):
            raise VCError(
                "mode-v needs --kind username|token (a Talk username is not a number)")
        body["kind"] = kind
    code, payload = _request("POST", "/api/test-call", body)
    if code >= 400:
        raise VCError(f"dry-run failed (HTTP {code}): {json.dumps(payload, indent=2)}")
    return payload


def fire(agent_id: str, to: str, brief: str, bridge: str = "mode-c",
         kind: str | None = None, target_display: str = "") -> dict:
    """PLACE A REAL CALL. The phone actually rings.

    Same doc discipline as ``validate``: the body carries the STORED doc, never a
    locally authored one, because the server refuses to fire a draft that differs
    from the ACTIVATED outbound profile (it would validate one config and dial
    another). So ``agent_id`` must already be the activated outbound agent -
    ``activate`` it first.

    ``brief`` is required by the server: it is the mission/opening the agent
    pursues on this dial.

    Returns the server's placement record. The placement id (``call_sid`` for
    mode-c, ``token`` for mode-v) is the JOIN KEY: it is how a row in the shared
    event log is tied to a call that was actually placed, rather than to a row
    that merely appeared around the right time.
    """
    doc = get_doc(agent_id)
    # The bridges write ``target_display`` straight into the event log's ``target``
    # column, so omitting it leaves the row unable to name who was called. Fall back
    # to the dialled address: less friendly than a real name, never empty.
    body: dict = {"doc": doc, "to": to, "bridge": bridge, "fire": True,
                  "brief": brief,
                  "target_display": target_display.strip() or to}
    if bridge == "mode-v":
        if kind not in ("username", "token"):
            raise VCError(
                "mode-v needs --kind username|token (a Talk username is not a number)")
        body["kind"] = kind
    code, payload = _request("POST", "/api/test-call", body, allow_fire=True)
    if code >= 400:
        # 409 carries a real refusal (gate failed / busy / draft≠activated) - surface
        # it verbatim rather than flattening it, the reason is the diagnostic.
        raise VCError(f"fire refused (HTTP {code}): {json.dumps(payload, indent=2)}")
    return payload


# --------------------------------------------------------------------------- cli


def main() -> int:
    parser = argparse.ArgumentParser(description="Drive the Voice Control plane (read/config/validate/fire).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("roster", help="list agent profiles")
    sub.add_parser("active", help="show the active inbound/outbound pointer")
    sub.add_parser("providers", help="provider readiness")
    sub.add_parser("checkpoint", help="snapshot pointer + roster before mutating")

    p = sub.add_parser("get", help="print a stored agent doc"); p.add_argument("id")
    p = sub.add_parser("delete", help="delete an agent"); p.add_argument("id")
    p = sub.add_parser("activate", help="point OUTBOUND at an agent"); p.add_argument("id")

    p = sub.add_parser("clone", help="server round-trip copy of an agent")
    p.add_argument("src"); p.add_argument("new")

    p = sub.add_parser("set", help="surgically set one dotted key")
    p.add_argument("id"); p.add_argument("key"); p.add_argument("value")

    p = sub.add_parser("restore", help="restore outbound pointer from checkpoint")
    p.add_argument("--expect", default=None,
                   help="the id you activated, so an unexpected pointer aborts")

    p = sub.add_parser("validate", help="dry-run a stored doc (NEVER fires)")
    p.add_argument("id"); p.add_argument("--to", required=True)
    p.add_argument("--bridge", default="mode-c", choices=["mode-c", "mode-v"])
    p.add_argument("--kind", default=None, choices=["username", "token"],
                   help="mode-v only: how --to should be resolved")

    p = sub.add_parser("fire", help="PLACE A REAL CALL - the phone actually rings")
    p.add_argument("id", help="must already be the ACTIVATED outbound agent")
    p.add_argument("--to", required=True, help="E.164 for mode-c, Talk username/token for mode-v")
    p.add_argument("--brief", required=True, help="the mission/opening for this dial")
    p.add_argument("--bridge", default="mode-c", choices=["mode-c", "mode-v"])
    p.add_argument("--kind", default=None, choices=["username", "token"],
                   help="mode-v only: how --to should be resolved")
    p.add_argument("--target-display", default="", dest="target_display",
                   help="friendly name of the callee for the report and the event-log "
                        "'target' column (defaults to --to)")

    args = parser.parse_args()
    try:
        if args.cmd == "roster":
            out = get_roster()
        elif args.cmd == "active":
            out = get_active()
        elif args.cmd == "providers":
            code, payload = _request("GET", "/api/providers")
            out = _ok(code, payload, "GET providers")
        elif args.cmd == "checkpoint":
            out = checkpoint()
        elif args.cmd == "get":
            out = get_doc(args.id)
        elif args.cmd == "delete":
            delete(args.id)
            out = {"deleted": args.id}
        elif args.cmd == "clone":
            out = clone(args.src, args.new)
        elif args.cmd == "set":
            out = set_key(args.id, args.key, args.value)
        elif args.cmd == "activate":
            out = activate(args.id)
        elif args.cmd == "restore":
            out = restore(args.expect)
        elif args.cmd == "validate":
            out = validate(args.id, args.to, args.bridge, args.kind)
        elif args.cmd == "fire":
            out = fire(args.id, args.to, args.brief, args.bridge, args.kind,
                       args.target_display)
        else:  # pragma: no cover - argparse enforces the set
            raise VCError(f"unknown command {args.cmd!r}")
    except VCError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
