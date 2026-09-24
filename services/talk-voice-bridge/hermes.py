"""Hermes-agent tool + voice system prompt, lifted from Mode C (services/voice/server.py).
Self-contained so the working Twilio path can never regress."""
import logging
import os
from datetime import datetime
from pathlib import Path

import httpx

from voicecore import hermes_gateway

logger = logging.getLogger("mode-v.hermes")

# s11a L1: told-the-model-it-has-the-tool stanza. Appended to the OUTBOUND instructions
# ONLY when the session actually opens tools (guardrails.on_call_tools) — a persona frame
# alone never mentions hermes_agent, so without this the model can hold a tool it was
# never told about. Cut byte-identical when tools are closed (the sandbox says the
# opposite: "you have NO tools").
CAPABILITY_STANZA = (
    "== YOUR CAPABILITIES ==\n"
    "You can act for your operator during this call via the hermes_agent tool — files, "
    "web, infrastructure, messages, code, and memory (the same reach you have on "
    "Telegram). When a request needs it, say a brief holding phrase ('one moment'), call "
    "hermes_agent, then report the result conversationally."
)

def gateway_url_for_profile(profile: str, default_url: str) -> "str | None":
    """Base gateway URL for a hermes_profile name; None = not routable (the caller
    MUST fail honestly — never silently fall back to the default profile's backend, or a
    misconfigured profile's tool calls would land on the wrong agent). The resolution is
    voicecore's, shared with the phone bridge and the dashboard: env map, then
    ``default`` (the call's configured gateway, passed in), then the registry the Hermes
    supervisor writes."""
    return hermes_gateway.gateway_url_for_profile(profile, default_url=default_url)


def hermes_profile_name(profile) -> str:
    """The ``hermes_profile`` of a call profile (an ActiveProfile or None), default
    'default' — for profile-less calls AND profiles that omit the field."""
    doc = getattr(profile, "doc", None)
    if isinstance(doc, dict):
        name = doc.get("hermes_profile")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return "default"

_OWNER_POLICY = (
    "The caller is the OWNER (full trust). Use the hermes_agent tool directly for any "
    "action — files, web, infra, messages, code, servers, memory."
)
_GUEST_POLICY = (
    "The caller is a GUEST (no standing authority). Converse and answer freely, but you MUST NOT "
    "take any ACTION on their behalf (no sending, spending, changing files/systems, scheduling, "
    "running code, controlling devices). When a guest asks for an action: say a brief holding "
    "phrase ('let me check with the owner, one sec'), call the request_owner_approval tool with a "
    "one-line summary, and WAIT. If it returns approved, perform the action with hermes_agent and "
    "tell the guest the result. If denied or it times out, politely tell the guest you couldn't do it."
)


def _read_file(path: Path) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


def build_system_prompt(config_dir: str, *, trust: str = "owner", caller_display: str = "") -> str:
    soul = _read_file(Path(config_dir) / "SOUL.md")
    policy = _OWNER_POLICY if trust == "owner" else _GUEST_POLICY
    who = f" You are speaking with {caller_display}." if caller_display else ""
    return f"""You are on a live phone call. You ARE Robot — the Hermes agent.
You have the same personality, memory, and capabilities as when you talk on Telegram.

== YOUR SOUL ==
{soul}

== CALLER TRUST ({trust.upper()}) =={who}
{policy}

== VOICE CALL RULES ==
- You are speaking out loud, not typing. Be conversational and concise (1-3 sentences).
- Natural speech — contractions, backchanneling. Never output markdown/code/lists — speak plainly.
- When calling a tool, tell the caller briefly: "Let me check that" / "One moment".
- Report tool results conversationally — summarise, don't read raw data.
- Current date/time: {datetime.now().strftime("%A, %d %B %Y, %H:%M %Z")}
"""


TOOLS = [
    {
        "type": "function",
        "name": "hermes_agent",
        "description": (
            "Execute a request through the Hermes agent backend — full access to ALL of Robot's "
            "capabilities (same as Telegram). Use for ANY request beyond pure conversation. "
            "For a GUEST caller, only call this AFTER request_owner_approval returns approved."
        ),
        "parameters": {
            "type": "object",
            "properties": {"instruction": {"type": "string",
                          "description": "Clear natural-language instruction for the Hermes agent."}},
            "required": ["instruction"],
        },
    },
    {
        "type": "function",
        "name": "request_owner_approval",
        "description": (
            "Ask the owner to approve an action a GUEST requested. The owner is messaged on Talk and "
            "their approve/deny reply is returned. Only use for guest callers before taking any action."
        ),
        "parameters": {
            "type": "object",
            "properties": {"summary": {"type": "string",
                          "description": "One-line summary of the action the guest is requesting."}},
            "required": ["summary"],
        },
    },
]

# s11a c3: an OUTBOUND persona call talks to a party WE dialled — the inbound-guest
# `request_owner_approval` flow makes no sense there (there is no owner-approval loop for
# an outbound callee). Outbound therefore exports ONLY hermes_agent. INBOUND keeps the
# full TOOLS set.
OUTBOUND_TOOLS = [t for t in TOOLS if t["name"] == "hermes_agent"]


async def call_hermes_agent(instruction: str, *, gateway_url: str, token: str, timeout: float) -> str:
    # Skill-discovery nudge: a bare one-shot ("The user asked: check my email") makes the
    # backend model answer from its NATIVE tool list and reply "I can't do that" — it never
    # runs `skills_list` to find capabilities delivered as skills (Gmail via the
    # google-workspace skill, etc.). Telling it to discover + actually execute skills is what
    # makes voice reach the same capabilities as Telegram (verified live: with this nudge the
    # api_server path found the google-workspace skill and returned real Gmail). Kept
    # conditional ("if the request needs …") so trivial questions stay fast, and the
    # plain-spoken/read-aloud constraint is preserved (this is a live voice call).
    prompt = ("You are Robot's full agent backend answering a request from a live phone call. Use "
              "your FULL capabilities — you have a skill library (email & Gmail via the "
              "google-workspace skill, calendar, files, web, infrastructure, memory, and more). If "
              "the request needs a tool or skill, discover it (run skills_list) and actually execute "
              "it; never say you can't do something without first checking your skills. Reply "
              "concisely in plain spoken English (no markdown, no code blocks, no bullet points) — it "
              f"will be read aloud on a phone call. The user asked: {instruction}")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{gateway_url}/v1/chat/completions", headers=headers,
                                     json={"messages": [{"role": "user", "content": prompt}]})
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return content or "I got a response but couldn't parse it."
    except httpx.TimeoutException:
        logger.error("Hermes call timed out (%ss)", timeout)
        return "Sorry, that took too long. Try again or simplify the request."
    except httpx.HTTPStatusError as e:
        logger.error("Hermes HTTP %s: %s", e.response.status_code, e.response.text[:300])
        return "Sorry, the backend returned an error."
    except Exception as e:  # noqa: BLE001
        logger.error("Hermes call failed: %s", e)
        return "Sorry, something went wrong on my end."
