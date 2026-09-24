"""Mode V — Nextcloud Talk voice bridge control API.

Thin FastAPI surface over CallSession (single-call orchestration) and ApprovalStore
(guest-escalation single-slot). Call detection and human-hangup detection both live in
the plugin (running inside the Hermes gateway) — it POSTs here to start/stop a call and
polls/resolves pending owner approvals. This module owns no OCS/Talk polling itself.
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse

import config
import outbound
from voicecore import hermes_gateway
from voicecore import lkg
from voicecore import profiles
import readiness
from approval import ApprovalStore
from browser import TalkBrowser
from outbound import OutboundMission
from session import CallSession

# Emit INFO from all mode-v.* loggers to stderr (uvicorn only configures its own loggers,
# so without this the browser/session/bridge INFO lines — greeting, OpenAI events, WebRTC
# stats — are dropped by logging's WARNING-level "last resort" handler).
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger("mode-v.server")

# VC24: this bridge hosts the cascade lane on the Talk Outlet (over parec+pacat), both
# directions. profiles.activation_problem still refuses inbound for an outside-vendor
# cascade; only the direct Hermes lane answers a Talk call, and CallSession.start keeps
# it for the owner (a guest stays on the Realtime lane with the approval loop).
profiles.declare_cascade_host(profiles.OUTLET_TALK, ("outbound", "inbound"))

# s3: the process-wide Config is the pure env-derived BASE — the active profile (env
# VOICE_AGENT or the active.yaml pointer) is resolved freshly per call setup in
# CallSession.start and overlaid there, so profile edits land on the NEXT call.
_cfg = config.load_base()
_approvals = ApprovalStore()
_browser = TalkBrowser(_cfg)
_session = CallSession(_cfg, _browser, _approvals)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _browser.start()
    logger.info("TalkBrowser started")
    try:
        yield
    finally:
        # Drain any active call first (stops the bridge + leaves the call cleanly) rather
        # than leaving parec/pacat to loop-cancellation, then shut the browser down.
        if _session.active_token:
            await _session.stop(_session.active_token)
        await _browser.stop()
        logger.info("TalkBrowser stopped")


app = FastAPI(title="Mode V — Nextcloud Talk voice bridge", lifespan=lifespan)


@app.post("/call/start")
async def call_start(body: dict):
    ok = await _session.start(body["token"], body.get("trust", "guest"),
                              body.get("caller", ""), body.get("caller_display", ""))
    return JSONResponse({"joined": ok}, status_code=200 if ok else 409)


@app.post("/call/stop")
async def call_stop(body: dict):
    await _session.stop(body["token"])
    return {"stopped": True}


@app.post("/call/outbound")
async def call_outbound(body: dict, authorization: str = Header(default="")):
    """Place an autonomous OUTBOUND call: ring a Talk user and run a SANDBOXED mission.

    Owner-only by construction: this endpoint requires the gateway bearer token, so only the
    Hermes gateway (which the owner drives) can trigger a call — not any other LAN actor. The
    on-call model itself is tool-less and mission-only (see outbound.build_outbound_prompt /
    RealtimeBridge), so a callee has no path back to the owner's data regardless.

    Body: {brief|objective, token|target, report_channel?, report_address?, target_display?}.
    """
    # Fail-closed (VC24): an unset token refuses every caller. It used to skip the check.
    refusal = hermes_gateway.bearer_problem(authorization, _cfg.hermes_gateway_token)
    if refusal is not None:
        return JSONResponse({"error": refusal[1]}, status_code=refusal[0])

    brief = (body.get("brief") or body.get("objective") or "").strip()
    if not brief:
        return JSONResponse({"error": "missing brief/objective"}, status_code=400)

    # s11b-1: TYPED target — a room token (`token`, incl. group) is dialed VERBATIM; a
    # username (`target`) is OCS-resolved to a 1:1 room. The kind is the explicit body
    # key, never format-guessed off the string.
    raw_token = (body.get("token") or "").strip()
    raw_target = (body.get("target") or "").strip()
    if raw_token:
        kind, dial_id = "token", raw_token
    elif raw_target:
        kind, dial_id = "username", raw_target
    else:
        return JSONResponse({"error": "need a token or a target"}, status_code=400)

    # s11b-1: talk_policy.allow — the pre-dial gate, matched on the DIALED FORM BEFORE any
    # OCS resolve (a denied dial creates no room). A selected outbound profile carrying the
    # field REPLACES the allow-any Mode V posture ([] = deny-all); absent / no profile
    # leaves Talk allow-any. number_policy.allow (E.164) is Twilio-only — never consulted here.
    # s8 (LKG): a broken outbound assignment falls back to the last-known-good snapshot
    # here, so the gate grades the profile the call will actually run.
    try:
        gate_profile = lkg.resolve("outbound", outlet=profiles.OUTLET_TALK)
    except profiles.ProfileError as exc:
        logger.error("Refusing outbound dial — active voice profile failed to load: %s", exc)
        return JSONResponse({"error": f"active voice profile failed to load: {exc}"},
                            status_code=500)
    talk_allow = gate_profile.talk_allow_list() if gate_profile is not None else None
    if not profiles.talk_allow_decision(kind, dial_id, talk_allow):
        logger.warning("Refusing outbound Talk call to %s %r — not in the profile's "
                       "talk_policy.allow list", kind, dial_id)
        return JSONResponse(
            {"error": f"{kind} {dial_id} is not in the profile's talk_policy.allow list"},
            status_code=403)

    # Resolve AFTER the gate: a token dials verbatim, a username → OCS 1:1 room.
    if kind == "token":
        token = dial_id
    else:
        try:
            token = await outbound.resolve_room(_cfg, dial_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to resolve room for target %r", dial_id)
            return JSONResponse({"error": f"could not resolve target: {exc}"}, status_code=502)

    mission = OutboundMission(
        brief=brief,
        report_channel=body.get("report_channel", "talk"),
        report_address=body.get("report_address", "") or _cfg.home_room,
        target_display=body.get("target_display", ""),
    )
    ok = await _session.start(token, "outbound", "", mission.target_display, mission=mission)
    body = {"placed": ok, "token": token}
    if not ok:
        # s14b: say WHY. A bare {"placed": false} + 409 was read downstream as "busy"
        # for every cause, including a hard crash in bridge setup.
        body["failure"] = _session.last_start_failure or {
            "code": "unknown", "detail": "start() refused without recording a reason"}
    return JSONResponse(body, status_code=200 if ok else 409)


@app.get("/health")
async def health():
    return {"status": "ok", "busy": _session.busy, "active_token": _session.active_token,
            "trust": _session.trust}


@app.get("/readiness/cascade")
async def readiness_cascade(authorization: str = Header(default="")):
    """s14a-2a: can THIS process run a cascade call right now?

    The dashboard's Talk dry-run cannot answer this — it executes inside voice-control and
    so resolves every provider against voice-control's env. That blindness is what let
    s14a-1 find this bridge carrying ZERO cascade keys while the dry-run showed green.

    Owner-only (same gateway bearer as /call/outbound): it reports on credential state.
    READ-ONLY — no call, no room, no slot lock, no log write — so it is safe to poll during
    a live call, which s14b does. Reports on the ACTIVATED OUTBOUND profile, i.e. the exact
    profile a fire would dial (D9: resolved fresh, never cached).
    """
    # Fail-closed (VC24): an unset token refuses every caller. It used to skip the check.
    refusal = hermes_gateway.bearer_problem(authorization, _cfg.hermes_gateway_token)
    if refusal is not None:
        return JSONResponse({"error": refusal[1]}, status_code=refusal[0])
    try:
        active = profiles.load_effective_profile(
            "outbound", outlet=profiles.OUTLET_TALK)
    except profiles.ProfileError as exc:
        return JSONResponse(
            {"ready": False, "bridge": "mode-v", "error": f"activation: {exc}"},
            status_code=200)
    if active is None:
        return JSONResponse(
            {"ready": False, "bridge": "mode-v",
             "error": "no activated outbound profile — nothing to report readiness for"},
            status_code=200)
    doc = active.doc
    if doc.get("pipeline") != "cascade":
        return JSONResponse(
            {"ready": False, "bridge": "mode-v", "agent": doc.get("id"),
             "error": f"activated outbound profile is pipeline "
                      f"{doc.get('pipeline')!r}, not cascade"},
            status_code=200)
    report = await readiness.cascade_readiness(
        doc, profiles.load_registry(), os.environ)
    return JSONResponse(report, status_code=200)


@app.get("/voice/pending-approval")
async def pending():
    return {"pending": _approvals.get_pending()}


@app.post("/voice/approval-verdict")
async def verdict(body: dict):
    ok = _approvals.resolve(body["approval_id"], body["decision"])
    return {"resolved": ok}
