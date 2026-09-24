"""The ONE dashboard placement of an outbound Call (ticket 09, ticket 11).

A Place-a-call request is one shot: the Agent travels in the body. This
module never reads or writes ``active.yaml``. It does not run a dry-run.
It does not consult an allow-list. The guard is the gateway bearer on
the bridge, plus the callee-side tool sandbox.

``place_phone_call`` POSTs ``/voice/outbound`` on mode-c. The dashboard
itself never touches the Twilio Calls API.

**Ticket 11 — there is one placement, not two.** A Schedule is a Call that
has not happened yet, so when it fires it must ring the phone the same way
the button does: same validation, same refusals, same Agent snapshot
binding, same disclosure handling, same dial. ``place_from_request`` is
that one path. ``POST /api/calls/place`` is a thin HTTP wrapper around it
and the scheduler calls it directly — neither owns a second copy of any of
it. A refusal is a ``PlaceRejected``, carrying the status the HTTP route
answers with and the exact words the screen shows, so a failed Schedule's
recorded reason is the same sentence a person would have read.
"""
import os

import httpx

from voicecore import profiles
from voicecore.dial_request import build_dial_payload
from voicecore.e164 import normalize_e164

__all__ = ["PlaceRejected", "PlaceRequest", "normalize_e164", "place_from_request",
           "place_phone_call", "safe_agent_id", "validate_place_request"]


class PlaceRejected(Exception):
    """This Call cannot be placed, and why — in the words the owner reads.

    ``status`` is the HTTP status ``POST /api/calls/place`` answers with
    (422 malformed, 409 the Agent cannot run or the bridge refused, 502 the
    bridge is unreachable or broke). The scheduler stores ``reason`` on the
    Schedule verbatim, which is why the text has to read as a sentence and
    not as a code.
    """

    def __init__(self, status: int, detail: "list[str]"):
        self.status = status
        self.detail = list(detail)
        super().__init__("; ".join(self.detail))

    @property
    def reason(self) -> str:
        return "; ".join(self.detail)


def safe_agent_id(agent_id) -> bool:
    """The s1 id rule, plus a dashboard-only refusal of extension-smuggling ids
    (x.yaml/x.yml) — REJECTED loudly, never sanitized into a different id."""
    if not isinstance(agent_id, str) or not profiles._ID_RE.match(agent_id):
        return False
    return not agent_id.endswith((".yaml", ".yml"))


class PlaceRequest:
    """A validated placement: the four things a Call needs, canonicalized.

    Built only by ``validate_place_request``. ``to`` is already E.164, the
    Mission is already stripped and non-empty, ``disclose`` is already a
    bool — so everything downstream of validation handles one shape.
    """

    __slots__ = ("agent", "to", "mission", "disclose", "target_display")

    def __init__(self, *, agent: str, to: str, mission: str, disclose: bool,
                 target_display: str = ""):
        self.agent = agent
        self.to = to
        self.mission = mission
        self.disclose = disclose
        self.target_display = target_display

    def as_body(self) -> dict:
        """The request shape again — what a caller would have POSTed to get this."""
        return {"agent": self.agent, "to": self.to, "mission": self.mission,
                "disclose": self.disclose, "target_display": self.target_display}

    def __eq__(self, other):
        return isinstance(other, PlaceRequest) and self.as_body() == other.as_body()

    def __repr__(self):
        return f"PlaceRequest({self.as_body()!r})"


def validate_place_request(body) -> PlaceRequest:
    """Grade a placement body. Raises PlaceRejected(422) with the whole reason.

    Shared by ``POST /api/calls/place`` and ``POST /api/schedules``: a Schedule
    that could never be placed is refused when it is written, not silently at
    3pm when nobody is watching.
    """
    if not isinstance(body, dict):
        raise PlaceRejected(422, ["request body must be a JSON object"])

    agent_id = body.get("agent")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise PlaceRejected(422, ["agent: required — the Agent that will speak"])
    agent_id = agent_id.strip()
    if not safe_agent_id(agent_id):
        raise PlaceRejected(422, [f"agent: {agent_id!r} is not a valid agent id"])

    to = body.get("to")
    if not isinstance(to, str) or not to.strip():
        raise PlaceRejected(422, ["to: required target number"])
    to_norm = normalize_e164(to.strip())
    if to_norm is None:
        raise PlaceRejected(422, [f"to: {to!r} is not a valid E.164 number"])

    mission = body.get("mission")
    if not isinstance(mission, str) or not mission.strip():
        raise PlaceRejected(422, ["mission: required — what this Call is for"])
    mission = mission.strip()

    if "disclose" in body and not isinstance(body.get("disclose"), bool):
        raise PlaceRejected(422, ["disclose: must be a boolean"])
    disclose = body.get("disclose") is True

    target_display = body.get("target_display")
    target_display = (target_display.strip()
                      if isinstance(target_display, str) else "")

    return PlaceRequest(agent=agent_id, to=to_norm, mission=mission,
                        disclose=disclose, target_display=target_display)


def check_agent_can_run(request: PlaceRequest, env=None) -> None:
    """Refuse an Agent that cannot run this Call, in the bridge's own words.

    The bridge makes the same check again at dial time (it is the one that
    binds the snapshot); this one exists so the refusal is a 409 with a
    reason rather than a bridge 409 relayed as a 502.
    """
    try:
        profiles.load_named_profile(request.agent, "outbound", env=env)
    except profiles.ProfileError as exc:
        raise PlaceRejected(409, [f"cannot place: agent '{request.agent}' cannot "
                                  f"run outbound: {exc}"]) from exc


async def place_phone_call(*, to: str, mission: str, agent: str,
                           disclose: bool, target_display: str = "",
                           env=None, transport=None) -> "tuple[int, dict]":
    """Dial the phone bridge. Returns (status_code, body-dict).

    Transport errors surface as (0, {error}) so the route can say the
    bridge was unreachable rather than pretending a call was placed.
    """
    env = os.environ if env is None else env
    token = (env.get("HERMES_GATEWAY_TOKEN") or "").strip()
    base = (env.get("VOICE_MODE_C_URL") or "http://127.0.0.1:3336").rstrip("/")
    url = base + "/voice/outbound"
    payload = build_dial_payload(to=to, mission=mission, agent=agent,
                                 disclose=disclose, target_display=target_display)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=30.0) as client:
            resp = await client.post(
                url,
                headers=({"Authorization": f"Bearer {token}"} if token else {}),
                json=payload,
            )
    except httpx.HTTPError as exc:
        return 0, {"error": f"the phone bridge is unreachable at {base} "
                            f"({type(exc).__name__}) — no call placed"}
    try:
        body = resp.json() or {}
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return resp.status_code, body


async def place_validated(request: PlaceRequest, *, env=None,
                          transport=None) -> dict:
    """Dial a request that has already been graded, and grade the answer."""
    code, placed = await place_phone_call(
        to=request.to, mission=request.mission, agent=request.agent,
        disclose=request.disclose, target_display=request.target_display,
        env=env, transport=transport)
    if code != 200:
        err = placed.get("error", "")
        status = 409 if code == 409 else 502
        raise PlaceRejected(status, [
            (f"the phone bridge refused the dial (HTTP {code})"
             if code else "the phone bridge is unreachable")
            + (f": {err}" if err else "")])
    return {
        "placed": True,
        "call_sid": placed.get("call_sid"),
        "call_id": placed.get("call_id"),
        "agent": request.agent,
        "to": request.to,
        "mission": request.mission,
        "disclose": request.disclose,
    }


async def place_from_request(body, *, env=None, transport=None) -> dict:
    """Validate, refuse, dial — THE placement, for every caller.

    The button and the scheduler both end up here with the same body shape.
    Anything that happens on the way to the phone happens here or it happens
    to neither of them.
    """
    request = validate_place_request(body)
    check_agent_can_run(request, env=env)
    return await place_validated(request, env=env, transport=transport)
