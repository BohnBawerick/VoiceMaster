"""
Mode C — Single-layer OpenAI Realtime S2S with Hermes agent backend.

Architecture:
  Twilio -> cloudflared -> localhost:3336 -> OpenAI Realtime API (S2S)
  Tool calls -> Hermes gateway API (HTTP POST)

Forked from OpenClaw Mode C. Backend replaced: openclaw agent CLI -> Hermes gateway HTTP.

Port: 3336
"""
import os
import json
import time
import base64
import asyncio
import contextvars
import functools
import logging
import secrets
from pathlib import Path
from datetime import datetime

import httpx
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.request_validator import RequestValidator
from twilio.rest import Client
import uvicorn

from voicecore import call_record
from voicecore import cascade_config
from voicecore import cascade_live
from voicecore import eventlog
from voicecore import hermes_gateway
from voicecore import hermes_voice
from voicecore import hindsight
from voicecore import lkg
from voicecore import outbound_request
from voicecore import profiles
from voicecore import recording
from voicecore import summary as call_summary
from voicecore import turn_detect
from outbound import (OutboundMission, build_outbound_prompt,
                      build_persona_outbound_prompt, deliver_transcript)

load_dotenv()

# VC24: mode-c hosts the cascade lane on the phone Outlet, both directions. Inbound is
# still refused for an outside-vendor cascade by profiles.activation_problem; only the
# direct Hermes lane (providers.llm: hermes-agent) answers a call.
profiles.declare_cascade_host(profiles.OUTLET_PHONE, ("outbound", "inbound"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mode-c")

# --- Configuration ---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
VOICE = os.environ.get("OPENAI_VOICE", "cedar")
OPENAI_MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime")
# Idea 3: input-transcription model for the Realtime session (drives the report-back + memory
# transcripts). gpt-4o-transcribe is the same $/min as whisper-1 but stronger on accents / proper
# nouns / technical vocab — most of what we say to Robot. Env-overridable (whisper-1 is the fallback).
TRANSCRIPTION_MODEL = os.environ.get("VOICE_TRANSCRIPTION_MODEL", "gpt-4o-transcribe")
# Idea 1: dead-air filler. While a slow hermes tool call runs (~29s for a Gmail lookup), speak a
# short line so Robot isn't mute. Debounced — only spoken if the backend hasn't returned within the
# window, so trivial sub-2s turns stay snappy. Env-tunable, no rebuild.
FILLER_TEXT = os.environ.get("VOICE_FILLER_TEXT", "One sec, let me check that.")
FILLER_DEBOUNCE_S = float(os.environ.get("VOICE_FILLER_DEBOUNCE_MS", "1500")) / 1000.0
# Idea 4: retain call transcripts into the call archive. With HINDSIGHT_URL set that is a
# Hindsight bank (s5, ticket 05: default `voice`, this app's OWN archive, not the shared
# `hermes` gateway-session bank); unset, it is the built-in SQLite store (voicecore.call_store).
HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", hindsight.DEFAULT_URL)
HINDSIGHT_BANK = os.environ.get("HINDSIGHT_BANK", hindsight.DEFAULT_BANK)
RETAIN_ENABLED = os.environ.get("VOICE_RETAIN_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
CONFIG_DIR = Path(os.environ.get("HERMES_CONFIG_DIR", "/app/config"))
PORT = int(os.environ.get("PORT", "3336"))

# Hermes gateway connection
HERMES_GATEWAY_URL = os.environ.get("HERMES_GATEWAY_URL", "http://hermes:18789")
HERMES_GATEWAY_TOKEN = os.environ.get("HERMES_GATEWAY_TOKEN", "")


def gateway_url_for_profile(profile: str) -> "str | None":
    """Base gateway URL for a hermes_profile name; None = not routable (the caller
    must fail honestly - never silently fall back to the default profile's backend).
    The resolution itself is voicecore's, shared with the Talk bridge and the dashboard:
    env map, then ``default``, then the registry the Hermes supervisor writes."""
    return hermes_gateway.gateway_url_for_profile(profile, default_url=HERMES_GATEWAY_URL)


# Twilio signature verification.
# TWILIO_AUTH_TOKEN is the account's primary token — the REST credential (see Client() below).
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
# Twilio signs each webhook with the auth token of the REGION that handled the call, and a
# regional token is a DIFFERENT secret from the primary one (it won't even authenticate REST).
# Our AU number is handled in the AU region, so real inbound webhooks arrive signed with the AU
# token while outbound REST still needs the primary — one token cannot serve both roles. Hence a
# set of accepted signing tokens, kept separate from the REST credential. Defaults to the primary
# token alone, so a single-region deploy needs no extra config.
TWILIO_SIGNING_TOKENS = tuple(
    t.strip() for t in os.environ.get("TWILIO_SIGNING_TOKENS", TWILIO_AUTH_TOKEN).split(",")
    if t.strip()
)

# Comma-separated E.164 allow-list of CALLERS who may reach the inbound (full-tool) Robot.
# FAIL-CLOSED by design — the opposite default of ALLOWED_OUTBOUND: empty/unset rejects
# EVERY caller. The inbound session carries the owner's full backend (hermes_agent tool),
# so "nobody configured" must mean "nobody gets in", never "everybody does".
ALLOWED_CALLERS = frozenset(
    n.strip() for n in os.environ.get("VOICE_INBOUND_ALLOWED_CALLERS", "").split(",") if n.strip())

# Twilio outbound (autonomous calling) — see outbound.py + POST /voice/outbound.
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")
# Comma-separated E.164 allow-list of numbers Hermes may DIAL. Empty => allow any (the Talk/Mode V
# posture). Seeded with the owner's mobile so the first live call is self-contained; PSTN reaches
# real phones and costs real money, so the allow-list is the accidental/injected-call guard.
ALLOWED_OUTBOUND = frozenset(
    n.strip() for n in os.environ.get("VOICE_OUTBOUND_ALLOWED_NUMBERS", "").split(",") if n.strip())
# When true the sandboxed outbound prompt opens with an automated-caller disclosure (off = parity
# with Mode V; the mission brief sets the identity). Env-toggleable, no rebuild.
AI_DISCLOSURE = os.environ.get("VOICE_OUTBOUND_AI_DISCLOSURE", "false").strip().lower() in (
    "1", "true", "yes", "on")

# Public host Twilio uses to reach this service (behind the Cloudflare tunnel,
# request.url reflects the internal http origin — NOT the public https URL that
# Twilio signed its webhook against, nor the host Twilio must dial back for the
# Media Stream). Pin it so signature validation and the <Stream> URL are correct.
# Empty in local dev → fall back to request.url (direct, unproxied).
PUBLIC_HOST = os.environ.get("VOICE_PUBLIC_HOST", "")

# --- Effective realtime config (s1 voice profiles + s3 active pointer) ---

# s3 TOCTOU rule 3: media_stream loads the active profile EXACTLY ONCE per call setup
# and threads that snapshot through URL and session.update building. The snapshot rides
# this contextvar because _send_session_update's signature is frozen by the s1 suites.
# _UNSET (not None!) means "no snapshot threaded — resolve yourself"; None is a real
# snapshot meaning no-profile.
# Both are reload-stable (globals().get): the parity harness importlib.reload()s this
# module, and sentinels/contextvars bound before the reload must keep their identity.
_UNSET = globals().get("_UNSET", object())
_CALL_PROFILE: contextvars.ContextVar = globals().get(
    "_CALL_PROFILE") or contextvars.ContextVar("mode_c_call_profile")


def _resolve_profile(profile=_UNSET, direction: str = "inbound"):
    """The profile snapshot for the current build: explicit arg > the per-call
    contextvar (set by media_stream) > a fresh one-shot load for ``direction``.
    s16: this bridge IS the phone Outlet, so every load resolves that outlet."""
    if profile is not _UNSET:
        return profile
    snapshot = _CALL_PROFILE.get(_UNSET)
    if snapshot is not _UNSET:
        return snapshot
    return profiles.load_effective_profile(direction, outlet=profiles.OUTLET_PHONE)


def _current_hermes_profile() -> str:
    """The hermes_profile of the call's snapshot, defaulting to "default" — for
    profile-less calls AND profiles that omit the field. Reads only the per-call
    contextvar; never triggers a file load (the tool path must not re-read pointers)."""
    snapshot = _CALL_PROFILE.get(_UNSET)
    if snapshot is _UNSET or snapshot is None:
        return "default"
    return _profile_of_snapshot(snapshot)


def _profile_of_snapshot(snapshot) -> str:
    doc = getattr(snapshot, "doc", None) or {}
    name = doc.get("hermes_profile")
    return name.strip() if isinstance(name, str) and name.strip() else "default"


def _summariser_for(snapshot):
    """Ticket 06: the post-call summariser for THIS call's Agent, or None when off.

    Same resolution as the in-call `hermes_agent` tool - `hermes_profile` picks the
    gateway - so the summary is written by the Agent that was actually on the call, using
    that Agent's own configuration. An unknown profile resolves to no gateway, which the
    summariser reports as `unavailable` rather than quietly routing to another Agent.
    """
    return call_summary.make_summariser(
        gateway_url=gateway_url_for_profile(_profile_of_snapshot(snapshot)),
        token=HERMES_GATEWAY_TOKEN)


def _effective_realtime_config(profile=_UNSET, direction: str = "inbound") -> dict:
    """The realtime knobs the session builder + URL actually use.

    No profile selected (VOICE_AGENT unset/blank) => EXACTLY the module constants and
    the coded VAD numbers below — profiles.load_active_profile touches no files on
    that path, so VOICE_CONFIG_DIR may be missing entirely (s1 drop-in invariant).
    With a profile: precedence per knob is profile > env > registry default_knobs >
    coded default (the constant passed as fallback is "env if set else coded default").
    A selected but broken profile raises profiles.ProfileError — loud, no env fallback.
    s3: selection is env VOICE_AGENT first, else the active.yaml pointer for the call
    direction (profiles.load_effective_profile); media_stream threads its one per-call
    snapshot in via ``profile``/the contextvar so URL and payload share a single load.
    """
    profile = _resolve_profile(profile, direction)
    if profile is None:
        return {
            "voice": VOICE,
            "model": OPENAI_MODEL,
            "transcription_model": TRANSCRIPTION_MODEL,
            "vad_threshold": 0.5,
            "vad_prefix_padding_ms": 300,
            "vad_silence_ms": 500,
            "persona": "",
        }
    return {
        "voice": profile.resolve(("voice",), "OPENAI_VOICE", VOICE),
        "model": profile.resolve(("model",), "OPENAI_REALTIME_MODEL", OPENAI_MODEL),
        "transcription_model": profile.resolve(
            ("transcription_model",), "VOICE_TRANSCRIPTION_MODEL", TRANSCRIPTION_MODEL),
        "vad_threshold": profile.resolve(("vad", "threshold"), None, 0.5),
        "vad_prefix_padding_ms": profile.resolve(("vad", "prefix_padding_ms"), None, 300),
        "vad_silence_ms": profile.resolve(("vad", "silence_ms"), None, 500),
        "persona": profile.persona,
    }


def _realtime_url(profile=_UNSET, direction: str = "inbound") -> str:
    """The full wss URL media_stream dials (model honors the same precedence chain)."""
    return ("wss://api.openai.com/v1/realtime?model="
            f"{_effective_realtime_config(profile, direction)['model']}")


# --- System prompt builder ---


def _read_file(path: Path) -> str:
    """Read a file, return empty string if missing."""
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


def build_system_prompt() -> str:
    """Build full system prompt from config directory identity files.

    Reads:
    1. SOUL.md - who you are (personality and values)
    """
    soul = _read_file(CONFIG_DIR / "SOUL.md")

    prompt = f"""You are on a live phone call. You ARE Robot — the Hermes agent.
You have the same personality, memory, and capabilities as when you talk on Telegram.

== YOUR SOUL ==
{soul}

== VOICE CALL RULES ==
- You are speaking out loud on a phone call, not typing. Be conversational.
- Keep responses concise (1-3 sentences for simple things, longer only when needed).
- Use natural speech patterns — contractions, backchanneling ("right", "got it").
- Never output markdown, code blocks, bullet lists, or formatting — speak plainly.
- If you need to do something that requires tools (files, web search, infrastructure,
  messages, email, code, checking servers, docker, anything beyond pure conversation),
  use the "hermes_agent" tool. It connects to your full Hermes agent backend
  which has access to everything — same as when you get a Telegram message.
- When calling a tool, tell the user briefly: "Let me check that" / "One moment" / etc.
- Report tool results conversationally — summarise, don't read raw data.
- You can call the tool multiple times in a conversation if needed.
- Current date/time: {datetime.now().strftime("%A, %d %B %Y, %H:%M %Z")}
"""
    return prompt


def _outbound_base_prompt(snapshot, mission) -> str:
    """s8b c1: pick the base prompt for an OUTBOUND on-call session.

    A profile carrying a ``persona`` is a TRUSTED roleplay (the Fire arm dials it only to
    an allow-listed number, and ``on_call_tools`` may hand it the real backend) — it gets
    a persona-first frame with NO containment. A persona-LESS outbound profile is a mission
    call to an UNTRUSTED third party and keeps the hard containment sandbox
    (``build_outbound_prompt``). Either way the profile persona is appended downstream by
    ``_send_session_update`` (base + "\\n\\n" + persona), so ``build_persona_outbound_prompt``
    deliberately omits it.
    """
    # s11a L1: containment ONLY for a mission-only, persona-less, tools-OFF call. A
    # persona OR on_call_tools yields the no-containment base — otherwise a tool-capable
    # call would carry a prompt that flatly claims "you have NO tools".
    tools_on = snapshot is not None and snapshot.on_call_tools
    disclose = (
        mission.disclose if mission is not None and mission.disclose is not None
        else AI_DISCLOSURE
    )
    if (snapshot is not None and snapshot.persona) or tools_on:
        return build_persona_outbound_prompt(
            mission.brief, mission.target_display, disclose)
    return build_outbound_prompt(mission.brief, mission.target_display, disclose)


# --- Tools exposed to OpenAI Realtime ---

TOOLS = [
    {
        "type": "function",
        "name": "hermes_agent",
        "description": (
            "Execute a request through the Hermes agent backend. This gives you "
            "full access to ALL of Robot's capabilities — the same tools available "
            "via Telegram. Use this for ANY request that goes beyond pure conversation: "
            "reading/writing files, web search, checking email, calendar, sending messages, "
            "infrastructure checks (NAS, pfSense, Docker, servers), code execution, "
            "project work, memory updates, or anything that requires action. "
            "Pass a clear natural-language instruction describing what to do. "
            "The backend agent has full workspace access and will execute autonomously."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": (
                        "Clear natural-language instruction for the Hermes agent. "
                        "Be specific about what to do and what information to return. "
                        "Examples: 'Check if there are any running Docker containers on the NAS', "
                        "'What meetings do I have tomorrow?', 'Read the file /home/jamie/PROJECTS/foo/README.md', "
                        "'Search the web for latest Python 3.13 release date'"
                    ),
                }
            },
            "required": ["instruction"],
        },
    },
]

# s11a L1: appended to an OUTBOUND session's instructions EXACTLY when tools are open
# (on_call_tools). The persona/context base never mentions hermes_agent, so without this
# the model holds a tool it was never told about. Cut when tools are closed (the sandbox
# base says the opposite: "you have NO tools").
CAPABILITY_STANZA = (
    "== YOUR CAPABILITIES ==\n"
    "You can act for your operator during this call via the hermes_agent tool — files, "
    "web, infrastructure, messages, code, and memory (the same reach you have on "
    "Telegram). When a request needs it, say a brief holding phrase ('one moment'), call "
    "hermes_agent, then report the result conversationally."
)

# --- Hermes agent execution ---


async def call_hermes_agent(instruction: str, profile: str = "default") -> str:
    """Execute an instruction through the Hermes gateway API.

    Makes an HTTP POST to the OpenAI-compatible chat endpoint of the gateway serving
    ``profile`` (each Hermes profile runs its own gateway — see
    gateway_url_for_profile). An unknown profile fails honestly, zero network.
    """
    gateway_url = gateway_url_for_profile(profile)
    if gateway_url is None:
        logger.error("hermes_profile '%s' is not routable (not in "
                     "HERMES_PROFILE_GATEWAY_URLS, not ok in gateways.json) - refusing "
                     "the tool call", profile)
        return (f"The '{profile}' backend profile is not connected on this line, so I "
                "can't run that request right now.")
    # Skill-discovery nudge (mirrors Mode V's services/talk-voice-bridge/hermes.py): a bare
    # one-shot makes the backend answer from its native tool list and say "I can't" — it never
    # runs `skills_list` to find capabilities delivered as skills (Gmail via the google-workspace
    # skill, etc.). Telling it to discover + actually execute skills is what gives the phone line
    # the same reach as Telegram. Conditional so trivial questions stay fast; plain-spoken/read-
    # aloud constraint preserved.
    prompt = (
        "You are Robot's full agent backend answering a request from a live phone call. Use your "
        "FULL capabilities — you have a skill library (email & Gmail via the google-workspace "
        "skill, calendar, files, web, infrastructure, memory, and more). If the request needs a "
        "tool or skill, discover it (run skills_list) and actually execute it; never say you "
        "can't do something without first checking your skills. Reply concisely in plain spoken "
        "English (no markdown, no code blocks, no bullet points) — it will be read aloud on a "
        f"phone call. The user asked: {instruction}"
    )
    try:
        headers = {"Content-Type": "application/json"}
        if HERMES_GATEWAY_TOKEN:
            headers["Authorization"] = f"Bearer {HERMES_GATEWAY_TOKEN}"

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{gateway_url}/v1/chat/completions",
                headers=headers,
                json={
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            # OpenAI-compatible response format
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content:
                return content
            # Fallback: try plain text response
            if isinstance(data, dict) and "text" in data:
                return data["text"]
            return "I got a response but couldn't parse it."
    except httpx.TimeoutException:
        logger.error("Hermes gateway call timed out (120s)")
        return "Sorry, that took too long. Try asking again or simplify the request."
    except httpx.HTTPStatusError as e:
        logger.error(f"Hermes gateway HTTP error: {e.response.status_code} {e.response.text[:300]}")
        return "Sorry, the backend returned an error."
    except Exception as e:
        logger.error(f"Hermes gateway call failed: {e}")
        return "Sorry, something went wrong on my end."


# --- Off-loop tool dispatch (A1) ---
# Backported from Mode V (services/talk-voice-bridge/realtime_bridge.py). The Hermes backend
# turn can take many seconds (up to 120s). Awaiting it inside the OpenAI receive loop stalls the
# single WS consumer: the inbound frame queue fills, websockets applies backpressure and stops
# reading — which also stalls pong handling, so OpenAI's keepalive ping times out and drops the
# call (the `sent 1011 keepalive ping timeout` failure Mode V's README records). So the round-trip
# runs OFF the loop as a tracked task; tool_lock serializes the terminal `response.create` because
# OpenAI allows only one active response at a time.


async def _create_response(openai_ws, response_idle, response_payload=None) -> None:
    """Send a response.create, first waiting until no response is active (OpenAI allows only one at
    a time). Claims the slot immediately (clear) so a follow-up create can't race the receive loop's
    response.created. A missed response.done can't wedge us — the wait is time-boxed."""
    if response_idle is not None:
        try:
            await asyncio.wait_for(response_idle.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("response_idle wait timed out — creating response anyway")
        response_idle.clear()
    msg = {"type": "response.create"}
    if response_payload is not None:
        msg["response"] = response_payload
    await openai_ws.send(json.dumps(msg))


async def _run_tool_call(openai_ws, tool_lock: asyncio.Lock, call_id: str, instruction: str,
                         recorder=None, response_idle=None,
                         filler_debounce: float = FILLER_DEBOUNCE_S,
                         filler_text: str = FILLER_TEXT) -> None:
    """Run one hermes_agent round-trip and feed the result back, serialized on tool_lock.

    Idea 1: if the backend hasn't returned within filler_debounce, speak a short filler so Robot
    isn't mute for the seconds a slow lookup takes. The filler and the result each go through
    _create_response so the two response.create calls can't collide (one active response at a time).
    """
    async with tool_lock:
        started = time.monotonic()
        res_task = asyncio.create_task(
            call_hermes_agent(instruction, profile=_current_hermes_profile()))
        filler_fired = False
        try:
            done, _ = await asyncio.wait({res_task}, timeout=filler_debounce)
            if res_task not in done:
                await _create_response(openai_ws, response_idle, {
                    "instructions": f"Say exactly, warmly and briefly: '{filler_text}'"})
                filler_fired = True
            result = await res_task
        except asyncio.CancelledError:
            res_task.cancel()
            raise
        duration_ms = (time.monotonic() - started) * 1000.0
        logger.info(f"Hermes result ({duration_ms / 1000:.1f}s, filler={filler_fired}): {result[:300]}")
        if recorder is not None:
            recorder.on_tool_call("hermes_agent", duration_ms, ok=True)
        await openai_ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": result,
            },
        }))
        await _create_response(openai_ws, response_idle)


async def _dispatch_tool_call(openai_ws, tool_lock: asyncio.Lock, call_id: str,
                              instruction: str, recorder=None, response_idle=None) -> None:
    """Error-isolated wrapper run as its own task (mirrors Mode V's _handle_tool_guarded).

    A stray tool error is logged and the call continues; cancellation during teardown propagates.
    """
    try:
        await _run_tool_call(openai_ws, tool_lock, call_id, instruction, recorder, response_idle)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("Tool dispatch failed — continuing call")


async def _deny_tool_call(openai_ws, tool_call_id: str, response_idle=None) -> None:
    """s8b c2: fail-closed refusal for a tool call on a tools-disabled session.

    tools=[]/tool_choice="none" should make a function_call structurally impossible on
    such a session; if one arrives anyway we feed a denial as the function result (so the
    model doesn't hang waiting on it) and let it speak a brief 'can't do that' — the
    backend is NEVER reached. Defense in depth on top of the session-level tool cut.
    """
    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {"type": "function_call_output", "call_id": tool_call_id,
                 "output": "That capability is not available on this call."},
    }))
    await _create_response(openai_ws, response_idle)


def _handle_function_call(openai_ws, tool_call_id: str, fn_name: str, args_str: str, *,
                          tools_enabled: bool, tool_lock: asyncio.Lock,
                          recorder=None, response_idle=None) -> "asyncio.Task | None":
    """Route one completed ``response.function_call_arguments.done`` event.

    FAIL-CLOSED (s8b c2): when this session's tools are cut (``tools_enabled`` False — an
    outbound call without ``guardrails.on_call_tools``), the call is DENIED and never
    dispatched, regardless of the function name. Otherwise a ``hermes_agent`` call is
    dispatched OFF the receive loop as a tracked task (A1). Returns the created task (a
    deny task or a dispatch task) so teardown can cancel it, or None for an unknown tool
    on an enabled session.
    """
    if not tools_enabled:
        logger.warning("Tool call %s on a tools-disabled session — denying (fail-closed)",
                       fn_name)
        return asyncio.create_task(
            _deny_tool_call(openai_ws, tool_call_id, response_idle),
            name="mode-c-tool-deny")
    if fn_name != "hermes_agent":
        return None
    try:
        instruction = json.loads(args_str).get("instruction", "")
    except json.JSONDecodeError:
        instruction = args_str
    return asyncio.create_task(
        _dispatch_tool_call(openai_ws, tool_lock, tool_call_id, instruction,
                            recorder, response_idle),
        name="mode-c-tool")


async def _handle_barge_in(twilio_ws, openai_ws, stream_sid: str, item_id: str,
                           played_ms: float) -> None:
    """A2: the caller talked over Robot. Flush Twilio's already-buffered outbound audio (Twilio
    keeps playing what we've sent it long after the model stops) and truncate the model's context
    to what was actually played, so Robot stops mid-word and its transcript stays honest."""
    await twilio_ws.send_json({"event": "clear", "streamSid": stream_sid})
    await openai_ws.send(json.dumps({
        "type": "conversation.item.truncate",
        "item_id": item_id,
        "content_index": 0,
        "audio_end_ms": int(played_ms),
    }))


# --- Outbound mission store ---

# Pending outbound missions, keyed by an unguessable call_id we mint and hand ONLY to Twilio (via
# the <Parameter> in the outbound TwiML). media_stream pops it on the Twilio `start` event to arm
# the sandboxed session. Entries are pruned on insert so an unanswered call can't leak forever.
#
# Ticket 09: a one-shot fire binds its Agent snapshot to the SAME call_id, not to
# a process-global "currently active agent". Two in-flight missions can therefore
# carry two different Agents; taking one cannot change the other.
_MISSIONS: dict[str, "tuple[float, OutboundMission]"] = {}
_OUTBOUND_SNAPSHOTS: dict[str, object] = {}
_MISSION_TTL_S = 300.0


def _prune_stale_missions(now: "float | None" = None) -> None:
    now = time.monotonic() if now is None else now
    for stale in [k for k, (ts, _) in _MISSIONS.items() if now - ts > _MISSION_TTL_S]:
        _MISSIONS.pop(stale, None)
        _OUTBOUND_SNAPSHOTS.pop(stale, None)


def _remember_mission(call_id: str, mission: OutboundMission, snapshot=None) -> None:
    now = time.monotonic()
    _prune_stale_missions(now)
    _MISSIONS[call_id] = (now, mission)
    if snapshot is not None:
        _OUTBOUND_SNAPSHOTS[call_id] = snapshot
    else:
        _OUTBOUND_SNAPSHOTS.pop(call_id, None)


def _take_mission(call_id: str):
    entry = _MISSIONS.pop(call_id, None)
    return entry[1] if entry else None


def _take_oneshot_snapshot(call_id: str):
    """The Agent bound to this call_id at fire time, or None (legacy pointer path)."""
    return _OUTBOUND_SNAPSHOTS.pop(call_id, None)


def _drop_pending_outbound(call_id: str) -> None:
    """A dial that never happened must not leave a mission or a snapshot behind."""
    _MISSIONS.pop(call_id, None)
    _OUTBOUND_SNAPSHOTS.pop(call_id, None)


# s7 c7: one outbound call at a time. A dial is "in progress" from calls.create until
# the media-stream session tears down (or the unanswered mission expires) — a second
# POST /voice/outbound in that window gets a clear 409, never a queued surprise call.
# Ticket 09: the lock applies ONLY to the legacy (no-agent) Hermes-skill path.
# A one-shot names its Agent on the request and does not share that cell.
_ACTIVE_OUTBOUND: set = set()


def _outbound_busy() -> bool:
    _prune_stale_missions()
    return bool(_MISSIONS) or bool(_ACTIVE_OUTBOUND)


# Single-use tokens gating the INBOUND leg of /voice/stream. The wss URL is public (behind
# the Cloudflare tunnel), so without a gate anyone could skip Twilio and open a full-tool
# Robot session directly (proven with a probe 2026-07-13). The webhook — which has already
# verified the Twilio signature AND the caller allow-list — mints a token into the TwiML
# <Parameter>, and media_stream refuses to arm a session without it (or an outbound call_id).
_INBOUND_TOKENS: dict[str, float] = {}
# VC24: who the token was minted for. `From` is part of the Twilio-signed payload and has
# already passed the caller list, so it is the one caller identity this service can
# trust; the direct lane tells Hermes who it is talking to.
_INBOUND_CALLERS: dict[str, str] = {}
_INBOUND_TOKEN_TTL_S = 300.0


def _mint_inbound_token(caller: str = "") -> str:
    now = time.monotonic()
    for stale in [k for k, ts in _INBOUND_TOKENS.items() if now - ts > _INBOUND_TOKEN_TTL_S]:
        _INBOUND_TOKENS.pop(stale, None)
        _INBOUND_CALLERS.pop(stale, None)
    token = secrets.token_urlsafe(24)
    _INBOUND_TOKENS[token] = now
    if caller:
        _INBOUND_CALLERS[token] = caller
    return token


def _take_inbound_token(token: str) -> bool:
    ts = _INBOUND_TOKENS.pop(token, None)
    return ts is not None and time.monotonic() - ts <= _INBOUND_TOKEN_TTL_S


# Outbound-only: events carrying a finished `transcript` (callee ASR + our agent's speech). GA and
# beta output-transcript names differ, so both are listened for. Mirrors the Mode V bridge.
_TRANSCRIPT_EVENTS = frozenset({
    "conversation.item.input_audio_transcription.completed",  # callee (input transcription is on)
    "response.output_audio_transcript.done",                  # agent (GA)
    "response.audio_transcript.done",                         # agent (beta fallback)
})

# --- Logging ---

LOG_EVENT_TYPES = [
    "response.content.done",
    "response.done",
    "response.created",
    "response.output_item.added",
    "response.output_item.done",
    "input_audio_buffer.committed",
    "input_audio_buffer.speech_started",
    "input_audio_buffer.speech_stopped",
    "session.created",
    "session.updated",
    "conversation.item.created",
    "error",
]

# --- FastAPI app ---

app = FastAPI(title="Mode C — OpenAI Realtime S2S + Hermes Agent")


@app.post("/voice/webhook")
async def incoming_call(request: Request):
    """Twilio webhook — returns TwiML to start a Media Stream."""
    # Verify Twilio request signature to prevent unauthorized access.
    # Twilio signs the PUBLIC webhook URL (https://<PUBLIC_HOST>/voice/webhook).
    # Behind Cloudflare, str(request.url) is the internal http origin and won't
    # match — so validate against the pinned public URL first, then fall back to
    # request.url for direct/local (unproxied) hits.
    params = dict(await request.form())
    if TWILIO_SIGNING_TOKENS:
        signature = request.headers.get("X-Twilio-Signature", "")
        candidate_urls = []
        if PUBLIC_HOST:
            candidate_urls.append(f"https://{PUBLIC_HOST}{request.url.path}")
        candidate_urls.append(str(request.url))
        if not any(RequestValidator(t).validate(u, params, signature)
                   for t in TWILIO_SIGNING_TOKENS for u in candidate_urls):
            logger.warning(f"Invalid Twilio signature from {request.client.host} "
                           f"(tried {len(TWILIO_SIGNING_TOKENS)} signing token(s) against "
                           f"{candidate_urls})")
            return Response(content="Forbidden", status_code=403)

    # Owner-only inbound: `From` is part of the Twilio-signed payload, so it can't be
    # spoofed past the check above. Strangers get a polite hangup — no Media Stream,
    # no token, no session.
    caller = params.get("From", "")
    if caller not in ALLOWED_CALLERS:
        logger.warning("Rejecting inbound call from %r — not in VOICE_INBOUND_ALLOWED_CALLERS",
                       caller)
        resp = VoiceResponse()
        resp.say("Sorry, this number is private. Goodbye.", voice="Polly.Amy")
        resp.hangup()
        return Response(content=str(resp), media_type="application/xml")

    logger.info("Incoming call received from allowed caller")
    resp = VoiceResponse()
    resp.say("Connecting you to Robot.", voice="Polly.Amy")
    resp.pause(length=1)
    # Dial back the PUBLIC host for the Media Stream (request.url.hostname may be
    # the internal Cloudflare origin, which Twilio can't reach). The single-use token
    # is what lets media_stream trust this connection — see _INBOUND_TOKENS.
    host = PUBLIC_HOST or request.url.hostname
    connect = Connect()
    stream = connect.stream(url=f"wss://{host}/voice/stream")
    stream.parameter(name="inbound_token", value=_mint_inbound_token(caller))
    resp.append(connect)
    return Response(content=str(resp), media_type="application/xml")


@app.post("/voice/outbound")
async def place_outbound_call(request: Request):
    """Place an autonomous OUTBOUND PSTN call: dial a number and run a SANDBOXED mission.

    Trust-inverse of the inbound webhook. Owner-only by construction — requires the gateway bearer
    token, so only the Hermes gateway (which the owner drives) can trigger a call. The on-call model
    itself is tool-less and mission-only (see outbound.build_outbound_prompt + the sandbox branch in
    media_stream), so a callee has no path back to the owner's data regardless of what they say.

    Body: {brief|objective, to|number, agent?, disclose?, report_channel?,
           report_address?, target_display?}
      → {placed, call_sid, call_id, agent?} (200) · 400 bad input · 401 bad token
        · 403 number not allowed (legacy path only) · 409 agent cannot run.

    Ticket 09: when ``agent`` is present this is a one-shot. The named Agent is
    loaded and bound to THIS call_id. The pointer is not read and not written.
    The pre-dial allow-list is not consulted (outbound stays allow-any; the
    guard is the gateway bearer plus the callee-side sandbox). The one-at-a-time
    busy lock is not applied, so two one-shots with different Agents can be in
    flight without sharing any "currently active agent" cell.

    Omitting ``agent`` keeps the pre-09 Hermes-skill path: the pointer (then
    LKG) supplies the profile, the allow-list still grades it, one at a time.
    """
    # Fail-closed (VC24): an unset token refuses every caller. It used to skip the check.
    refusal = hermes_gateway.bearer_problem(
        request.headers.get("authorization", ""), HERMES_GATEWAY_TOKEN)
    if refusal is not None:
        return JSONResponse({"error": refusal[1]}, status_code=refusal[0])

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    brief = (body.get("brief") or body.get("objective") or "").strip()
    to = (body.get("to") or body.get("number") or "").strip()
    if not brief:
        return JSONResponse({"error": "missing brief/objective"}, status_code=400)
    if not to:
        return JSONResponse({"error": "missing to/number"}, status_code=400)

    agent_id = body.get("agent")
    if agent_id is not None and not isinstance(agent_id, str):
        return JSONResponse({"error": "agent must be a string id"}, status_code=400)
    agent_id = (agent_id or "").strip() or None

    if "disclose" in body:
        disclose = bool(body.get("disclose"))
    else:
        disclose = None

    oneshot_snapshot = None
    if agent_id is not None:
        try:
            oneshot_snapshot = profiles.load_named_profile(agent_id, "outbound")
        except profiles.ProfileError as e:
            logger.error("Refusing one-shot outbound — agent '%s' cannot run: %s",
                         agent_id, e)
            return JSONResponse(
                {"error": f"cannot place: agent '{agent_id}' cannot run outbound: {e}"},
                status_code=409)
    else:
        try:
            gate_profile = lkg.resolve("outbound", outlet=profiles.OUTLET_PHONE)
        except profiles.ProfileError as e:
            logger.error("Refusing outbound dial — active voice profile failed to load: %s", e)
            return JSONResponse({"error": f"active voice profile failed to load: {e}"},
                                status_code=500)
        profile_allow = (gate_profile.outbound_allow_list()
                         if gate_profile is not None else None)
        if profile_allow is not None:
            if to not in profile_allow:
                logger.warning("Refusing outbound call to %s — not in the profile's "
                               "number_policy.allow list", to)
                return JSONResponse({"error": f"number {to} is not in the outbound allow-list"},
                                    status_code=403)
        elif ALLOWED_OUTBOUND and to not in ALLOWED_OUTBOUND:
            logger.warning("Refusing outbound call to %s — not in allow-list", to)
            return JSONResponse({"error": f"number {to} is not in the outbound allow-list"},
                                status_code=403)
        if _outbound_busy():
            return JSONResponse({"error": "an outbound call is already in progress — "
                                          "wait for it to finish (one at a time)"},
                                status_code=409)

    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
        return JSONResponse({"error": "Twilio credentials not configured"}, status_code=500)
    if not PUBLIC_HOST:
        return JSONResponse({"error": "VOICE_PUBLIC_HOST not set"}, status_code=500)

    call_id = secrets.token_urlsafe(24)
    _remember_mission(call_id, OutboundMission(
        brief=brief,
        report_channel=body.get("report_channel", "talk"),
        report_address=body.get("report_address", ""),
        target_display=body.get("target_display", ""),
        to=to,
        disclose=disclose,
    ), snapshot=oneshot_snapshot)

    kwargs = outbound_request.build_outbound_request(
        to=to, from_number=TWILIO_FROM_NUMBER, public_host=PUBLIC_HOST, call_id=call_id)

    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        call = await asyncio.to_thread(client.calls.create, **kwargs)
    except Exception as e:  # noqa: BLE001
        _drop_pending_outbound(call_id)
        logger.error(f"Twilio calls.create failed: {e}")
        return JSONResponse({"error": f"Twilio calls.create failed: {e}"}, status_code=502)

    logger.info("Placed outbound call to %s (sid=%s, agent=%s)", to, call.sid,
                agent_id or "(pointer)")
    payload = {"placed": True, "call_sid": call.sid, "call_id": call_id}
    if agent_id is not None:
        payload["agent"] = agent_id
    return JSONResponse(payload, status_code=200)


@app.websocket("/voice/stream")
async def media_stream(websocket: WebSocket):
    """Relay audio between a Twilio Media Stream and OpenAI Realtime.

    Serves BOTH call directions off the same stream:
      - INBOUND  (no call_id parameter): the on-call model IS Robot — full SOUL prompt + the
        hermes_agent tool (unchanged behaviour).
      - OUTBOUND (call_id parameter, minted by /voice/outbound): a SANDBOXED session — tools=[],
        mission-only prompt, caller ASR captured for the code-driven transcript report-back.
    The direction is only known once the Twilio `start` event arrives (it carries
    customParameters), so the OpenAI session is configured there rather than at WS-accept time.
    """
    await websocket.accept()
    logger.info("Twilio WebSocket connected — waiting for the stream `start` event")

    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY not set")
        await websocket.close()
        return

    # --- Phase 1: direction discovery. `start` carries customParameters (outbound
    # call_id / webhook-minted inbound token) and s3 profiles are per-direction, so the
    # OpenAI dial — whose URL carries the effective model — happens only after it.
    # `connected` and any pre-start `media` are dropped (nothing is armed yet).
    start_data = None
    try:
        async for message in websocket.iter_text():
            data = json.loads(message)
            event = data.get("event")
            if event == "start":
                start_data = data["start"]
                break
            if event == "stop":
                break
    except WebSocketDisconnect:
        logger.info("Twilio WebSocket disconnected before start")
    if start_data is None:
        return

    stream_sid = start_data["streamSid"]
    # customParameters carries either the outbound call_id or the webhook-minted
    # inbound_token. The wss URL is public, so a connection proving NEITHER is refused
    # before any session config or audio forwarding (see _INBOUND_TOKENS).
    params = start_data.get("customParameters") or {}
    call_id = params.get("call_id")
    inbound_token = params.get("inbound_token")
    oneshot_snapshot = _take_oneshot_snapshot(call_id) if call_id else None
    mission = _take_mission(call_id) if call_id else None
    caller = _INBOUND_CALLERS.pop(inbound_token, "") if inbound_token else ""
    if mission is not None:
        _ACTIVE_OUTBOUND.add(call_id)     # busy until THIS session tears down (s7 c7)
        # s8b c1: the outbound base prompt (persona-driven vs containment sandbox) is
        # chosen once the profile snapshot is loaded below — deferred here.
        prompt = None
        logger.info("Outbound call armed → %s", mission.to)
    elif inbound_token and _take_inbound_token(inbound_token):
        prompt = build_system_prompt()
        logger.info("Inbound call — full Robot session (token verified)")
    else:
        logger.warning("Refusing to arm stream %s — no valid call_id/"
                       "inbound_token (direct WS access?)", stream_sid)
        # Legacy observable refusal shape (the socket used to be opened pre-validation):
        # open-and-close, no session config, no audio. Env model only — refused streams
        # never touch profile files.
        async with websockets.connect(
            f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}",
            additional_headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        ) as openai_ws:
            if openai_ws.state.name == "OPEN":
                await openai_ws.close()
        return

    direction = "outbound" if mission is not None else "inbound"
    # s3 rule 3 (TOCTOU): ONE profile load per call setup. The snapshot feeds BOTH the
    # wss URL and the session.update (threaded via the _CALL_PROFILE contextvar because
    # _send_session_update's signature is frozen by the s1 parity suites). A broken
    # selected profile refuses the call loudly — never a silent env fallback (s1 rule).
    # Ticket 09: a one-shot outbound already bound its Agent at /voice/outbound.
    # Using that snapshot here is what makes two in-flight calls with different
    # Agents possible — lkg.resolve would re-read the pointer and both calls
    # would become whoever is assigned right now.
    # s8 (LKG): a broken assignment answers with the last-known-good snapshot instead of
    # refusing, loudly (event-log record + warning + the dashboard keeps showing the
    # slot broken). With no snapshot the loud refusal below is unchanged.
    if oneshot_snapshot is not None:
        snapshot = oneshot_snapshot
    else:
        try:
            snapshot = lkg.resolve(direction, outlet=profiles.OUTLET_PHONE)
        except profiles.ProfileError:
            logger.exception("Refusing %s call setup — active voice profile failed to load "
                             "(no last-known-good snapshot either)", direction)
            if call_id:
                _ACTIVE_OUTBOUND.discard(call_id)
            await websocket.close()
            return

    # --- s7: lane routing. A cascade profile NEVER reaches the realtime bridge below
    # — it runs its own pipeline behind the same call setup (auth, snapshot, recorder,
    # teardown). VC24: an inbound stream gets here only on the direct Hermes lane;
    # activation refuses every other inbound cascade, and the check below is the same
    # rule held a second time at the door.
    if snapshot is not None and snapshot.pipeline == "cascade":
        direct = profiles.is_hermes_direct(snapshot.doc)
        if mission is None and not direct:
            logger.error("Outside-vendor cascade profile on an inbound stream - "
                         "refusing the call")
            await websocket.close()
            return
        lane_problem = await _direct_lane_problem(snapshot) if direct else None
        if lane_problem is None:
            try:
                await _run_cascade_call(
                    websocket, stream_sid, snapshot, mission,
                    oneshot=oneshot_snapshot is not None,
                    direction=direction, caller=caller)
            finally:
                _ACTIVE_OUTBOUND.discard(call_id)
            return
        if mission is not None:
            # An outbound Mission was written for THIS Agent. Handing it to a different
            # being because a gateway is down would be a lie about who was sent.
            logger.error("Refusing outbound call for agent '%s' - %s",
                         snapshot.agent_id, lane_problem)
            _ACTIVE_OUTBOUND.discard(call_id)
            await websocket.close()
            return
        # q6-failure: the phone is ringing and Hermes is not there. Answer on the
        # Realtime lane, loudly, with this bridge's own defaults: the direct Agent's
        # knobs are an ElevenLabs voice and a Hermes route, which mean nothing to OpenAI.
        lkg.announce_lane_fallback(profiles.OUTLET_PHONE, direction,
                                   snapshot.agent_id, lane_problem)
        snapshot = None

    # s8b c1: now that the snapshot is loaded, choose the outbound base prompt —
    # persona-driven (drops containment) when the profile carries a persona, else the
    # untrusted-third-party containment sandbox. Inbound already set its prompt above.
    if mission is not None:
        prompt = _outbound_base_prompt(snapshot, mission)
    # s8b c2: FAIL-CLOSED tool gate for this session. Inbound always has tools; an
    # outbound session gets them ONLY when its profile sets guardrails.on_call_tools
    # (matching open_tools in _send_session_update). The receive loop denies any tool
    # call that arrives while this is False — defense in depth on the tools=[] cut.
    tools_enabled = mission is None or (snapshot is not None and snapshot.on_call_tools)

    transcript: list[str] = []  # outbound only — captured by code, delivered on teardown
    # A1: off-loop tool dispatch state. tool_lock serializes each tool's response.create; tool_tasks
    # tracks in-flight round-trips so teardown can cancel a hung backend call.
    tool_lock = asyncio.Lock()
    tool_tasks: set[asyncio.Task] = set()
    # A2: barge-in tracking. current_item_id = the assistant audio item currently playing (from the
    # audio-delta events); played_ms = ms of it already relayed to Twilio (μ-law @ 8k → 8 B/ms).
    current_item_id: "str | None" = None
    played_ms: float = 0.0
    # Idea 2: structured event log (direction is known — phase 1 resolved it).
    recorder = eventlog.CallRecorder(
        call_id=stream_sid or "", mode="twilio", pipeline="realtime", direction=direction,
        outlet=profiles.OUTLET_PHONE,
        target=mission.to if mission is not None else "")
    # Ticket 07: capture both legs of this call. The audio is ALREADY passing through
    # this process, so capture is a tee, not a new hop. ``recording.start`` never raises
    # and never returns None — a disabled feature, a missing encoder or an unwritable
    # volume all yield a no-op object — so the relay loops below have exactly ONE shape
    # whether or not a recording is possible.
    call_audio = recording.start(
        call_id=stream_sid or "", outlet=profiles.OUTLET_PHONE, direction=direction,
        fmt=cascade_config.MULAW_8K)
    # Idea 1: gate so the filler and result response.create can't overlap (OpenAI allows one active
    # response at a time). Set = idle; the receive loop clears on response.created, sets on
    # response.done. Starts idle.
    response_idle = asyncio.Event()
    response_idle.set()

    openai_url = _realtime_url(profile=snapshot)
    # s5: how this call ENDED, recorded rather than assumed. "ok" until something tears the
    # session down; the except below is the only thing that changes it.
    outcome = "ok"
    call_err = None
    cv_token = _CALL_PROFILE.set(snapshot)
    # s8 (LKG): true only once the session actually answered (update + greeting on the
    # wire) - a connection failure must never record a snapshot as known-good.
    session_established = False
    try:
        # GA Realtime API: no "OpenAI-Beta: realtime=v1" header. That beta shape
        # was retired 2026-05-12 and now hard-errors with `beta_api_shape_disabled`.
        async with websockets.connect(
            openai_url,
            additional_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
            },
        ) as openai_ws:
            await _send_session_update(openai_ws, prompt, outbound=mission is not None)
            await _send_initial_greeting(openai_ws, outbound=mission is not None)
            session_established = True
            logger.info(f"Twilio stream started: {stream_sid}")

            async def receive_from_twilio():
                """Forward Twilio audio to OpenAI; `stop` tears the session down."""
                try:
                    async for message in websocket.iter_text():
                        data = json.loads(message)
                        event = data.get("event")
                        if event == "media":
                            if openai_ws.state.name == "OPEN":
                                await openai_ws.send(
                                    json.dumps({
                                        "type": "input_audio_buffer.append",
                                        "audio": data["media"]["payload"],
                                    })
                                )
                            # Ticket 07: tee the caller's leg AFTER the forward, so
                            # capture can never sit between the caller and the model.
                            call_audio.caller_audio(data["media"]["payload"])
                        elif event == "stop":
                            logger.info("Twilio stream stopped")
                            break
                except WebSocketDisconnect:
                    logger.info("Twilio WebSocket disconnected")
                finally:
                    # ANY Twilio-side exit (clean `stop`, disconnect, error) must close the
                    # OpenAI socket, else send_to_twilio keeps iterating it until OpenAI's
                    # idle timeout — stalling teardown and the outbound transcript delivery
                    # (observed live 2026-07-13: transcript stuck >8 min after hangup).
                    if openai_ws.state.name == "OPEN":
                        await openai_ws.close()

            async def send_to_twilio():
                """Forward OpenAI audio to Twilio, handle tool calls, capture transcript."""
                nonlocal current_item_id, played_ms
                try:
                    async for openai_message in openai_ws:
                        response = json.loads(openai_message)
                        event_type = response.get("type", "")

                        if event_type in LOG_EVENT_TYPES:
                            logger.info(f"OpenAI: {event_type}")
                        elif "audio" not in event_type:
                            logger.debug(f"OpenAI: {event_type}")

                        if event_type == "error":
                            logger.error(f"OpenAI error: {response.get('error', {})}")

                        # --- A2 barge-in: caller started talking over Robot ---
                        if (event_type == "input_audio_buffer.speech_started"
                                and current_item_id is not None):
                            try:
                                await _handle_barge_in(websocket, openai_ws, stream_sid,
                                                       current_item_id, played_ms)
                            except Exception as e:
                                logger.error(f"Barge-in flush/truncate failed: {e}")
                            # Ticket 07: Twilio was told to drop what it had buffered,
                            # so the recording must drop the same tail — otherwise the
                            # agent talks over the caller for audio nobody heard.
                            call_audio.agent_truncate(played_ms)
                            current_item_id = None
                            played_ms = 0.0
                            response_idle.set()  # truncated response is over — release the gate

                        # Idea 2: TTFB clock starts when the caller stops speaking.
                        if event_type == "input_audio_buffer.speech_stopped" and recorder:
                            recorder.on_speech_stopped(time.monotonic())

                        # Idea 1: track whether a response is in flight (filler/result gate).
                        if event_type == "response.created":
                            response_idle.clear()

                        # A turn finished (or a barge-in cleared it): nothing left to truncate.
                        if event_type == "response.done":
                            current_item_id = None
                            played_ms = 0.0
                            response_idle.set()
                            if recorder:
                                recorder.on_response_done(
                                    usage=(response.get("response") or {}).get("usage"),
                                    ts=time.time())

                        # --- Transcript capture (BOTH directions now — Idea 4). Outbound also
                        # gets the code-driven report-back below; inbound feeds Hindsight memory.
                        if event_type in _TRANSCRIPT_EVENTS:
                            text = (response.get("transcript") or "").strip()
                            if text:
                                who = "Them" if "input_audio_transcription" in event_type else "AI"
                                transcript.append(f"{who}: {text}")

                        # --- Tool call handling ---
                        # Inbound always has tools; an outbound session has them only when
                        # its profile set guardrails.on_call_tools (s8b c2). A distinct
                        # variable name — the outer `call_id` is THIS call's outbound id
                        # (used by teardown to clear _ACTIVE_OUTBOUND); the tool's call_id
                        # must not clobber it, or the busy flag never releases.
                        if event_type == "response.function_call_arguments.done":
                            tool_call_id = response.get("call_id", "")
                            fn_name = response.get("name", "")
                            args_str = response.get("arguments", "{}")
                            logger.info(f"Tool call: {fn_name}({args_str[:200]})")
                            # FAIL-CLOSED gate + off-loop dispatch live in _handle_function_call.
                            task = _handle_function_call(
                                openai_ws, tool_call_id, fn_name, args_str,
                                tools_enabled=tools_enabled, tool_lock=tool_lock,
                                recorder=recorder, response_idle=response_idle)
                            if task is not None:
                                tool_tasks.add(task)
                                task.add_done_callback(tool_tasks.discard)
                            continue

                        # --- Audio relay ---
                        if (
                            event_type in ("response.audio.delta", "response.output_audio.delta")
                            and response.get("delta")
                        ):
                            # A2: track which assistant item is playing + how much we've sent Twilio,
                            # so a barge-in can truncate the model's context to what was heard.
                            item_id = response.get("item_id")
                            if item_id and item_id != current_item_id:
                                current_item_id = item_id
                                played_ms = 0.0
                            try:
                                played_ms += len(base64.b64decode(response["delta"])) / 8.0
                            except Exception:
                                pass
                            if recorder:
                                recorder.on_audio_delta(time.monotonic())  # first-after-speech = TTFB
                            try:
                                await websocket.send_json({
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": response["delta"]},
                                })
                            except Exception as e:
                                logger.error(f"Error relaying audio: {e}")
                            else:
                                # Ticket 07: record only what we actually relayed, and
                                # only AFTER relaying it.
                                call_audio.agent_audio(response["delta"],
                                                       item_id=current_item_id)

                except Exception as e:
                    logger.error(f"Error in OpenAI receive loop: {e}")

            await asyncio.gather(receive_from_twilio(), send_to_twilio())

    except Exception as e:
        # s5: the call record and the archive both state how the call ENDED, so this
        # branch has to be recorded. It used to fall through to a hard-coded outcome="ok",
        # which said every call finished cleanly - including the ones that did not.
        outcome = "error"
        call_err = f"{type(e).__name__}: {e}"
        logger.error(f"Failed to connect to OpenAI Realtime: {e}")
    finally:
        _CALL_PROFILE.reset(cv_token)
        if call_id:
            _ACTIVE_OUTBOUND.discard(call_id)
        # s8 (LKG): this call COMPLETED with the snapshot that served it - record it as
        # the phone outlet's last-known-good for this direction. Only a session that
        # actually answered records; a profile-less call records nothing.
        # Ticket 09: a one-shot Agent is a per-call binding, not a configuration of
        # the Outlet. Recording it would make a later broken assignment answer as
        # whoever was last placed from /place, which the owner never assigned.
        if (session_established and snapshot is not None
                and oneshot_snapshot is None):
            lkg.record(profiles.OUTLET_PHONE, direction, snapshot,
                       call_id=stream_sid or "")
        logger.info("Call session ended")
        # A1: cancel any in-flight tool round-trip so a hung backend can't keep the session
        # half-alive after hangup (Mode C analogue of Mode V's _teardown tool-task cancel).
        for t in list(tool_tasks):
            t.cancel()
        if tool_tasks:
            await asyncio.gather(*tool_tasks, return_exceptions=True)
        # Outbound only: deliver the captured transcript to the owner AFTER teardown, in code
        # (never via the on-call model), as inert text. deliver_transcript swallows its own errors.
        if mission is not None:
            await deliver_transcript(mission, transcript)
        # Idea 4: retain BOTH directions into the call archive (fire-and-forget, so this never
        # blocks teardown). Outbound records a third party - the
        # direction/tags metadata keeps it filterable/wipeable later.
        # Ticket 07: close the capture BEFORE the retain, so the Call's metadata can
        # carry a reference to a file that is already on the volume. finish_async never
        # raises, is bounded, and does the writer-thread join OFF this event loop — which
        # serves every other live call.
        rec = await recording.finish_async(call_audio)
        retain_status, doc_id = _maybe_retain(snapshot, recorder, transcript,
                                              mission=mission, outcome=outcome,
                                              recording=rec.ref)
        # Idea 2: emit the per-call summary (guarded so a logging failure can't wedge teardown).
        if recorder is not None:
            try:
                recorder.finish(outcome=outcome, transcript_ref=doc_id,
                                retain_status=retain_status, err=call_err,
                                recording_ref=rec.ref, recording_status=rec.status)
            except Exception:  # noqa: BLE001
                logger.exception("eventlog finish failed")


def _maybe_retain(snapshot, recorder, transcript, *, mission=None,
                  outcome: str = None, recording: str = None) -> tuple:
    """Idea 4 + s8b c3: decide whether this call's transcript is written to the call archive.

    memory.retain overlays the env flag at the CONSUMER: a selected profile's boolean wins
    (the girlfriend-caller example's `memory.retain: false` was a WRITE opt-out — D8;
    ticket 15 deleted that worked example, the rule stands); field absent / no
    profile falls through to RETAIN_ENABLED. A dispatch also requires a NON-EMPTY transcript
    (nothing to store otherwise), so "skipped" distinguishes flag-off from empty only via
    the flag check here — a retain:false profile is skipped even with a full transcript. The
    opt-out stops the post-call WRITE only; it does not touch in-call tool memory READs
    (those go through the gateway). Returns (retain_status, doc_id) for the eventlog.

    s5 (ticket 05): the metadata is built by the shared `call_record` builder, so this lane
    and the two Talk lanes write one shape. Agent and Mission come from what this call
    actually resolved - a no-profile call records no agent and an inbound call records no
    mission, and both render as unknown rather than as a plausible-looking default.
    """
    retain_on = (snapshot.retain_enabled(RETAIN_ENABLED) if snapshot is not None
                 else RETAIN_ENABLED)
    if not (retain_on and recorder is not None and transcript):
        return "skipped", None
    doc_id = f"voice-twilio-{recorder.call_id}"
    status = call_record.retain_call(
        url=HINDSIGHT_URL, bank=HINDSIGHT_BANK, recorder=recorder,
        transcript=transcript, document_id=doc_id,
        platform="voice_twilio", lane="twilio",
        agent=getattr(snapshot, "agent_id", None),
        mission=(mission.brief if mission is not None else None),
        outcome=outcome,
        # Ticket 07: one additive field. None when the capture produced nothing, so a
        # Call never carries a pointer to audio that is not there.
        recording=recording,
        # Ticket 06: the second additive field, same shape. It runs inside the detached
        # retain, never here.
        summariser=_summariser_for(snapshot))
    return status, (doc_id if status == "dispatched" else None)


async def _direct_lane_problem(snapshot) -> "str | None":
    """Why the direct Hermes lane cannot take THIS call right now, or None.

    The pickup check (q6-failure): the Agent's profile must be routable and its gateway
    must answer inside ``hermes_voice.PROBE_BUDGET_S``. It is the only await between the
    snapshot and the lane, and it is bounded, so it cannot hold a ringing call."""
    name = _profile_of_snapshot(snapshot)
    gateway = gateway_url_for_profile(name)
    if gateway is None:
        return (f"its hermes_profile '{name}' is not routable (not in "
                "HERMES_PROFILE_GATEWAY_URLS, not ok in gateways.json)")
    if not await hermes_voice.probe(gateway):
        return (f"the '{name}' gateway at {gateway} did not answer the pickup check "
                f"within {hermes_voice.PROBE_BUDGET_S:.0f}s")
    return None


async def _run_cascade_call(websocket, stream_sid: str, snapshot, mission,
                            *, oneshot: bool = False, direction: str = "outbound",
                            caller: str = "") -> None:
    """One live cascade call (s7): config from the SHARED builder, streaming STT
    (ElevenLabs Scribe or Deepgram), streaming TTS — teardown + report-back mirror the
    realtime lane's obligations (one call record, one retain, transcript delivered).
    VC24: ``direction`` is the call's real direction, so the event log, the recording,
    the call record and the last-known-good snapshot all say inbound for an inbound call.
    An inbound call has no Mission."""
    env = dict(os.environ)
    try:
        config = cascade_config.build_cascade_config(
            snapshot.doc, snapshot.registry, env,
            base_prompt=cascade_live.base_prompt_for(snapshot.doc))
    except cascade_config.CascadeConfigError:
        logger.exception("Cascade config build failed — refusing the call")
        await websocket.close()
        return
    if not config["stt"]["wired_live"]:
        logger.error("Cascade STT provider '%s' has no LIVE streaming client — "
                     "refusing the call (no silent remap)", config["stt"]["provider"])
        await websocket.close()
        return
    if not config["tts"].get("wired_live"):
        logger.error("Cascade TTS provider '%s' has no LIVE streaming client — "
                     "refusing the call (no silent remap)", config["tts"]["provider"])
        await websocket.close()
        return
    stt = cascade_live.open_stt(config, env, cascade_config.MULAW_8K)
    # VAD silence knob read DIRECTLY from the cascade doc — ActiveProfile.resolve()
    # walks the realtime registry (providers.realtime), which a cascade profile lacks.
    _vad = (snapshot.doc.get("knobs") or {}).get("vad") or {}
    detector = turn_detect.TurnDetector(
        silence_ms=_vad.get("silence_ms") or turn_detect.DEFAULT_SILENCE_MS)
    recorder = eventlog.CallRecorder(call_id=stream_sid or "", mode="twilio",
                                     pipeline="cascade", direction=direction,
                                     outlet=profiles.OUTLET_PHONE,
                                     target=mission.to if mission is not None else "")
    # Ticket 07: the cascade lane records exactly like the realtime lane — same module,
    # same volume, same μ-law/8k phone format. session.teardown() closes it.
    call_audio = recording.start(
        call_id=stream_sid or "", outlet=profiles.OUTLET_PHONE, direction=direction,
        fmt=cascade_config.MULAW_8K)
    try:
        session = _build_cascade_session(
            websocket, stream_sid, config, snapshot, recorder, env, mission,
            stt, detector, call_audio, direction=direction, caller=caller)
    except Exception:  # noqa: BLE001
        # The engine never got the recording, so nothing else will close it.
        await recording.finish_async(call_audio)
        logger.exception("Cascade session construction failed — refusing the call")
        await websocket.close()
        return
    outcome = "ok"
    try:
        logger.info("Cascade call armed (agent=%s, %s, llm=%s, tools=%s) → %s",
                    snapshot.agent_id, direction, (config.get("llm") or {}).get("provider"),
                    session._tools_enabled,
                    mission.to if mission is not None else "(inbound caller)")
        await session.run()
    except WebSocketDisconnect:
        logger.info("Twilio WebSocket disconnected (cascade)")
    except Exception:  # noqa: BLE001
        outcome = "error"
        logger.exception("Cascade call failed")
    finally:
        await session.teardown(outcome=outcome)
        if mission is not None:
            await deliver_transcript(mission, session.transcript)
        # s8 (LKG): a cascade call that ran to a clean end records its snapshot as the
        # phone outlet's outbound last-known-good (a profile that crashed mid-call is
        # not recorded). A one-shot is not a configuration of the Outlet - same
        # rule as the realtime teardown above.
        # Ticket 21: a call whose STT was lost for good was not heard to its end, so it
        # is not proof that this configuration works.
        if outcome == "ok" and not oneshot and not session.stt_lost:
            lkg.record(profiles.OUTLET_PHONE, direction, snapshot,
                       call_id=stream_sid or "")


def _build_cascade_session(websocket, stream_sid, config, snapshot, recorder, env,
                           mission, stt, detector, call_audio,
                           *, direction: str = "outbound", caller: str = ""):
    brief = mission.brief if mission is not None else ""
    return cascade_live.CascadeLiveSession(
        twilio_ws=websocket, stream_sid=stream_sid, config=config, profile=snapshot,
        recorder=recorder, env=env, mission_brief=brief,
        recording=call_audio, direction=direction,
        # VC24: None unless the llm stage is the Agent's own Hermes profile. Raises for
        # an unroutable profile, which the caller turns into a refused call.
        hermes_conversation=cascade_live.hermes_conversation_for(
            config, call_id=stream_sid or "", token=HERMES_GATEWAY_TOKEN,
            mission_brief=brief, caller=caller),
        stt=stt,
        hermes_call=functools.partial(
            call_hermes_agent, profile=_profile_of_snapshot(snapshot)),
        summariser=_summariser_for(snapshot),
        tools_enabled=snapshot.on_call_tools, detector=detector,
        filler_text=FILLER_TEXT, filler_debounce_s=max(FILLER_DEBOUNCE_S, 2.0),
        retain_default=RETAIN_ENABLED, hindsight_url=HINDSIGHT_URL,
        hindsight_bank=HINDSIGHT_BANK)


@app.get("/health")
async def health():
    return {"status": "ok"}


async def _send_initial_greeting(openai_ws, *, outbound: bool = False):
    """Inject an opener so Robot speaks first without waiting for user input.

    Inbound: greet the caller. Outbound: WE dialled THEM, so open the mission the moment they pick
    up (the mission brief in the instructions already says who we are and why we're calling).
    """
    text = (
        "(The call just connected — the other party has answered. Open the conversation now: "
        "greet them briefly and get to the point of your mission.)"
        if outbound else
        "(The phone call just connected. Greet the caller warmly and briefly.)"
    )
    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": text,
                }
            ],
        },
    }))
    await openai_ws.send(json.dumps({"type": "response.create"}))
    logger.info("Sent initial greeting trigger")


async def _send_session_update(openai_ws, system_prompt: str, *, outbound: bool = False):
    """Configure the OpenAI Realtime session (GA API shape).

    The Realtime *Beta* API (flat session shape + `OpenAI-Beta: realtime=v1`
    header) was retired by OpenAI on 2026-05-12 — it now hard-errors with
    `beta_api_shape_disabled`. GA differences applied here:
      - `session.type: "realtime"` is now required
      - `modalities` -> `output_modalities`
      - audio config is nested under `session.audio.input` / `session.audio.output`
      - audio formats are objects, not strings: `{"type": "audio/pcmu"}`
        (`audio/pcmu` == G.711 mu-law, the codec Twilio Media Streams use)
      - `voice` moves under `session.audio.output.voice`
      - `input_audio_transcription` -> `session.audio.input.transcription`
      - `turn_detection` -> `session.audio.input.turn_detection`
    The model is selected via the `?model=` URL query param (see media_stream).

    OUTBOUND sandbox: the ONLY divergence from inbound is `tools`/`tool_choice`. A mission call is
    sent `tools: []` / `tool_choice: "none"` so the on-call model has NO channel to reach Hermes or
    the owner's data — the core guardrail, enforced in code not prompt. Input transcription is on
    for both directions already, so the outbound report-back can capture the callee's ASR verbatim.
    """
    # s1 voice profiles: every knob below honors profile > env > registry default_knobs
    # > coded default; with no profile selected eff is exactly the module constants +
    # the coded VAD numbers, so the payload stays byte-identical to pre-profile code.
    # Persona (pinned rule 4): instructions = base + "\n\n" + persona, else base alone.
    # s3: the profile snapshot comes from the per-call contextvar when media_stream is
    # driving (one load per call setup), else it is resolved once right here.
    direction = "outbound" if outbound else "inbound"
    profile = _resolve_profile(direction=direction)
    eff = _effective_realtime_config(profile, direction)
    # s3 guardrails.on_call_tools — boolean, FAIL-CLOSED, inbound-inert: only a selected
    # profile carrying a literal true restores the INBOUND tool set on an outbound
    # session (the same TOOLS object and tool_choice the inbound path emits); false,
    # absent and no-profile keep the sandbox tools: [] / "none" cut byte-identical.
    # The inbound branch never reads the field.
    open_tools = outbound and profile is not None and profile.on_call_tools
    instructions = (f"{system_prompt}\n\n{eff['persona']}" if eff["persona"]
                    else system_prompt)
    # s11a c2 (L1): tell the model it holds hermes_agent EXACTLY when outbound tools open.
    if open_tools:
        instructions = f"{instructions}\n\n{CAPABILITY_STANZA}"
    session_update = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": instructions,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "transcription": {"model": eff["transcription_model"]},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": eff["vad_threshold"],
                        "prefix_padding_ms": eff["vad_prefix_padding_ms"],
                        "silence_duration_ms": eff["vad_silence_ms"],
                    },
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": eff["voice"],
                },
            },
            "tools": ([] if outbound and not open_tools else TOOLS),
            "tool_choice": ("none" if outbound and not open_tools else "auto"),
        },
    }
    logger.info(
        "Sending session update to OpenAI Realtime (GA API, %s)",
        "inbound / full tools" if not outbound
        else ("OUTBOUND persona / tools open (on_call_tools)" if open_tools
              else "OUTBOUND sandbox / no tools"))
    await openai_ws.send(json.dumps(session_update))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
