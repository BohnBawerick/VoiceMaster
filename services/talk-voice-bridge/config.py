"""Environment parsing for the Mode V audio sidecar. Pure and testable — no import side effects."""
import os
from dataclasses import dataclass, replace

from voicecore import hindsight
from voicecore import profiles


@dataclass(frozen=True)
class Config:
    nextcloud_base_url: str
    talk_user: str
    voice_app_password: str
    # Real ai-agent account password — REQUIRED for the headless browser login form.
    # Nextcloud rejects app-passwords at the interactive web login form (they only work
    # for OCS/DAV Basic-auth), so voice_app_password CANNOT authenticate the browser.
    login_password: str
    openai_api_key: str
    openai_voice: str
    openai_model: str
    hermes_gateway_url: str
    hermes_gateway_token: str
    hermes_timeout: float
    config_dir: str
    # Idea 3: input-transcription model. gpt-4o-transcribe is same-price as whisper-1 but better on
    # accents / proper nouns / technical vocab. Env-overridable (VOICE_TRANSCRIPTION_MODEL).
    transcription_model: str = "gpt-4o-transcribe"
    # Idea 1: dead-air filler while a slow hermes tool call runs. Debounced — only spoken if the
    # backend hasn't returned within filler_debounce_ms, so trivial sub-2s turns stay snappy.
    filler_debounce_ms: int = 1500
    filler_text: str = "One sec, let me check that."
    # Idea 4: retain call transcripts into the call archive. With a Hindsight URL that is a
    # Hindsight bank (s5, ticket 05: default `voice`, this app's own call archive, not the
    # shared `hermes` bank); empty, it is the built-in SQLite store (voicecore.call_store).
    hindsight_url: str = hindsight.DEFAULT_URL
    hindsight_bank: str = hindsight.DEFAULT_BANK
    retain_enabled: bool = True
    audio_rate: int = 24000
    # Server-VAD end-of-turn silence window (ms). This is the single biggest fixed latency
    # per turn: OpenAI waits this long after the caller goes quiet before deciding the turn
    # ended and starting to respond. Lowered from OpenAI's 500 ms default to 250 ms to shave
    # ~250 ms off every reply. Too low → a mid-sentence pause is misread as end-of-turn and
    # the agent talks over the caller; ~250 ms is the usual sweet spot. Env-tunable (no
    # rebuild) via TALK_VOICE_VAD_SILENCE_MS.
    vad_silence_ms: int = 250
    # Idle-watchdog backstop. MUST stay comfortably above approval_timeout: a guest's
    # approval wait blocks the receive loop (no speech events → no idle reset), so an
    # idle_timeout <= approval_timeout could tear the call down mid-approval. See
    # RealtimeBridge._handle_tool (it also refreshes _last_activity around the wait).
    idle_timeout: float = 240.0
    approval_timeout: float = 90.0
    port: int = 3338
    # Outbound-call report-back defaults (see outbound.py). home_room is the owner's Talk
    # home conversation — the default/fallback room a call transcript is delivered to.
    # telegram_bot_token is OPTIONAL: only needed if a call was triggered from Telegram and
    # the transcript should return there (else outbound falls back to the Talk home room).
    home_room: str = ""
    telegram_bot_token: str = ""


# Reload-stable sentinel (the parity harness importlib.reload()s this module; default
# args bound before a reload must still `is`-match the module global afterwards).
_UNSET = globals().get("_UNSET", object())


def load(profile=_UNSET) -> Config:
    """Env-derived Config, with the VOICE_AGENT profile overlaid when one is selected.

    No profile selected (VOICE_AGENT unset/blank) => the returned Config is EXACTLY the
    env-derived one below — profiles.load_active_profile touches no files on that path,
    so VOICE_CONFIG_DIR may be missing entirely (s1 drop-in invariant). A selected but
    broken profile raises profiles.ProfileError — loud, no env fallback.

    s3: pass ``profile`` (an ActiveProfile or None) to overlay a snapshot the caller
    already loaded — the per-call-setup path (CallSession.start) resolves the profile
    exactly ONCE and threads it here, so config and session.update can never disagree.
    """
    cfg = _load_env()
    if profile is _UNSET:
        profile = profiles.load_active_profile()
    return overlay_profile(cfg, profile)


def load_base() -> Config:
    """The pure env-derived Config, NO profile overlay — the process-wide base the s3
    per-call-setup path overlays freshly on every call (see CallSession.start)."""
    return _load_env()


def overlay_profile(cfg: Config, profile) -> Config:
    """``cfg`` with a profile snapshot overlaid; ``profile is None`` returns cfg as-is.

    Precedence per knob: profile > env > registry default_knobs > coded default.
    The cfg field passed as fallback IS "env if set else coded default", so resolve()
    only has to distinguish the env-set case (env beats registry defaults).
    s3 also overlays memory.retain onto retain_enabled (profile bool > env).
    """
    if profile is None:
        return cfg
    # vad_silence_ms + retain_enabled are pipeline-agnostic; the other three are
    # realtime-only knobs a cascade session never reads (s14b — overlaying them for
    # cascade also used to crash, see profiles._registry_default_knobs).
    overlays = dict(
        vad_silence_ms=profile.resolve(
            ("vad", "silence_ms"), "TALK_VOICE_VAD_SILENCE_MS", cfg.vad_silence_ms, cast=int),
        retain_enabled=profile.retain_enabled(cfg.retain_enabled),
    )
    if profile.pipeline != "cascade":
        overlays.update(
            openai_voice=profile.resolve(("voice",), "OPENAI_VOICE", cfg.openai_voice),
            openai_model=profile.resolve(
                ("model",), "OPENAI_REALTIME_MODEL", cfg.openai_model),
            transcription_model=profile.resolve(
                ("transcription_model",), "VOICE_TRANSCRIPTION_MODEL",
                cfg.transcription_model),
        )
    return replace(cfg, **overlays)


def _load_env() -> Config:
    return Config(
        nextcloud_base_url=os.environ.get("NEXTCLOUD_BASE_URL", "").rstrip("/"),
        talk_user=os.environ.get("NEXTCLOUD_TALK_USER", "ai-agent"),
        voice_app_password=os.environ.get("NEXTCLOUD_VOICE_APP_PASSWORD", ""),
        login_password=os.environ.get("NEXTCLOUD_VOICE_LOGIN_PASSWORD", ""),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        openai_voice=os.environ.get("OPENAI_VOICE", "cedar"),
        openai_model=os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2"),
        hermes_gateway_url=os.environ.get("HERMES_GATEWAY_URL", "http://localhost:18789"),
        hermes_gateway_token=os.environ.get("HERMES_GATEWAY_TOKEN", ""),
        hermes_timeout=float(os.environ.get("HERMES_TIMEOUT", "120")),
        config_dir=os.environ.get("HERMES_CONFIG_DIR", "/app/config"),
        transcription_model=os.environ.get("VOICE_TRANSCRIPTION_MODEL", "gpt-4o-transcribe"),
        filler_debounce_ms=int(os.environ.get("VOICE_FILLER_DEBOUNCE_MS", "1500")),
        filler_text=os.environ.get("VOICE_FILLER_TEXT", "One sec, let me check that."),
        hindsight_url=os.environ.get("HINDSIGHT_URL", hindsight.DEFAULT_URL),
        hindsight_bank=os.environ.get("HINDSIGHT_BANK", hindsight.DEFAULT_BANK),
        retain_enabled=os.environ.get("VOICE_RETAIN_ENABLED", "true").strip().lower()
            in ("1", "true", "yes", "on"),
        audio_rate=int(os.environ.get("AUDIO_RATE", "24000")),
        vad_silence_ms=int(os.environ.get("TALK_VOICE_VAD_SILENCE_MS", "250")),
        idle_timeout=float(os.environ.get("TALK_VOICE_IDLE_TIMEOUT", "240")),
        approval_timeout=float(os.environ.get("TALK_VOICE_APPROVAL_TIMEOUT", "90")),
        port=int(os.environ.get("PORT", "3338")),
        home_room=os.environ.get("NEXTCLOUD_TALK_HOME_CONVERSATION", ""),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
    )
