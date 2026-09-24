"""Mission authoring: the Agent writes the Mission (ticket 10).

Two assists, same being, same seam as the in-call ``hermes_agent`` tool and
ticket 06's per-call summary: the Agent's ``hermes_profile`` picks the
gateway. There is no global writer, no second model, and no new provider
key.

A Mission is still just a string that belongs to the Call. This module
produces that string. It never stores it, never dials, never writes
``active.yaml``, and never asks the Agent to speak back.

FAILURE IS TOTAL AND QUIET ON THE FORM
======================================

Every function here returns ``(text_or_None, error_or_None)`` and never
raises into the dashboard. A failure — no gateway, unreachable, timeout,
empty reply, an error body, silence, unreadable audio — is ``(None, why)``.
The caller must leave the typed Mission intact. Returning ``("", ...)``
is deliberately not a success: an empty Mission is not producible.

The Agent is asked to write ONLY the Mission. The HTTP body is listen-only
(``tool_choice: none``, no audio output). Dictation that cannot be heard
is a failure, not a guessed Mission.
"""
import base64
import logging
import os

from voicecore import hermes_gateway
from voicecore import profiles

logger = logging.getLogger("voice.mission")

NO_SPEECH = "NO_SPEECH"
MIN_AUDIO_BYTES = 200
DEFAULT_TIMEOUT_S = 30.0
TIMEOUT_ENV = "VOICE_MISSION_TIMEOUT_S"
INPUT_MAX_CHARS = 4000

# Formats the OpenAI-compatible input_audio part accepts. Anything else is
# sent as webm (what Chrome's MediaRecorder produces) and the gateway may
# refuse it — that refusal is a failure, not a retry against another model.
_AUDIO_FORMATS = {
    "audio/webm": "webm",
    "audio/webm;codecs=opus": "webm",
    "audio/ogg": "ogg",
    "audio/ogg;codecs=opus": "ogg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "mp4",
    "audio/m4a": "m4a",
}


EXPAND_PROMPT = (
    "You are writing the Mission for one outbound phone call you yourself "
    "will place. A Mission is the instruction for this Call: who to reach "
    "and what to accomplish. It belongs to the Call, not to you.\n"
    "The operator typed a short prompt. Turn it into a full Mission in your "
    "own voice, as you would want to receive it before the phone rings.\n"
    "Write ONLY the Mission. No greeting, no questions, no preamble, no "
    "title, no markdown, no quotation marks, no bullet points.\n"
    "This is authoring, not a conversation. Do not use tools. Do not place "
    "a call. Do not speak.\n\nPROMPT\n"
)

DICTATE_FROM_TEXT_PROMPT = (
    "You are writing the Mission for one outbound phone call you yourself "
    "will place. A Mission is the instruction for this Call: who to reach "
    "and what to accomplish. It belongs to the Call, not to you.\n"
    "The operator spoke the following. This is dictation, not a conversation: "
    "they are not on a call with you and you must not reply to them.\n"
    "Write ONLY the Mission in your own voice, as you would want to receive "
    "it before the phone rings. No greeting, no questions, no preamble, no "
    "title, no markdown, no quotation marks, no bullet points.\n"
    "If the words are empty or are not a Mission, reply with exactly "
    f"{NO_SPEECH} and nothing else.\n"
    "Do not use tools. Do not place a call. Do not speak.\n\nSPOKEN\n"
)

DICTATE_FROM_AUDIO_PROMPT = (
    "You are writing the Mission for one outbound phone call you yourself "
    "will place. A Mission is the instruction for this Call: who to reach "
    "and what to accomplish. It belongs to the Call, not to you.\n"
    "Listen to the recording. The operator spoke the Mission. This is "
    "dictation, not a conversation: do not greet, do not ask questions, "
    "do not speak back, do not continue a call that is not happening.\n"
    "Write ONLY the Mission in your own voice, as you would want to receive "
    "it before the phone rings. No preamble, no markdown, no quotation marks.\n"
    f"If the recording is silent or you cannot hear speech, reply with exactly "
    f"{NO_SPEECH} and nothing else.\n"
    "Do not use tools. Do not place a call.\n"
)


def timeout_from_env(env=None) -> float:
    env = os.environ if env is None else env
    try:
        seconds = float(env.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    return seconds if seconds > 0 else DEFAULT_TIMEOUT_S


def _clip(text: str) -> str:
    body = (text or "").strip()
    if len(body) > INPUT_MAX_CHARS:
        body = body[:INPUT_MAX_CHARS]
    return body


def _clean_mission(text) -> "str | None":
    """A written Mission, or None. Empty / NO_SPEECH are not Missions."""
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    if cleaned.upper() == NO_SPEECH:
        return None
    # Strip wrapping quotes a model sometimes adds despite the prompt.
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
        cleaned = cleaned[1:-1].strip()
    return cleaned or None


def resolve_agent_gateway(agent_id: str, *, env=None):
    """``(hermes_profile, gateway_url|None)`` for a named Agent.

    Loads the Agent the same way one-shot place does (``load_named_profile``),
    then picks the gateway the same way the in-call tool does. Raises
    ``profiles.ProfileError`` when the Agent cannot run outbound — the
    dashboard surfaces that as a 409, same as place.
    """
    snapshot = profiles.load_named_profile(agent_id, "outbound", env=env)
    name = hermes_gateway.hermes_profile_of(snapshot.doc)
    return name, hermes_gateway.gateway_url_for_profile(name, env)


def audio_format_for(content_type: str) -> str:
    raw = (content_type or "").strip().lower()
    if raw in _AUDIO_FORMATS:
        return _AUDIO_FORMATS[raw]
    subtype = raw.split(";", 1)[0].strip()
    if subtype in _AUDIO_FORMATS:
        return _AUDIO_FORMATS[subtype]
    if "/" in subtype:
        return subtype.split("/", 1)[1] or "webm"
    return "webm"


async def _ask_for_mission(prompt: str, *, gateway_url: str, token: str,
                           timeout_s: float, ask=None, transport=None,
                           extra_content=None) -> "tuple[str | None, str | None]":
    """One listen-only turn that must produce a Mission. Never raises."""
    try:
        if ask is None:
            reply = await hermes_gateway.ask_chat(
                prompt, gateway_url=gateway_url, token=token,
                timeout_s=timeout_s, extra_content=extra_content,
                transport=transport)
        else:
            reply = await ask(prompt)
    except Exception:  # noqa: BLE001
        logger.warning("mission authoring: the Agent's gateway failed", exc_info=True)
        return None, "the Agent could not write a Mission"
    mission = _clean_mission(reply)
    if mission is None:
        return None, "the Agent did not write a Mission"
    return mission, None


async def expand_mission(prompt: str, *, gateway_url: "str | None",
                         token: str = "", timeout_s: float = DEFAULT_TIMEOUT_S,
                         ask=None, transport=None) -> "tuple[str | None, str | None]":
    """``(mission, None)`` or ``(None, error)``. Never raises."""
    text = _clip(prompt)
    if not text:
        return None, "type a short line for the Agent to turn into a Mission"
    if not gateway_url:
        return None, "this Agent has no Hermes gateway configured"
    return await _ask_for_mission(
        EXPAND_PROMPT + text, gateway_url=gateway_url, token=token,
        timeout_s=timeout_s, ask=ask, transport=transport)


async def dictate_mission(audio: bytes, *, gateway_url: "str | None",
                          token: str = "", timeout_s: float = DEFAULT_TIMEOUT_S,
                          content_type: str = "audio/webm",
                          filename: str = "recording.webm",
                          ask=None, transcribe=None, transport=None
                          ) -> "tuple[str | None, str | None]":
    """Turn a browser recording into a Mission. Never raises.

    Order: transcribe on the Agent's own gateway, then ask that same Agent
    to write the Mission from what it heard. If the gateway has no
    transcriptions endpoint, fall back to one listen-only chat turn that
    carries the audio. Neither path requests spoken audio; neither path
    can reach the outbound dial.
    """
    if not audio or len(audio) < MIN_AUDIO_BYTES:
        return None, "the recording was silent or too short"
    if not gateway_url:
        return None, "this Agent has no Hermes gateway configured"

    try:
        spoken = None
        if transcribe is not None:
            spoken = await transcribe(audio)
        else:
            spoken = await hermes_gateway.post_audio_transcription(
                gateway_url=gateway_url, token=token, audio=audio,
                filename=filename, content_type=content_type,
                timeout_s=timeout_s, transport=transport)

        if isinstance(spoken, str) and spoken.strip():
            return await _ask_for_mission(
                DICTATE_FROM_TEXT_PROMPT + _clip(spoken),
                gateway_url=gateway_url, token=token, timeout_s=timeout_s,
                ask=ask, transport=transport)

        # Gateway has no transcriptions (or it heard nothing). Ask the Agent
        # to listen to the bytes itself. Still listen-only.
        extra = [{
            "type": "input_audio",
            "input_audio": {
                "data": base64.b64encode(audio).decode("ascii"),
                "format": audio_format_for(content_type),
            },
        }]
        mission, err = await _ask_for_mission(
            DICTATE_FROM_AUDIO_PROMPT, gateway_url=gateway_url, token=token,
            timeout_s=timeout_s, ask=ask, transport=transport,
            extra_content=None if ask is not None else extra)
        if mission is None:
            return None, err or "the Agent heard nothing it could turn into a Mission"
        return mission, None
    except Exception:  # noqa: BLE001
        logger.warning("mission dictate: the Agent's gateway failed", exc_info=True)
        return None, "the Agent could not write a Mission from the recording"
