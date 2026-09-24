"""The ONE cascade config builder + wiring tables (s4 c1, s7 c1).

Extracted from the bench module so the LIVE cascade lane (mode-c ``cascade_live``)
and the bench/preview (``cascade.run_session`` / ``/api/agents/preview``) build their
pipeline config through literally the same code. Pure: reads ``doc``/``registry``/
``env``, resolves nothing over the network.
"""
import audioop
import logging
import re
from dataclasses import dataclass

import httpx

from . import hermes_gateway
from . import profiles

logger = logging.getLogger("voice.cascade_config")

# Some models (minimax-m3, kimi, deepseek-r1-style, qwen-thinking) emit their chain of
# thought as a <think>…</think> block INSIDE the reply content. A voice lane must never
# speak that inner monologue. Strip it at the reply seam — a no-op for the non-reasoning
# providers (the tag simply never appears), so this is a universal safety guard, not an
# opencodego special-case. (opencodego/minimax-m3 leaks it even with enable_thinking=false
# — live-verified 2026-07-20; a system-prompt instruction is unreliable, stripping is not.)
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text):
    """Remove <think>…</think> reasoning blocks (and a dangling unclosed <think> tail
    from a max_tokens truncation) from a reply. Returns the stripped string, or the
    input unchanged when it is not a str."""
    if not isinstance(text, str):
        return text
    text = _THINK_BLOCK.sub("", text)
    low = text.lower()
    idx = low.rfind("<think>")
    if idx != -1 and "</think>" not in low[idx:]:   # unclosed tail = truncated reasoning
        text = text[:idx]
    return text.strip()

# Bench base prompt: the realtime lane gets its base from the bridge; the bench has no
# bridge, so this stands in. Persona (if any) is appended via compose_instructions.
# The LIVE lane passes its own phone-call base via the ``base_prompt`` kwarg.
CASCADE_BASE_PROMPT = (
    "You are a helpful voice assistant. Answer in one or two short, spoken-style "
    "sentences.")

# Which registry providers actually have a wired cascade client. The editor greys
# everything else per role WITH an explanation; an unwired provider that reaches a
# stage fails honestly as that stage rather than silently remapping (s4 c3 / D1).
# ``stt_live`` is the s7 streaming lane's wiring — the bench keeps batch STT.
STT_ELEVENLABS = "elevenlabs-scribe"
CASCADE_WIRING = {
    "stt": {"openai-gpt-4o-transcribe"},
    "stt_live": {"deepgram", STT_ELEVENLABS},
    "llm": {"nvidia-nemotron", "openrouter", "grok", "gpt-4.1", "gemini-2.5-flash",
            "glm", "opencodego", profiles.HERMES_LLM_PROVIDER},
    "tts": {"elevenlabs", "deepgram-aura"},
    "tts_live": {"elevenlabs", "deepgram-aura"},
}

# provider id -> OpenAI-compatible chat-completions endpoint. Dispatch is on the agent's
# llm.provider, so every wired provider hits its OWN host (no remap). All speak the
# chat-completions dialect (gemini via Google's /openai/ compat surface; endpoints for
# gemini/gpt-4.1 live-verified 2026-07-19, grok/glm shapes per vendor docs — keyless
# here, so they fail honestly at the llm stage until a key lands). opencodego = OpenCode
# Zen Go catalog (s9), OpenAI-compatible, key held — see LLM_EXTRA_HEADERS for its
# Cloudflare-1010 User-Agent quirk.
LLM_ENDPOINTS = {
    "nvidia-nemotron": "https://integrate.api.nvidia.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "grok": "https://api.x.ai/v1/chat/completions",
    "gpt-4.1": "https://api.openai.com/v1/chat/completions",
    "gemini-2.5-flash":
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "glm": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    "opencodego": "https://opencode.ai/zen/go/v1/chat/completions",
}

# Browser-shaped User-Agent. The OpenCode Zen host sits behind Cloudflare bot-protection
# that 403s with "error code: 1010" for non-browser UAs — httpx's default UA and the
# probe's `hermes-voice-control-probe/*` both get blocked; curl / a browser UA pass
# (measured 2026-07-15). This is the ONE shared source for the UA — both cascade lanes
# (bench cascade._stage_llm, live cascade_live._llm_round) and the OPENCODE_GO_API_KEY
# probe read it, so the three call sites can never drift apart.
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# Per-provider extra request HEADERS (merged over the Authorization header at each LLM
# call site). Only opencodego needs one today — its CF-1010 quirk (above).
LLM_EXTRA_HEADERS = {
    "opencodego": {"User-Agent": BROWSER_UA},
}

# Per-provider extra chat-completions body params. gemini-flash-latest is a thinking
# model by default — without capping reasoning it burns the token budget on hidden
# thought (live-verified: max_tokens 10 returned EMPTY content, reasoning_effort:low
# returns clean prose).
# NOTE: opencodego deliberately gets NO response_format here — Zen's response_format is
# decorative (~35% non-compliant, measured 2026-07-15); the cascade lane never asks for
# it, and the registry does not advertise json-mode for opencodego.
LLM_EXTRA_BODY = {
    "gemini-2.5-flash": {"reasoning_effort": "low"},
}

STT_URL = "https://api.openai.com/v1/audio/transcriptions"
TTS_URL_TMPL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}"
AURA_URL = "https://api.deepgram.com/v1/speak"
# The live lane speaks over ElevenLabs' stream-input websocket, for which ElevenLabs
# recommends flash where latency matters (it was eleven_turbo_v2_5 over HTTP).
ELEVENLABS_MODEL = "eleven_flash_v2_5"
DEFAULT_TTS_FORMAT = "mp3_44100_128"
# Per-provider bench format defaults — the ElevenLabs-shaped mp3_44100_128 vocabulary
# must not leak into an Aura config (Aura takes encoding/container query params; its
# bench default is plain mp3, the live lane always asks for mulaw/8000).
DEFAULT_TTS_FORMATS = {
    "elevenlabs": DEFAULT_TTS_FORMAT,
    "deepgram-aura": "mp3",
}
TTS_ENV_VOICE = "ELEVENLABS_VOICE_ID"


# ── Transport audio format (s12) ────────────────────────────────────────────
# The cascade turn engine is transport-agnostic in its LOGIC but every rate/codec
# constant it touches (VAD frame_ms, RMS decode, Deepgram encoding + the audio-sent
# cursor, TTS output_format, the outbound frame size and pacing period, barge-in
# truncation math) derives from ONE of these descriptors — so a
# lane can never mix an 8k frame size with a 24k stream. mode-c (Twilio) is μ-law/8k;
# the Talk lane (s12) is linear16/24k over the Pulse parec/pacat pipes. The leaf
# clients (turn_detect.TurnDetector, deepgram_live.DeepgramLive) stay dependency-light
# and take PRIMITIVES; this descriptor is what the engine + construction sites use to
# derive those primitives, so the format lives in exactly one place.
@dataclass(frozen=True)
class AudioFormat:
    name: str
    sample_rate: int
    deepgram_encoding: str          # Deepgram /listen ``encoding`` — "mulaw" | "linear16"
    bytes_per_sample: int           # μ-law 1 byte/sample; linear16 2
    frame_ms: int = 20              # outbound playout per media frame
    elevenlabs_output_format: str = "ulaw_8000"   # ElevenLabs /stream ``output_format``
    aura_encoding: str = "mulaw"    # Deepgram Aura ``encoding`` (sample_rate follows below)

    @property
    def bytes_per_ms(self) -> float:
        return self.sample_rate * self.bytes_per_sample / 1000.0

    @property
    def frame_bytes(self) -> int:
        return int(self.bytes_per_ms * self.frame_ms)

    @property
    def frame_period_s(self) -> float:
        return self.frame_ms / 1000.0

    def decode_pcm16(self, frame: bytes) -> bytes:
        """Frame → 16-bit linear PCM (for RMS energy). μ-law is
        decoded; linear16 is already PCM and passes through untouched."""
        if not frame:
            return b""
        if self.deepgram_encoding == "mulaw":
            return audioop.ulaw2lin(frame, 2)
        return frame


# mode-c / Twilio Media Streams: G.711 μ-law, 8 kHz, 1 byte/sample. 20ms = 160 B.
MULAW_8K = AudioFormat(
    name="mulaw_8k", sample_rate=8000, deepgram_encoding="mulaw", bytes_per_sample=1,
    elevenlabs_output_format="ulaw_8000", aura_encoding="mulaw")
# Talk / Nextcloud over Pulse pipes: linear16, 24 kHz, 2 bytes/sample. 20ms = 960 B.
# Deepgram takes linear16@24000 natively, ElevenLabs emits and hears pcm_24000, Aura
# linear16 — so this path needs NO resampler.
PCM_24K = AudioFormat(
    name="pcm_24k", sample_rate=24000, deepgram_encoding="linear16", bytes_per_sample=2,
    elevenlabs_output_format="pcm_24000", aura_encoding="linear16")

# The hermes_agent tool in chat-completions ("tools") shape — the ONE canonical
# definition for every cascade LLM call site. The live lane (cascade_live) advertises it
# on a profile opt-in; the s10 eval harness sends it to SCORE whether a model would call
# it (detection only — the harness never executes the tool). Both import it from here so
# the schema can never drift between the lane that runs it and the harness that grades it.
# (The realtime lane's server.TOOLS is the flat Realtime-API shape — same name/contract,
# different envelope — and intentionally stays separate.)
CHAT_TOOLS = [{
    "type": "function",
    "function": {
        "name": "hermes_agent",
        "description": (
            "Execute a request through the Hermes agent backend (files, web, email, "
            "calendar, infrastructure, code, memory - anything beyond conversation). "
            "Pass a clear natural-language instruction."),
        "parameters": {
            "type": "object",
            "properties": {"instruction": {"type": "string"}},
            "required": ["instruction"],
        },
    },
}]


class CascadeConfigError(ValueError):
    """The draft cannot build a cascade pipeline config (missing/unknown stage provider,
    wrong pipeline). Surfaced to the editor as a 422, same class as validate errors."""


def host_of(url: str) -> str:
    return httpx.URL(url).host


def _stage_provider(doc: dict, role: str) -> str:
    providers = doc.get("providers") if isinstance(doc.get("providers"), dict) else {}
    pid = providers.get(role)
    if not isinstance(pid, str) or not pid.strip():
        raise CascadeConfigError(
            f"providers.{role}: required for a cascade pipeline (the {role} stage has "
            "no provider selected)")
    return pid.strip()


def _entry(registry: dict, pid: str, role: str) -> dict:
    entry = registry.get(pid)
    if entry is None:
        raise CascadeConfigError(f"providers.{role}: unknown provider id '{pid}' "
                                 "(not in the registry)")
    if entry.get("role") != role:
        raise CascadeConfigError(
            f"providers.{role}: provider '{pid}' has role '{entry.get('role')}', "
            f"not '{role}'")
    return entry


def _knob(knobs: dict, entry: dict, key: str, default=None):
    """Precedence: profile knob > registry default_knobs > coded default. (Env fill for
    voice is handled by the caller — it is provider-specific.)"""
    if key in knobs and knobs[key] is not None:
        return knobs[key]
    dk = entry.get("default_knobs")
    if isinstance(dk, dict) and dk.get(key) is not None:
        return dk[key]
    return default


def _bool_knob(knobs: dict, defaults: dict, key: str) -> "bool | None":
    for source in (knobs, defaults):
        if isinstance(source.get(key), bool):
            return source[key]
    return None


def _stt_model(stt_id: str, knobs: dict, defaults: dict):
    """The STT knob (transcription_model), then the registry default. Scribe has its own
    model names, so a Deepgram model left on an Agent that switched ears is ignored
    rather than sent to ElevenLabs as a model it does not have."""
    chosen = knobs.get("transcription_model")
    if stt_id == STT_ELEVENLABS and not (isinstance(chosen, str)
                                         and chosen.startswith("scribe")):
        chosen = None
    return chosen or defaults.get("model")


_LANGUAGE_CODE = re.compile(r"[a-z]{2,3}")


def scribe_language(language) -> "str | None":
    """A language Scribe accepts, or None for its auto-detect. Scribe refuses the whole
    session on a code it does not know (checked live: 'multi' and 'english' are
    `invalid_request`), and the Agent's language knob is shared with Deepgram, whose
    `multi` an Agent may still carry. So `multi` means auto-detect, a region tag keeps its
    language ('en-AU' -> 'en'), and anything else is dropped rather than sent."""
    if not isinstance(language, str) or not language.strip():
        return None
    code = language.strip().lower().replace("_", "-").split("-")[0]
    if code in ("multi", "auto"):
        return None
    if _LANGUAGE_CODE.fullmatch(code):
        return code
    logger.warning("STT language %r is not a language code Scribe accepts - using its "
                   "auto-detect instead", language)
    return None


def _stt_language(stt_id: str, knobs: dict, defaults: dict):
    language = knobs.get("language") or defaults.get("language")
    return scribe_language(language) if stt_id == STT_ELEVENLABS else language


def build_cascade_config(doc: dict, registry: dict, env: dict,
                         base_prompt: str = CASCADE_BASE_PROMPT) -> dict:
    """Build the effective cascade pipeline config from a validated draft/agent doc.

    The SINGLE builder behind bench session create, the effective-config preview AND
    the live lane's session setup. Raises CascadeConfigError for a non-cascade doc or
    a missing/unknown stage provider. ``base_prompt`` is the lane's system-prompt base
    (bench default here; the live lane passes its phone-call base) — persona composition
    always goes through ``profiles.ActiveProfile.compose_instructions``, never a fork.
    """
    if doc.get("pipeline") != "cascade":
        raise CascadeConfigError(
            f"pipeline: build_cascade_config needs pipeline: cascade (got "
            f"{doc.get('pipeline')!r})")
    knobs = doc.get("knobs") if isinstance(doc.get("knobs"), dict) else {}

    stt_id = _stage_provider(doc, "stt")
    stt_entry = _entry(registry, stt_id, "stt")
    stt_defaults = stt_entry.get("default_knobs") or {}
    stt = {
        "provider": stt_id,
        "secret_env": stt_entry.get("secret_env"),
        "wired": stt_id in CASCADE_WIRING["stt"],
        "wired_live": stt_id in CASCADE_WIRING["stt_live"],
        # STT model = the STT knob (transcription_model) then the registry default —
        # NEVER knobs.model, which is the LLM stage's knob (they share the flat knobs map
        # but must not cross stages; a bad LLM model must fail at LLM, not STT).
        "model": _stt_model(stt_id, knobs, stt_defaults),
        "language": _stt_language(stt_id, knobs, stt_defaults),
        # Deepgram keyterm prompting (s8 c4): profile knob > registry default > none.
        # A list of proper nouns the STT should bias toward ("Hermes", agent names) —
        # threaded verbatim into the live /listen query, empty by default.
        "keyterms": list(knobs.get("keyterms")
                         or stt_defaults.get("keyterms") or []),
        # VC24: Deepgram formatting switches. None = "not set on this Agent", which the
        # live client turns into "not sent", so an untouched Agent's /listen URL is
        # unchanged.
        "smart_format": _bool_knob(knobs, stt_defaults, "smart_format"),
        "numerals": _bool_knob(knobs, stt_defaults, "numerals"),
    }

    llm_id = _stage_provider(doc, "llm")
    llm_entry = _entry(registry, llm_id, "llm")
    # compose_instructions is ActiveProfile's — reuse it verbatim (no forked assembly).
    persona_holder = profiles.ActiveProfile(
        agent_id=str(doc.get("id")), source="<draft>", doc=doc, registry=registry)
    # VC24: when the llm stage IS a Hermes profile there is no static vendor endpoint.
    # The Agent's own hermes_profile picks the gateway, through the one resolver every
    # service shares. An unroutable profile leaves the endpoint None, which the live lane
    # refuses at call setup exactly like any other unwired stage - it never borrows
    # another Agent's backend.
    hermes_direct = llm_id == profiles.HERMES_LLM_PROVIDER
    hermes_profile = hermes_gateway.hermes_profile_of(doc) if hermes_direct else None
    llm = {
        "provider": llm_id,
        "kind": "hermes" if hermes_direct else "chat",
        "hermes_profile": hermes_profile,
        # The Agent's tools setting (guardrails.on_call_tools), as the wire value the
        # direct lane sends on every turn. None off that lane: a vendor LLM's tools are
        # cut in this process (``tools_enabled``), not by a request field.
        "tool_choice": (hermes_gateway.tool_choice_for(profiles.on_call_tools_of(doc))
                        if hermes_direct else None),
        "secret_env": llm_entry.get("secret_env"),
        "wired": llm_id in CASCADE_WIRING["llm"],
        "endpoint": (hermes_gateway.gateway_url_for_profile(hermes_profile, env)
                     if hermes_direct else LLM_ENDPOINTS.get(llm_id)),
        "model": _knob(knobs, llm_entry, "model"),
        "temperature": _knob(knobs, llm_entry, "temperature", 0.7),
        "extra_body": dict(LLM_EXTRA_BODY.get(llm_id, {})),
        "extra_headers": dict(LLM_EXTRA_HEADERS.get(llm_id, {})),
        "system_prompt": persona_holder.compose_instructions(base_prompt),
    }

    tts_id = _stage_provider(doc, "tts")
    tts_entry = _entry(registry, tts_id, "tts")
    tts_defaults = tts_entry.get("default_knobs") or {}
    # Voice precedence: profile > env > registry default_knobs (the profiles doctrine).
    # The ELEVENLABS_VOICE_ID env var is ElevenLabs-shaped — it must never fill an
    # Aura (or any other provider's) voice slot.
    env_voice = ((env.get(TTS_ENV_VOICE) or "").strip()
                 if tts_id == "elevenlabs" else "")
    voice = knobs.get("voice") or env_voice or tts_defaults.get("voice") or None
    voice_source = ("profile" if knobs.get("voice") else
                    "env" if env_voice else
                    "registry" if tts_defaults.get("voice") else "unset")
    tts = {
        "provider": tts_id,
        "secret_env": tts_entry.get("secret_env"),
        "wired": tts_id in CASCADE_WIRING["tts"],
        "wired_live": tts_id in CASCADE_WIRING["tts_live"],
        "voice": voice,
        "voice_source": voice_source,
        # Aura has no speed control — carrying one through would be a knob with no
        # runtime effect (the editor greys it with this exact reason).
        "speed": (_knob(knobs, tts_entry, "speed", 1.0)
                  if tts_id != "deepgram-aura" else None),
        "format": knobs.get("format") or DEFAULT_TTS_FORMATS.get(
            tts_id, DEFAULT_TTS_FORMAT),
        "model": ELEVENLABS_MODEL if tts_id == "elevenlabs" else None,
    }
    return {"pipeline": "cascade", "stt": stt, "llm": llm, "tts": tts}
