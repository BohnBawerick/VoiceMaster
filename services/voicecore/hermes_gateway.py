"""The Agent's own Hermes gateway (the ticket 06 / in-call hermes_agent seam).

An Agent is a Hermes profile. Its ``hermes_profile`` name picks a gateway base
URL; there is no per-request profile selection on ``/v1/chat/completions``, so
a different being is a different process. This module is the ONE copy of that
resolution and the ONE HTTP client that talks to it for off-call work.
Per-call summary and Mission authoring both go through ``ask_chat``; there
is no second client in ``summary.py``.

Resolution order (VC24): the operator's ``HERMES_PROFILE_GATEWAY_URLS`` map,
then ``default`` -> ``HERMES_GATEWAY_URL``, then the registry the Hermes
supervisor writes at ``$VOICE_CONFIG_DIR/gateways/gateways.json``. Both
bridges and the dashboard call this one function, so a profile the supervisor
started is reachable everywhere with no compose edit and no redeploy.

An unknown profile resolves to no URL, and so does a profile the registry
lists with any status other than ``ok``. The caller must fail honestly - never
silently fall back to another Agent's backend.

This module never dials, never imports the phone path, and never asks a
gateway for spoken audio.
"""
import hmac
import json
import logging
import os
from pathlib import Path

import httpx

from . import profiles

logger = logging.getLogger("voice.hermes_gateway")

DEFAULT_GATEWAY_URL = "http://hermes:18789"
DEFAULT_TIMEOUT_S = 30.0

ENV_GATEWAY_URL = "HERMES_GATEWAY_URL"
ENV_GATEWAY_TOKEN = "HERMES_GATEWAY_TOKEN"
ENV_PROFILE_GATEWAY_URLS = "HERMES_PROFILE_GATEWAY_URLS"

# hermes_profile_registry.REGISTRY_BASENAME (hermes/supervisor/), resolved under
# VOICE_CONFIG_DIR: the writer's directory is mounted into every voice service
# at that service's own config path.
REGISTRY_BASENAME = os.path.join("gateways", "gateways.json")
# The one registry status that means "a gateway is running and answering".
REGISTRY_STATUS_OK = "ok"

# The only paths this client will POST to. Anything else would be a new
# provider or a speak-back / dial path, and is refused here so a refactor
# cannot add one by accident.
CHAT_PATH = "/v1/chat/completions"
TRANSCRIBE_PATH = "/v1/audio/transcriptions"
ALLOWED_PATHS = (CHAT_PATH, TRANSCRIBE_PATH)

# The Agent's tools switch, as Hermes reads it: ``tool_choice`` on a chat request. The
# merged Hermes server enforces it, so VoiceMaster must send it on EVERY request - an
# absent field means the profile's full tool set, and ``tools: []`` enforces nothing.
# ``none`` cuts the tools; ``auto`` leaves the profile's own set intact.
TOOL_CHOICE_NONE = "none"
TOOL_CHOICE_AUTO = "auto"
TOOL_CHOICES = (TOOL_CHOICE_NONE, TOOL_CHOICE_AUTO)


def tool_choice_for(tools_enabled: bool) -> str:
    """The wire value for an Agent's tools setting. Anything but a literal ``True`` cuts
    the tools: a malformed setting can never open them."""
    return TOOL_CHOICE_AUTO if tools_enabled is True else TOOL_CHOICE_NONE


def check_tool_choice(value) -> str:
    """``value`` if it is a tool choice this client may send, else ValueError. Called
    before a request is built, so a malformed internal value is refused rather than
    quietly becoming whatever the upstream default is (which is tools on)."""
    if value not in TOOL_CHOICES:
        raise ValueError(f"tool_choice must be one of {TOOL_CHOICES}, got {value!r}")
    return value


def profile_gateway_map(env=None) -> dict:
    """``name -> base URL`` from ``HERMES_PROFILE_GATEWAY_URLS``.

    Format: ``scout=http://localhost:18790,other=http://host:port``.
    """
    env = os.environ if env is None else env
    out = {}
    for pair in (env.get(ENV_PROFILE_GATEWAY_URLS) or "").split(","):
        name, _, url = pair.strip().partition("=")
        if name.strip() and url.strip():
            out[name.strip()] = url.strip().rstrip("/")
    return out


def registry_path(config_dir=None, env=None) -> Path:
    base = Path(config_dir) if config_dir is not None else profiles.config_dir(env)
    return base / REGISTRY_BASENAME


def read_gateway_registry(config_dir=None, env=None) -> dict:
    """``gateways.json`` as the Hermes side last wrote it, or {} when absent.

    Never raises. Absent is the normal state before that deploy lands and is
    not an error: it means "nothing has told us which gateways are running",
    never "no profiles exist". Read on every call and never cached: the
    supervisor rewrites it each scan, and a cached copy is how a profile
    created a minute ago stays unreachable until a restart.
    """
    try:
        doc = json.loads(registry_path(config_dir, env).read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def registry_entry(registry: dict, name: str) -> dict:
    entries = registry.get("profiles") if isinstance(registry, dict) else None
    if not isinstance(entries, dict):
        return {}
    entry = entries.get(name)
    return entry if isinstance(entry, dict) else {}


def gateway_url_for_profile(profile: str, env=None, *,
                            default_url: "str | None" = None) -> "str | None":
    """Base gateway URL for a hermes_profile name; None = not routable.

    1. ``HERMES_PROFILE_GATEWAY_URLS`` - the operator's explicit word wins.
    2. ``default`` (and a blank name) - ``default_url`` when the caller holds
       one, else ``HERMES_GATEWAY_URL``, else the coded default. The registry
       is not consulted: ``default`` is the container's own top-level gateway,
       which the supervisor does not describe.
    3. The registry. Only a ``status: ok`` entry carrying a URL routes. An
       entry with any other status resolves to None, and so does a name the
       registry has never heard of - neither borrows the default backend.
    """
    env = os.environ if env is None else env
    name = (profile or "").strip() or "default"
    mapping = profile_gateway_map(env)
    if name in mapping:
        return mapping[name]
    if name == "default":
        url = (default_url or env.get(ENV_GATEWAY_URL) or DEFAULT_GATEWAY_URL).strip()
        return url.rstrip("/") if url else None
    entry = registry_entry(read_gateway_registry(env=env), name)
    url = entry.get("gateway_url")
    if entry.get("status") == REGISTRY_STATUS_OK and isinstance(url, str) and url.strip():
        return url.strip().rstrip("/")
    return None


def hermes_profile_of(doc) -> str:
    """The hermes_profile on an Agent document, or ``default`` when omitted."""
    if not isinstance(doc, dict):
        return "default"
    name = doc.get("hermes_profile")
    return name.strip() if isinstance(name, str) and name.strip() else "default"


def gateway_token(env=None) -> str:
    env = os.environ if env is None else env
    return (env.get(ENV_GATEWAY_TOKEN) or "").strip()


def bearer_problem(authorization: str, token: str) -> "tuple[int, str] | None":
    """Why this caller may not use a dialing endpoint, or None when it may.

    FAIL-CLOSED. With no token configured the old check was skipped, so an
    unset variable turned an owner-only endpoint into an open one. Since VC24
    the same bearer also guards a client that lets Hermes act on a call, so
    "nobody configured a token" now means "nobody gets in", the same posture
    the inbound caller list already takes.
    """
    token = (token or "").strip()
    if not token:
        return 503, (f"{ENV_GATEWAY_TOKEN} is not set on this service - refusing "
                     "every caller rather than accepting any")
    if not hmac.compare_digest((authorization or "").encode(),
                               f"Bearer {token}".encode()):
        return 401, "unauthorized"
    return None


def reply_text(data) -> "str | None":
    """The assistant text out of an OpenAI-compatible body, or None.

    Audio output fields are ignored on purpose: this client is listen-and-
    write, never speak-back. A body that only carries audio is "no text".
    """
    if not isinstance(data, dict):
        return None
    try:
        choices = data.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        content = message.get("content") if isinstance(message, dict) else None
    except (AttributeError, IndexError, TypeError):
        content = None
    if not isinstance(content, str) or not content.strip():
        content = data.get("text")
    if isinstance(content, str) and content.strip():
        return content.strip()
    return None


def _headers(token: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def listen_only_chat_body(messages) -> dict:
    """A /v1/chat/completions body that cannot ask the model to speak.

    ``tool_choice: none`` so a profile that would otherwise fire skills
    (including anything that places a call) does not. No ``modalities``
    audio, no ``audio`` output config.
    """
    return {
        "messages": messages,
        "tool_choice": TOOL_CHOICE_NONE,
    }


def assert_listen_only(body: dict) -> None:
    """Raise if a request body could produce spoken audio or fire tools."""
    if not isinstance(body, dict):
        raise ValueError("gateway body must be an object")
    if body.get("tool_choice") != TOOL_CHOICE_NONE:
        raise ValueError("gateway body must set tool_choice=none")
    modalities = body.get("modalities")
    if modalities is not None and "audio" in list(modalities):
        raise ValueError("gateway body must not request audio output")
    if "audio" in body:
        raise ValueError("gateway body must not carry an audio output config")


def _check_path(path: str) -> None:
    if path not in ALLOWED_PATHS:
        raise ValueError(f"refusing to POST {path!r} — not a listen-only Hermes path")


async def post_json(path: str, *, gateway_url: str, token: str, body: dict,
                    timeout_s: float, transport=None) -> "tuple[int | None, object]":
    """POST JSON to an allowed path. Never raises. ``(status, parsed|None)``."""
    _check_path(path)
    if path == CHAT_PATH:
        assert_listen_only(body)
    url = gateway_url.rstrip("/") + path
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout_s) as client:
            resp = await client.post(url, headers=_headers(token), json=body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes gateway %s unreachable (%s): %s",
                       url, type(exc).__name__, str(exc).strip() or type(exc).__name__)
        return None, None
    try:
        parsed = resp.json()
    except ValueError:
        parsed = None
    return resp.status_code, parsed


async def post_audio_transcription(*, gateway_url: str, token: str,
                                   audio: bytes, filename: str, content_type: str,
                                   timeout_s: float, transport=None) -> "str | None":
    """POST one recording to ``/v1/audio/transcriptions``. Never raises.

    Text in, no speech out: this endpoint returns a transcript. A missing
    endpoint, an error, or an empty transcript is None.
    """
    _check_path(TRANSCRIBE_PATH)
    url = gateway_url.rstrip("/") + TRANSCRIBE_PATH
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    files = {"file": (filename or "recording.webm", audio, content_type or "application/octet-stream")}
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout_s) as client:
            resp = await client.post(url, headers=headers, files=files)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes gateway transcription failed (%s): %s",
                       url, str(exc).strip() or type(exc).__name__)
        return None
    text = None
    if isinstance(data, dict):
        text = data.get("text")
    elif isinstance(data, str):
        text = data
    if isinstance(text, str) and text.strip():
        return text.strip()
    return None


async def ask_chat(prompt: str, *, gateway_url: str, token: str,
                   timeout_s: float = DEFAULT_TIMEOUT_S,
                   extra_content=None, transport=None) -> "str | None":
    """One listen-only turn. Never raises. None = the Agent did not write."""
    if extra_content:
        content = [{"type": "text", "text": prompt}] + list(extra_content)
        messages = [{"role": "user", "content": content}]
    else:
        messages = [{"role": "user", "content": prompt}]
    body = listen_only_chat_body(messages)
    status, parsed = await post_json(
        CHAT_PATH, gateway_url=gateway_url, token=token, body=body,
        timeout_s=timeout_s, transport=transport)
    if status != 200:
        return None
    return reply_text(parsed)
