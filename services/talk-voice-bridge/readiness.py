"""s14a-2a — cascade readiness, answered IN THE PROCESS THAT RUNS THE CASCADE.

The dashboard's Talk dry-run (`voice-control/dryrun.py::run_dry_run_talk`) resolves every
provider against *voice-control's* environment, because that is the process it executes in.
That makes it structurally blind to this bridge: s14a-1 found `hermes-talk-voice` carrying
ZERO cascade credentials while the Talk dry-run reported `would_place=true`. Fixing the keys
did not fix the blindness — only moving the question here does.

This module is the honest answer to "could I run a cascade call right now?", and s14a-2b
folds it into the Talk dry-run as a stanza that can FAIL `would_place`.

READ-ONLY by construction. It never starts a call, never resolves an OCS room, never takes
the session slot lock, and never writes CONTENT to the event log (it can create the file, and
chmods it exactly as append_event does — see check_eventlog_writable). Safe to poll during a
live call — which s14b does.

Scope honesty (do not over-read a green report): the provider stanza runs the shared
`probes` module, whose endpoints are AUTH-METADATA only (Deepgram `/auth/token`, ElevenLabs
`/v1/user`, OpenRouter `/api/v1/key`). "ready" therefore means *this credential
authenticates from this process*, NOT that Deepgram's live WebSocket or the ElevenLabs
stream will work. Only a real call proves that.
"""
import logging
import os

from voicecore import cascade_config
from voicecore import eventlog
from voicecore import probes

logger = logging.getLogger("mode-v.readiness")

# The cascade roles this bridge must be able to serve. Kept in the same order the pipeline
# runs so a failing report reads like the call would have failed.
CASCADE_ROLES = ("stt", "llm", "tts")


def _check(status: str, detail: str, **extra) -> dict:
    """Same shape as the dashboard's dry-run stanzas, so s14a-2b can render this report
    beside its own checks without a translation layer."""
    return {"status": status, "detail": detail, **extra}


def check_live_wiring(doc: dict, registry: dict, env: dict) -> dict:
    """The `cunt`-incident gate, read from the REAL config builder's wired flags.

    A provider can be schema-valid AND keyed AND still have no live streaming client, in
    which case the bridge refuses the call at dial time. The Talk lane never had this check
    — `run_dry_run_talk` has no live_wiring stanza at all — so a bench-only STT provider
    could pass every dashboard check and die on the first real call.
    """
    try:
        config = cascade_config.build_cascade_config(doc, registry, dict(env))
    except cascade_config.CascadeConfigError as exc:
        return _check("fail", f"would fail: {exc}")

    stt, llm, tts = config["stt"], config["llm"], config["tts"]
    problems = []
    if not stt.get("wired_live"):
        problems.append(
            f"stt provider '{stt['provider']}' has no LIVE streaming client — this bridge "
            "refuses the call (live STT = elevenlabs-scribe or deepgram; this provider is "
            "bench-only)")
    if not (llm.get("wired") and llm.get("endpoint")):
        problems.append(
            f"llm provider '{llm['provider']}' has no wired cascade chat client")
    if not tts.get("wired_live"):
        problems.append(
            f"tts provider '{tts['provider']}' has no LIVE streaming client — this bridge "
            "refuses the call (live TTS = elevenlabs or deepgram-aura)")
    if problems:
        return _check("fail", "would fail: " + "; ".join(problems))
    return _check(
        "pass",
        f"all three stages have live clients in THIS process (stt {stt['provider']} / "
        f"llm {llm['provider']} / tts {tts['provider']})")


async def check_providers(doc: dict, registry: dict, env: dict, transport=None) -> dict:
    """Per-role readiness via the SHARED probe module, against THIS process's env.

    Deliberately `probes.probe_batch` and not a local reimplementation: a duplicated
    credential lookup is exactly how a dry-run drifts from the lane it claims to model.
    Probe discipline carries over unchanged — keyless resolves to `needs_key` with ZERO
    network, and detail strings are composed here from status codes only, never from an
    upstream body, so no credential can reach the payload.
    """
    roles = doc.get("providers") or {}
    entries, roles_of = [], {}
    for role in CASCADE_ROLES:
        pid = roles.get(role)
        if pid is None:
            return _check("fail", f"draft names no provider for role '{role}'", roles=[])
        entry = registry.get(pid)
        if entry is None:
            return _check("fail",
                          f"role '{role}' names unknown provider '{pid}'", roles=[])
        if pid not in roles_of:
            entries.append(entry)
        roles_of.setdefault(pid, []).append(role)

    results = await probes.probe_batch(entries, env=env, transport=transport)
    rows, failures = [], []
    for pid, result in results.items():
        for role in roles_of[pid]:
            rows.append({"role": role, "provider": pid, "status": result.status,
                         "detail": result.detail})
            if result.status != "ready":
                failures.append(f"{role} ({pid}): {result.status}")
    if failures:
        return _check("fail",
                      "provider not ready in THIS process — " + "; ".join(sorted(failures)),
                      roles=rows)
    return _check("pass",
                  f"all {len(rows)} cascade role(s) authenticate from this process",
                  roles=rows)


def check_eventlog_writable(path: str = None) -> dict:
    """Can THIS uid actually append to the shared event log?

    This is a readiness question, not an observability one. The bridge runs as uid 1000
    (pwuser, from the Playwright base image) while the shared log was created root:root by
    mode-c — and `eventlog.append_event` SWALLOWS the resulting EACCES. So the Talk lane
    lost its entire call record SILENTLY: of 22 historical records, ZERO had mode="talk".
    A readiness report blind to this green-lights a campaign that records nothing.

    Proven by ATTEMPTING the append, not by reading mode bits: ownership/mode is a poor
    proxy for "this process can write" (root bypasses permissions entirely, and a
    mode-permissive file on a read-only mount still fails). The probe writes no CONTENT, so
    the log is never polluted.

    ⚠️ It can still CREATE the file (open-for-append does), which makes this probe a writer
    in the create race — so it must apply the same 0o666 that `eventlog.append_event` does.
    Without that, a readiness poll landing before the first real event would leave a 0644
    file owned by THIS uid and lock out the other bridge: precisely the bug this stanza
    exists to detect, reintroduced by the detector. Caught by the s14a-2a evaluator.
    """
    target = path or eventlog.DEFAULT_PATH
    try:
        parent = os.path.dirname(target) or "."
        os.makedirs(parent, exist_ok=True)
        existed = os.path.exists(target)
        with open(target, "a", encoding="utf-8"):
            pass          # open-for-append succeeded; write nothing — no content added
        if not existed:
            try:
                os.chmod(target, 0o666)
            except OSError:
                logger.warning("readiness created the event log but chmod failed (path=%s)",
                               target)
    except OSError as exc:
        return _check("fail",
                      f"cannot append to the event log at {target} ({exc.__class__.__name__}"
                      f": {exc.strerror or 'error'}) — call records would be lost SILENTLY, "
                      "because append_event swallows this")
    return _check("pass", f"event log at {target} is appendable by this process "
                          f"(uid {os.getuid()})")


async def cascade_readiness(doc: dict, registry: dict, env: dict, *, transport=None,
                            eventlog_path: str = None) -> dict:
    """The full readiness report. `ready` is the conjunction of every stanza.

    `env` is passed in rather than read from `os.environ` here so the caller states which
    environment it is asking about — the route hands it THIS process's env, and tests hand
    it a constructed one. A stanza that silently fell back to a global would defeat the
    entire purpose of the endpoint.
    """
    checks = {
        "live_wiring": check_live_wiring(doc, registry, env),
        "providers": await check_providers(doc, registry, env, transport=transport),
        "eventlog_writable": check_eventlog_writable(eventlog_path),
    }
    return {
        "ready": all(c["status"] == "pass" for c in checks.values()),
        "bridge": "mode-v",
        "agent": doc.get("id"),
        "checks": checks,
    }
