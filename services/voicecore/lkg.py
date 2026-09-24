"""Per-Outlet last-known-good answering fallback (ticket 08).

Record side: each bridge records the profile that most recently COMPLETED a call
successfully on its own Outlet, keyed (outlet, direction). The write happens only at
teardown of a call that actually ran - never at activation - so a pointer that never
served a call is never a known-good configuration, and every successful call refreshes
the snapshot (a later working assignment supersedes it naturally).

Fallback side: when activation of the outlet's assigned configuration fails, the bridge
answers with that snapshot instead of refusing - and never quietly. Every fallback
answer writes an event-log record naming the outlet, the broken assignment and the
snapshot used, plus a WARNING log line. The dashboard half needs nothing from here: the
pointer file is never touched, so the per-slot health warning landed by tickets 16/02
keeps naming the broken field path while the fallback answers.

Persistence: one file per (outlet, direction) - ``<dir>/lkg-<outlet>-<direction>.json`` -
atomically replaced (tmp + os.replace) by the single bridge that owns that outlet, so
there are no cross-writer races and no append-scan. ``<dir>`` is the events volume (the
parent of VOICE_EVENTLOG_PATH, overridable via VOICE_LKG_DIR) - a persistent bind mount
both bridges already write to, so the snapshot survives restarts. The config dir is
mounted read-only for the bridges, which rules out storing the snapshot next to
active.yaml. A long-dead snapshot has a bounded failure mode by construction: it is
consulted ONLY when the current assignment fails to activate (never overrides a working
assignment), it is refreshed by every successful call, and every use is announced by
name.

Call sites: the bridges' ANSWERING paths only (media_stream, CallSession.start, the
pre-dial gates). The dashboard's dry-runs, readiness and preview keep calling
``profiles.load_effective_profile`` directly so a broken slot keeps reporting broken
there - a fallback answer must not make the dashboard look healthy.

Selection precedence is untouched: ``resolve`` first delegates to
``profiles.load_effective_profile`` exactly as before; only a raised ProfileError
consults the store, and with no snapshot the original error propagates, so the loud
refusal is byte-for-byte the pre-fallback behaviour. VOICE_AGENT still wins outright;
if the env-selected profile itself fails to activate, the fallback applies there too
(the rule is "the phone never goes dead silently", whatever broke the assignment).
"""
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from voicecore import eventlog
from voicecore import profiles

logger = logging.getLogger("voice.lkg")

LKG_DIR_ENV = "VOICE_LKG_DIR"
FALLBACK_LOG_ENV = "VOICE_FALLBACK_LOG_PATH"
SCHEMA_VERSION = 1

# The broken assignment, when the ProfileError message names one ("agent 'x' (searched
# …): not found"). Structural pointer faults name a field path instead - no agent id.
_BROKEN_AGENT_RE = re.compile(r"agent '([^']+)'")


def _events_dir(env=None) -> Path:
    env = os.environ if env is None else env
    raw = (env.get(LKG_DIR_ENV) or "").strip()
    if raw:
        return Path(raw)
    eventlog_path = env.get("VOICE_EVENTLOG_PATH") or eventlog.DEFAULT_PATH
    return Path(eventlog_path).parent


def snapshot_path(outlet: str, direction: str, env=None) -> Path:
    return _events_dir(env) / f"lkg-{outlet}-{direction}.json"


def _fallback_log_path(env=None) -> str:
    env = os.environ if env is None else env
    raw = (env.get(FALLBACK_LOG_ENV) or "").strip()
    if raw:
        return raw
    return str(_events_dir(env) / "fallback_events.jsonl")


@dataclass(frozen=True)
class LkgSnapshot:
    """A stored snapshot plus the provenance that makes the fallback announcement honest."""

    profile: "profiles.ActiveProfile"
    recorded_at: float
    last_call_id: str


def record(outlet: str, direction: str, profile, *, call_id: str = "",
           env=None, clock=time.time) -> None:
    """Persist the snapshot that just COMPLETED a call on (outlet, direction).

    Best-effort by construction - a failed record must never drop a live call. A
    failed write is logged and means only that the fallback will be unavailable,
    which keeps the loud refusal; it can never produce a silent fallback. The write
    is atomic (tmp + os.replace), so a crash mid-write cannot leave a half snapshot
    that later loads as garbage. ``profile is None`` (a profile-less call) records
    nothing - env-derived defaults are not a configuration that can break.
    """
    if profile is None:
        return
    if outlet not in profiles.OUTLETS or direction not in profiles.ACTIVE_DIRECTIONS:
        logger.warning("lkg.record: refusing to record for unknown slot %s/%s",
                       outlet, direction)
        return
    path = snapshot_path(outlet, direction, env)
    tmp = None
    try:
        payload = {
            "schema": SCHEMA_VERSION,
            "outlet": outlet,
            "direction": direction,
            "agent_id": profile.agent_id,
            "source": profile.source,
            "doc": profile.doc,
            "registry": profile.registry,
            "recorded_at": clock(),
            "call_id": call_id,
        }
        dir_existed = path.parent.is_dir()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Multi-UID rendezvous dir (mode-c runs root, the Talk bridge pwuser): make it
        # group/other writable when WE created it, same doctrine as the eventlog file.
        if not dir_existed:
            try:
                os.chmod(path.parent, 0o777)
            except OSError:
                pass
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".lkg-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.chmod(tmp, 0o666)
            os.replace(tmp, path)
            tmp = None
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except Exception:  # noqa: BLE001 — a failed record (incl. a non-profile stub the
        # dispatch tests thread through the session) must never raise into the call path
        logger.warning("lkg.record failed (path=%s)", path, exc_info=True)


def load(outlet: str, direction: str, env=None) -> "LkgSnapshot | None":
    """The recorded snapshot for (outlet, direction), or None when absent/broken.

    Reconstructs the ActiveProfile from the STORED doc + registry, so the fallback
    needs nothing from the (now broken) live config: the agent file may be deleted
    or edited invalid and the snapshot still answers with what actually worked. A
    missing/unreadable/corrupt store is exactly "no snapshot" - the caller then
    keeps the loud refusal, never a half fallback.
    """
    path = snapshot_path(outlet, direction, env)
    try:
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA_VERSION:
        return None
    if payload.get("outlet") != outlet or payload.get("direction") != direction:
        return None
    agent_id = payload.get("agent_id")
    doc = payload.get("doc")
    registry = payload.get("registry")
    source = payload.get("source")
    recorded_at = payload.get("recorded_at")
    if (not isinstance(agent_id, str) or not profiles._ID_RE.match(agent_id)
            or not isinstance(doc, dict) or doc.get("id") != agent_id
            or not isinstance(registry, dict) or not isinstance(source, str)
            or not isinstance(recorded_at, (int, float))):
        return None
    profile = profiles.ActiveProfile(agent_id=agent_id, source=source,
                                     doc=doc, registry=registry)
    return LkgSnapshot(profile=profile, recorded_at=float(recorded_at),
                       last_call_id=str(payload.get("call_id") or ""))


def resolve(direction: str, outlet: str, env=None) -> "profiles.ActiveProfile | None":
    """The answering-path resolution for one (outlet, direction) slot.

    ``profiles.load_effective_profile`` first - precedence and the no-profile path
    are untouched. On a raised ProfileError the recorded snapshot answers instead,
    announced loudly. With no snapshot the original error propagates: the loud
    refusal is preserved. Argument validation happens BEFORE the load so a
    programming error at a call site (unknown outlet/direction) is never masked by
    a fallback.
    """
    env = os.environ if env is None else env
    if direction not in profiles.ACTIVE_DIRECTIONS:
        raise profiles.ProfileError(
            f"unknown call direction {direction!r} "
            f"(expected one of {list(profiles.ACTIVE_DIRECTIONS)})")
    outlet = profiles.OUTLET_PHONE if outlet is None else outlet
    if outlet not in profiles.OUTLETS:
        raise profiles.ProfileError(
            f"unknown outlet {outlet!r} (expected one of {list(profiles.OUTLETS)})")
    try:
        return profiles.load_effective_profile(direction, outlet, env)
    except profiles.ProfileError as exc:
        snap = load(outlet, direction, env)
        if snap is None:
            raise
        _announce(outlet, direction, exc, snap, env)
        return snap.profile


def _announce(outlet: str, direction: str, error: "profiles.ProfileError",
              snap: LkgSnapshot, env=None) -> None:
    """The loud half: a fallback answer must leave evidence or it must not happen.

    One event-log record in the fallback log naming the outlet, the broken
    assignment and the snapshot used, plus a WARNING line. The event-log append is
    best-effort by doctrine (a broken log must not drop a live call), so the WARNING
    is the guaranteed backstop - and the dashboard keeps showing the slot broken
    because the pointer file was never touched.
    """
    broken = _BROKEN_AGENT_RE.search(str(error))
    eventlog.append_event({
        "type": "fallback",
        "schema": SCHEMA_VERSION,
        "ts": time.time(),
        "outlet": outlet,
        "direction": direction,
        "broken_agent": broken.group(1) if broken else None,
        "reason": str(error),
        "snapshot_agent_id": snap.profile.agent_id,
        "snapshot_source": snap.profile.source,
        "snapshot_recorded_at": snap.recorded_at,
        "last_call_id": snap.last_call_id,
    }, _fallback_log_path(env))
    logger.warning(
        "FALLBACK ANSWER on outlet %s %s: the assigned configuration failed to "
        "activate (%s) - answering with last-known-good agent '%s' (last completed "
        "call %s)", outlet, direction, error, snap.profile.agent_id,
        snap.last_call_id or "unknown")


def announce_lane_fallback(outlet: str, direction: str, agent_id: "str | None",
                           reason: str, env=None) -> None:
    """VC24: the assigned Agent is the direct Hermes lane and its gateway did not answer
    the pickup check, so THIS call is answered on the Realtime lane instead.

    Same rule as a last-known-good answer: it must leave evidence or it must not happen.
    One ``fallback`` record in the same log (``kind: lane``, so it is told apart from a
    snapshot fallback), plus a WARNING line. Nothing is written to the pointer and
    nothing becomes last-known-good: the Outlet's assignment is still the direct Agent,
    and the next call tries it again.
    """
    eventlog.append_event({
        "type": "fallback",
        "kind": "lane",
        "schema": SCHEMA_VERSION,
        "ts": time.time(),
        "outlet": outlet,
        "direction": direction,
        "assigned_agent": agent_id,
        "reason": reason,
        "answered_on": "realtime",
    }, _fallback_log_path(env))
    logger.warning(
        "LANE FALLBACK on outlet %s %s: agent '%s' is the direct Hermes lane and %s - "
        "answering THIS call on the Realtime lane with the bridge's own defaults",
        outlet, direction, agent_id, reason)
