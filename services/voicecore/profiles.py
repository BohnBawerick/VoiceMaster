"""Voice-agent profile loader/validator (s1 Voice Control Plane).

Both bridges and the dashboard import THIS loader. Pure stdlib + PyYAML; no import
side effects, no network, no caching.

Contract-critical behavior (s1):
  - Activation is EXPLICIT: only a non-blank VOICE_AGENT selects a profile. Blank or
    whitespace-only VOICE_AGENT is unset. With no profile selected nothing here reads
    any file — VOICE_CONFIG_DIR, providers.yaml and agents/ may be missing, unreadable
    or garbage and both bridges behave exactly as their env-derived defaults.
  - With VOICE_AGENT set, every problem is a HARD ProfileError (missing dir, missing
    id, invalid YAML, schema violation, disabled profile, cascade pipeline, non-OpenAI
    realtime provider, duplicate ids). There is NO fallback to env — a selected
    profile either loads or the call path refuses to run.
  - Effective knob precedence: profile > env > registry default_knobs > coded default.
    Registry defaults are FILL ONLY (knob absent from both profile and env) and only
    participate when a profile is selected.
  - Secrets are env-var NAMES via the registry's secret_env; inline credentials
    (api_key/token/secret/password keys at any depth) are rejected.
  - Outlet axis (s16): an Outlet is first-class ('phone' = the Twilio number, 'talk'
    = Nextcloud Talk). active.yaml assigns per outlet and direction, and that is the
    ONLY shape this loader knows (s17 deleted the pre-s16 flat shape). A slot naming
    a missing/invalid/disabled agent fails loud for THAT outlet+direction only -
    never a silent fallback on a live call path.

CLI: ``python -m voicecore.profiles validate <config-dir>`` — exit 0 iff every agent file in
<config-dir>/agents/ validates against the registry; errors (with file + field path)
go to stderr.
"""
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ENV_AGENT = "VOICE_AGENT"
ENV_CONFIG_DIR = "VOICE_CONFIG_DIR"

# s3: sentinel for "no profile snapshot was passed" — distinct from a real None, which
# is a legitimate snapshot meaning no-profile. globals().get keeps the sentinel's
# IDENTITY stable across importlib.reload (the parity harnesses reload modules in
# place; default args bound pre-reload must still compare `is`-equal post-reload).
UNSET = globals().get("UNSET", object())

# In-repo canonical config dir (services/voice-config/), the fallback when
# VOICE_CONFIG_DIR is unset or carries no providers.yaml. Resolves to the SAME
# directory from both services (…/services/<service>/profiles.py -> …/services/).
CANONICAL_CONFIG_DIR = Path(__file__).resolve().parent.parent / "voice-config"

ROLES = ("realtime", "llm", "stt", "tts")
PIPELINES = ("realtime", "cascade")
DIRECTIONS = ("inbound", "outbound", "both")

# The only realtime lane actually wired in s1. Anything else must fail loud.
IMPLEMENTED_REALTIME_PROVIDER = "openai-gpt-realtime"

# VC24: which cascade calls THIS PROCESS can run, named by Outlet and direction:
# {outlet: frozenset(directions)}. Every service imports THIS module, so capability is
# a runtime declaration: the phone bridge declares its own Outlet, the Talk bridge
# declares its own, and the dashboard declares both so its Outlet-assignment guard and
# per-slot health check say what the bridges will say. It replaced a single boolean
# (CASCADE_OUTBOUND_HOST) that could not name a direction, which is why inbound cascade
# had to be refused in three separate places. Empty = this process runs no cascade call.
# Reload-stable so the parity harness's importlib.reload() can't silently re-arm the
# refusal mid-suite.
CASCADE_CAPABILITY = globals().get("CASCADE_CAPABILITY", {})

# VC24: the registry id of the LLM stage that is a Hermes profile itself. A cascade
# Agent whose llm stage is this provider is the direct lane: Deepgram hears, the
# Agent's own hermes_profile thinks, ElevenLabs speaks. It is the ONLY cascade shape
# that may answer an inbound call; an outside-vendor cascade stays outbound-only.
HERMES_LLM_PROVIDER = "hermes-agent"

# Kill switch in the style of VOICE_RECORDING_ENABLED: false removes the direct lane from
# the process with no rebuild. A direct Agent then fails activation naming this variable,
# so an assigned Outlet answers from its last-known-good snapshot or refuses loudly.
ENV_HERMES_DIRECT = "VOICE_HERMES_DIRECT_ENABLED"


def declare_cascade_host(outlet: str, directions) -> None:
    """Declare that this process runs cascade calls on ``outlet`` in ``directions``."""
    CASCADE_CAPABILITY[outlet] = frozenset(directions)


def is_hermes_direct(doc) -> bool:
    """True when this Agent document is the direct Hermes lane."""
    if not isinstance(doc, dict) or doc.get("pipeline") != "cascade":
        return False
    stages = doc.get("providers")
    return isinstance(stages, dict) and stages.get("llm") == HERMES_LLM_PROVIDER


def hermes_direct_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    raw = (env.get(ENV_HERMES_DIRECT) or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


_SECRET_KEYS = frozenset({"api_key", "token", "secret", "password"})
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")

_TOP_LEVEL_KEYS = frozenset({
    "id", "description", "enabled", "hermes_profile", "direction", "pipeline",
    "providers", "knobs", "persona", "number_policy", "talk_policy", "guardrails",
    "memory",
})

# s11b-1: the two dialed forms a Talk (Mode V) target can take. A username is resolved
# to a 1:1 room token via OCS; a room token (incl. group) is dialed verbatim. The
# talk_policy.allow gate matches on this DIALED form BEFORE any resolve.
_TALK_TARGET_KINDS = frozenset({"username", "token"})
# voice/model/transcription_model/vad drive the realtime lane; the cascade lane (s4)
# reuses model (llm), transcription_model (stt) and voice (tts) and adds language (stt),
# temperature (llm), speed (tts) and format (tts). A given agent is one pipeline, so the
# shared keys never collide at runtime.
# VC24: smart_format and numerals are Deepgram /listen switches (stt). Deepgram's own
# endpointing is deliberately NOT a knob: turn-taking is VoiceMaster's (turn_detect), so
# it would be a setting with no effect.
_KNOB_KEYS = frozenset({"voice", "model", "transcription_model", "vad",
                        "language", "temperature", "speed", "format", "keyterms",
                        "smart_format", "numerals"})
_STR_KNOB_KEYS = ("voice", "model", "transcription_model", "language", "format")
_BOOL_KNOB_KEYS = ("smart_format", "numerals")
_VAD_KEYS = frozenset({"silence_ms", "threshold", "prefix_padding_ms"})

# Registry entry schema: name -> (required, type-check description)
_REGISTRY_REQUIRED = ("id", "role", "display_name", "secret_env", "capabilities", "default_knobs")
_REGISTRY_OPTIONAL = ("cost_hint", "latency_hint")


class ProfileError(RuntimeError):
    """Any hard failure in registry/profile loading or validation."""


def _yaml():
    # Lazy import: the no-profile path must never require PyYAML at module import.
    import yaml

    return yaml


# ---------------------------------------------------------------------------
# Activation + path resolution
# ---------------------------------------------------------------------------

def agent_id_from_env(env=None) -> "str | None":
    """The selected agent id, or None. Blank/whitespace VOICE_AGENT counts as unset."""
    env = os.environ if env is None else env
    raw = env.get(ENV_AGENT, "")
    aid = raw.strip()
    return aid or None


def config_dir(env=None) -> Path:
    env = os.environ if env is None else env
    raw = (env.get(ENV_CONFIG_DIR) or "").strip()
    return Path(raw) if raw else CANONICAL_CONFIG_DIR


def registry_path(directory: "Path | None" = None, env=None) -> Path:
    """providers.yaml under the given/env config dir, else the canonical registry."""
    base = Path(directory) if directory is not None else config_dir(env)
    candidate = base / "providers.yaml"
    if candidate.is_file():
        return candidate
    return CANONICAL_CONFIG_DIR / "providers.yaml"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def load_registry(directory: "Path | None" = None, env=None) -> dict:
    """Load + validate the provider registry. Returns {id: entry}. Raises ProfileError."""
    path = registry_path(directory, env)
    if not path.is_file():
        raise ProfileError(f"provider registry not found: {path}")
    try:
        doc = _yaml().safe_load(path.read_text())
    except Exception as e:  # noqa: BLE001 — yaml error classes vary
        raise ProfileError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("providers"), list):
        raise ProfileError(f"{path}: expected a top-level 'providers' list")
    errors: list = []
    entries: dict = {}
    for i, entry in enumerate(doc["providers"]):
        where = f"{path}: providers[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: entry must be a map")
            continue
        for key in _REGISTRY_REQUIRED:
            if key not in entry:
                errors.append(f"{where}.{key}: required key missing")
        eid = entry.get("id")
        if isinstance(eid, str):
            where = f"{path}: providers[{i}] ({eid})"
            if eid in entries:
                errors.append(f"{where}.id: duplicate provider id '{eid}'")
        if "role" in entry and entry.get("role") not in ROLES:
            errors.append(f"{where}.role: '{entry.get('role')}' not one of {list(ROLES)}")
        se = entry.get("secret_env")
        if se is not None and (not isinstance(se, str) or not _ENV_NAME_RE.match(se)):
            errors.append(f"{where}.secret_env: '{se}' is not an env-var NAME "
                          "(^[A-Z][A-Z0-9_]*$) — secret VALUES are forbidden here")
        if "capabilities" in entry and not isinstance(entry.get("capabilities"), list):
            errors.append(f"{where}.capabilities: must be a list")
        if "default_knobs" in entry and not isinstance(entry.get("default_knobs"), dict):
            errors.append(f"{where}.default_knobs: must be a map")
        for key in _REGISTRY_OPTIONAL:
            if key in entry and not isinstance(entry[key], str):
                errors.append(f"{where}.{key}: must be a string")
        if isinstance(eid, str):
            entries[eid] = entry
    if errors:
        raise ProfileError("\n".join(str(e) for e in errors))
    return entries


# ---------------------------------------------------------------------------
# Profile validation
# ---------------------------------------------------------------------------

def _scan_inline_secrets(node, path: str, errors: list) -> None:
    """Reject credential-shaped keys at ANY depth — secrets go through the registry's
    secret_env indirection (env-var NAMES), never inline in a profile."""
    if isinstance(node, dict):
        for k, v in node.items():
            child = f"{path}.{k}" if path else str(k)
            if isinstance(k, str) and k.lower() in _SECRET_KEYS:
                errors.append(
                    f"{child}: inline credentials are forbidden — use the registry's "
                    f"secret_env indirection (env-var NAMES only) instead of '{k}'")
            else:
                _scan_inline_secrets(v, child, errors)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _scan_inline_secrets(v, f"{path}[{i}]", errors)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_profile(doc, registry: dict, source) -> list:
    """Validate one agent document. Returns a list of '<file>: <path>: <msg>' strings.

    Schema-only POLICY: `enabled: false` and unimplemented realtime providers are VALID
    documents — they are rejected at ACTIVATION (load_active_profile), not here, so the
    CLI can validate a whole config dir including paused agents.

    Provider SHAPE is not policy and is checked here (s15b-A). `pipeline: cascade` used
    to be waved through entirely: activation_problem returned before ever reading
    providers, so a cascade doc carrying `providers.realtime` — and none of
    {stt, llm, tts} — validated, activated, and only failed at call time. That is the
    s14b defect class; a cascade profile must be cascade-SHAPED, not merely
    cascade-NAMED.
    """
    errors: list = []

    def err(path: str, msg: str) -> None:
        errors.append(f"{source}: {path}: {msg}")

    if not isinstance(doc, dict):
        err("<document>", "profile must be a YAML map")
        return errors

    secret_errors: list = []
    _scan_inline_secrets(doc, "", secret_errors)
    for e in secret_errors:
        path, _, msg = e.partition(": ")
        err(path, msg)

    for key in doc:
        if key not in _TOP_LEVEL_KEYS:
            err(str(key), f"unknown top-level key (allowed: {sorted(_TOP_LEVEL_KEYS)})")

    aid = doc.get("id")
    if not isinstance(aid, str) or not _ID_RE.match(aid):
        err("id", f"required, must match {_ID_RE.pattern} (got {aid!r})")

    for key in ("description", "hermes_profile", "persona"):
        if key in doc and not isinstance(doc[key], str):
            err(key, "must be a string")
    if "enabled" in doc and not isinstance(doc["enabled"], bool):
        err("enabled", "must be a boolean")
    if "direction" in doc and doc["direction"] not in DIRECTIONS:
        err("direction", f"'{doc.get('direction')}' not one of {list(DIRECTIONS)}")

    pipeline = doc.get("pipeline")
    if pipeline not in PIPELINES:
        err("pipeline", f"required, one of {list(PIPELINES)} (got {pipeline!r})")

    providers = doc.get("providers")
    if providers is not None and not isinstance(providers, dict):
        err("providers", "must be a map of role -> provider id")
        providers = None
    if isinstance(providers, dict):
        for role, pid in providers.items():
            ppath = f"providers.{role}"
            if role not in ROLES:
                err(ppath, f"unknown provider role (allowed: {list(ROLES)})")
                continue
            if pid is None:
                continue  # explicit-null handled with the pipeline check below
            if not isinstance(pid, str):
                err(ppath, "must be a provider id string")
                continue
            entry = registry.get(pid)
            if entry is None:
                err(ppath, f"unknown provider id '{pid}' (not in the registry)")
            elif entry.get("role") != role:
                err(ppath, f"provider '{pid}' has role '{entry.get('role')}', "
                           f"but is referenced as '{role}'")
    if pipeline == "realtime":
        rt = (providers or {}).get("realtime") if isinstance(providers, (dict, type(None))) else None
        if rt is None or not isinstance(rt, str) or not rt.strip():
            err("providers.realtime",
                "required for pipeline: realtime and may not be null — there is no "
                "implicit default provider")

    if pipeline == "cascade":
        cas = providers if isinstance(providers, dict) else {}
        if "realtime" in cas:
            err("providers.realtime",
                "must not be set for pipeline: cascade — the cascade lane resolves "
                "{stt, llm, tts} and never reads a realtime provider. A cascade-NAMED, "
                "realtime-SHAPED profile is the s14b defect class: it validated, "
                "activated, and then crashed at call time")
        for _role in ("stt", "llm", "tts"):
            _pid = cas.get(_role)
            if _pid is None or not isinstance(_pid, str) or not _pid.strip():
                err(f"providers.{_role}",
                    "required for pipeline: cascade and may not be null — the cascade "
                    "lane resolves all three of {stt, llm, tts}")

    knobs = doc.get("knobs")
    if knobs is not None and not isinstance(knobs, dict):
        err("knobs", "must be a map")
        knobs = None
    if isinstance(knobs, dict):
        for key in knobs:
            if key not in _KNOB_KEYS:
                err(f"knobs.{key}", f"unknown knob (allowed: {sorted(_KNOB_KEYS)})")
        for key in _STR_KNOB_KEYS:
            if key not in knobs:
                continue
            if not isinstance(knobs[key], str):
                err(f"knobs.{key}", "must be a string")
            elif not knobs[key].strip():
                # c9: an empty/whitespace string is REJECTED, never sent as model=""
                # upstream. Omit the key to inherit the env/registry/coded default.
                err(f"knobs.{key}",
                    "must not be empty — omit the key to inherit the default "
                    "(env → registry default_knobs → coded default)")
        for key in _BOOL_KNOB_KEYS:
            if key in knobs and not isinstance(knobs[key], bool):
                err(f"knobs.{key}", f"must be a boolean (got {knobs[key]!r})")
        if "temperature" in knobs and not _is_num(knobs["temperature"]):
            err("knobs.temperature", f"must be a number (got {knobs['temperature']!r})")
        elif "temperature" in knobs and not 0 <= knobs["temperature"] <= 2:
            err("knobs.temperature",
                f"must be a number in [0, 2] (got {knobs['temperature']!r})")
        if "speed" in knobs and not _is_num(knobs["speed"]):
            err("knobs.speed", f"must be a number (got {knobs['speed']!r})")
        elif "speed" in knobs and not 0.25 <= knobs["speed"] <= 4.0:
            err("knobs.speed",
                f"must be a number in [0.25, 4.0] (got {knobs['speed']!r})")
        kt = knobs.get("keyterms")
        if kt is not None and not isinstance(kt, list):
            err("knobs.keyterms",
                "must be a list of non-empty strings (Deepgram keyterm prompting) — "
                "omit the key for none")
        elif isinstance(kt, list):
            for i, term in enumerate(kt):
                if not isinstance(term, str) or not term.strip():
                    err(f"knobs.keyterms[{i}]",
                        f"must be a non-empty string (got {term!r})")
        vad = knobs.get("vad")
        if vad is not None and not isinstance(vad, dict):
            err("knobs.vad", "must be a map")
            vad = None
        if isinstance(vad, dict):
            for key in vad:
                if key not in _VAD_KEYS:
                    err(f"knobs.vad.{key}", f"unknown vad knob (allowed: {sorted(_VAD_KEYS)})")
            for key in ("silence_ms", "prefix_padding_ms"):
                if key in vad and not _is_int(vad[key]):
                    err(f"knobs.vad.{key}", f"must be an integer (got {vad[key]!r})")
                elif key in vad and not 0 <= vad[key] <= 10000:
                    err(f"knobs.vad.{key}",
                        f"must be between 0 and 10000 ms (got {vad[key]!r})")
            if "threshold" in vad and not _is_num(vad["threshold"]):
                err("knobs.vad.threshold", f"must be a number (got {vad['threshold']!r})")
            elif "threshold" in vad and not 0 <= vad["threshold"] <= 1:
                err("knobs.vad.threshold",
                    f"must be a number in [0, 1] (got {vad['threshold']!r})")

    for key in ("guardrails", "memory"):
        if key in doc and doc[key] is not None and not isinstance(doc[key], dict):
            err(key, "must be a map")
    if "number_policy" in doc and doc["number_policy"] is not None \
            and not isinstance(doc["number_policy"], (dict, list, str)):
        err("number_policy", "must be a map, list or string")

    # s3 pinned schemas for the WIRED fields (guardrails.on_call_tools boolean,
    # number_policy.allow = list of E.164 strings, memory.retain boolean).
    guardrails = doc.get("guardrails")
    if isinstance(guardrails, dict) and "on_call_tools" in guardrails \
            and not isinstance(guardrails["on_call_tools"], bool):
        err("guardrails.on_call_tools",
            f"field path 'guardrails.on_call_tools': must be a boolean (got "
            f"{guardrails['on_call_tools']!r}); true allows tools, false disables them")
    memory = doc.get("memory")
    if isinstance(memory, dict) and "retain" in memory \
            and not isinstance(memory["retain"], bool):
        err("memory.retain",
            f"field path 'memory.retain': must be a boolean (got {memory['retain']!r})")
    np = doc.get("number_policy")
    if isinstance(np, dict) and "allow" in np:
        allow = np["allow"]
        if not isinstance(allow, list):
            err("number_policy.allow",
                f"field path 'number_policy.allow': must be a list of E.164 strings "
                f"(got {allow!r}); it REPLACES the env allow-list, [] means deny-all")
        else:
            for i, n in enumerate(allow):
                if not isinstance(n, str) or not _E164_RE.match(n):
                    err(f"number_policy.allow[{i}]",
                        f"field path 'number_policy.allow[{i}]': must be an E.164 "
                        f"string matching {_E164_RE.pattern} (got {n!r})")

    # s11b-1: talk_policy.allow — the Talk (Mode V) allow-list. Talk IDENTITIES
    # (usernames or room tokens), NOT E.164 numbers: a bare username is valid here even
    # though it would be rejected in number_policy.allow. Present REPLACES the allow-any
    # Talk posture ([] = deny-all); the gate matches on the dialed form pre-resolve.
    tp = doc.get("talk_policy")
    if "talk_policy" in doc and tp is not None and not isinstance(tp, dict):
        err("talk_policy", "field path 'talk_policy': must be a map")
    if isinstance(tp, dict) and "allow" in tp:
        allow = tp["allow"]
        if not isinstance(allow, list):
            err("talk_policy.allow",
                f"field path 'talk_policy.allow': must be a list of Talk identity "
                f"strings (usernames or room tokens; NOT E.164) — it REPLACES the "
                f"allow-any Talk posture, [] means deny-all (got {allow!r})")
        else:
            for i, t in enumerate(allow):
                if not isinstance(t, str) or not t.strip():
                    err(f"talk_policy.allow[{i}]",
                        f"field path 'talk_policy.allow[{i}]': must be a non-empty "
                        f"Talk identity string (username or room token; got {t!r})")

    return errors


def talk_allow_decision(kind: str, target: str, allow: "list | None") -> bool:
    """Kind-scoped, PRE-RESOLVE membership for talk_policy.allow (s11b-1).

    ``target`` is the identifier AS DIALED — a Talk username (``kind="username"``) or a
    room token (``kind="token"``) — matched BEFORE any OCS resolve, so a dry-run never
    creates a room and a listed username never has to equal its resolved token.
    ``allow`` None (field absent) = allow-any; ``[]`` = deny-all; else EXACT string match
    (target trimmed). number_policy.allow plays NO part here (E.164 gate is Twilio-only).
    An unknown ``kind`` fails closed."""
    if kind not in _TALK_TARGET_KINDS:
        return False
    if allow is None:
        return True
    return isinstance(target, str) and target.strip() in allow


# ---------------------------------------------------------------------------
# Directory scan + activation
# ---------------------------------------------------------------------------

def _scan_agents_dir(agents_dir: Path, *, context: str) -> "tuple[dict, dict]":
    """Parse every agents/*.yaml|*.yml. Returns ({id: doc}, {id: file}). Raises loud."""
    if not agents_dir.is_dir():
        raise ProfileError(f"{context}: agents directory not found: {agents_dir}")
    files = sorted(list(agents_dir.glob("*.yaml")) + list(agents_dir.glob("*.yml")))
    if not files:
        raise ProfileError(f"{context}: no agent files (*.yaml) in {agents_dir}")
    docs: dict = {}
    sources: dict = {}
    for f in files:
        try:
            doc = _yaml().safe_load(f.read_text())
        except Exception as e:  # noqa: BLE001 — yaml error classes vary
            raise ProfileError(f"{context}: invalid YAML in {f}: {e}") from e
        if not isinstance(doc, dict):
            raise ProfileError(f"{context}: {f} is not a YAML map")
        did = doc.get("id")
        if isinstance(did, str):
            if did in sources:
                raise ProfileError(
                    f"{context}: duplicate agent id '{did}' in {agents_dir} "
                    f"({sources[did].name} and {f.name})")
            docs[did], sources[did] = doc, f
        else:
            # File without a usable id: recorded under its filename so validate_dir
            # can still report its schema errors; never allowed to shadow a real id.
            if f.stem in sources:
                raise ProfileError(
                    f"{context}: duplicate agent id '{f.stem}' in {agents_dir} "
                    f"({sources[f.stem].name} and {f.name})")
            docs[f.stem], sources[f.stem] = doc, f
    return docs, sources


@dataclass(frozen=True)
class ActiveProfile:
    """A validated, activated agent profile plus the registry it resolved against."""

    agent_id: str
    source: str
    doc: dict
    registry: dict

    @property
    def persona(self) -> str:
        return self.doc.get("persona") or ""

    @property
    def pipeline(self) -> str:
        return self.doc.get("pipeline") or "realtime"

    @property
    def realtime_provider(self) -> str:
        return self.doc["providers"]["realtime"]

    def _registry_default_knobs(self) -> dict:
        """Registry default_knobs for this profile's REALTIME provider, or {} when the
        profile has no realtime provider at all.

        s14b: a cascade profile's providers block is {stt, llm, tts} — there is no
        realtime key, and reaching for one raised KeyError all the way out through
        resolve(), killing session.start() for every Talk cascade call. Cascade knob
        defaults come from the per-stage provider entries, not from here, so falling
        through to the caller's fallback is the correct answer rather than an error."""
        if "realtime" not in (self.doc.get("providers") or {}):
            return {}
        entry = self.registry.get(self.realtime_provider) or {}
        knobs = entry.get("default_knobs")
        return knobs if isinstance(knobs, dict) else {}

    @staticmethod
    def _dig(mapping, path):
        node = mapping
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return node

    def resolve(self, path: tuple, env_var: "str | None", fallback, env=None, cast=None):
        """Effective knob value: profile > env > registry default_knobs > fallback.

        ``fallback`` is the caller's env-derived value (env value if set, else the
        bridge's coded default) so coded defaults stay defined in ONE place — the
        bridge. ``cast`` applies to raw env strings only; YAML values are already typed.
        """
        v = self._dig(self.doc.get("knobs") or {}, path)
        if v is not None:
            return v
        env = os.environ if env is None else env
        if env_var and env_var in env:
            raw = env[env_var]
            return cast(raw) if cast else raw
        v = self._dig(self._registry_default_knobs(), path)
        if v is not None:
            return v
        return fallback

    def compose_instructions(self, base_prompt: str) -> str:
        """Pinned rule 4: base + "\\n\\n" + persona, exactly; no persona -> base."""
        persona = self.persona
        return f"{base_prompt}\n\n{persona}" if persona else base_prompt

    # -- s3 wired-field overlays (consumed at the bridges' REAL mechanisms) -----

    def retain_enabled(self, default: bool) -> bool:
        """memory.retain overlays the env retain flag: a profile boolean wins; the
        field absent on a selected profile falls through to ``default`` (the env)."""
        v = (self.doc.get("memory") or {}).get("retain")
        return v if isinstance(v, bool) else default

    @property
    def on_call_tools(self) -> bool:
        """The Agent's tools policy. Realtime ignores it inbound; Hermes Direct
        reads it on every call. See ``on_call_tools_of`` for defaults."""
        return on_call_tools_of(self.doc)

    def outbound_allow_list(self) -> "list | None":
        """number_policy.allow when present — the list REPLACES the env allow-list at
        the pre-dial gate ([] = deny-all). None = field absent (env list governs)."""
        np = self.doc.get("number_policy")
        if isinstance(np, dict) and isinstance(np.get("allow"), list):
            return list(np["allow"])
        return None

    def talk_allow_list(self) -> "list | None":
        """talk_policy.allow when present — the Talk (Mode V) allow-list of identities
        (usernames / room tokens), matched pre-resolve by talk_allow_decision ([] =
        deny-all). None = field absent (Talk stays allow-any). Mirrors
        outbound_allow_list(); number_policy plays no part in the Talk gate."""
        tp = self.doc.get("talk_policy")
        if isinstance(tp, dict) and isinstance(tp.get("allow"), list):
            return list(tp["allow"])
        return None


def on_call_tools_of(doc: dict) -> bool:
    """Missing means on for Hermes Direct only. Explicit false and malformed values
    stay off; other runtimes keep their existing default-off policy."""
    if not isinstance(doc, dict):
        return False
    guardrails = doc.get("guardrails", {})
    return (isinstance(guardrails, dict)
            and guardrails.get("on_call_tools", is_hermes_direct(doc)) is True)


def load_active_profile(env=None, direction: str = "inbound") -> "ActiveProfile | None":
    """Load the VOICE_AGENT-selected profile, or None when no profile is selected.

    None is the ONLY soft path (and it touches no files). Everything else raises
    ProfileError naming the agent id and the searched directory — never falls back
    to env-only config. ``direction`` feeds the s7 activation rules (cascade is
    outbound-only); the default keeps the historical inbound-conservative posture.
    """
    env = os.environ if env is None else env
    aid = agent_id_from_env(env)
    if aid is None:
        return None
    return _activate(aid, env, direction)


def load_named_profile(agent_id: str, direction: str = "outbound",
                       env=None) -> "ActiveProfile":
    """Load one named agent for a one-shot call.

    Does not read or write the pointer. ``VOICE_AGENT`` does not override —
    the caller asked for THIS agent. A missing, invalid or disabled agent
    raises ProfileError; a last-known-good snapshot of a different agent
    would be a lie about who was sent.
    """
    env = os.environ if env is None else env
    aid = (agent_id or "").strip()
    if not aid:
        raise ProfileError("agent id is required")
    return _activate(aid, env, direction)


def activation_problem(doc: dict, context: str, direction: str = "inbound",
                       outlet: "str | None" = None, env=None) -> "str | None":
    """The activation-time refusal for a schema-VALID profile doc, or None.

    Single source of the "valid but cannot run a call today" rules — shared by
    ``_activate`` (the live bridges) and the dashboard's Outlet-assignment guard and
    per-slot health check, so what the dashboard refuses to store can never drift from
    what a bridge refuses to answer.

    VC24: cascade runs where ``CASCADE_CAPABILITY`` says it does. Outbound cascade is
    unchanged. INBOUND cascade activates only for the direct Hermes lane
    (``providers.llm: hermes-agent``): an outside-vendor cascade still has no inbound
    prompt, no caller identity and no tool policy, so it stays outbound-only. ``outlet``
    narrows the check to one Outlet (the dashboard passes it per slot); None means "any
    Outlet this process hosts", which in a bridge is exactly its own.
    """
    aid = doc.get("id")
    if doc.get("enabled", True) is False:
        return (f"{context}: field path 'enabled': agent '{aid}' has enabled: false — "
                "refusing to load a disabled profile (no env fallback)")
    if doc.get("pipeline") == "cascade":
        hosted = (CASCADE_CAPABILITY if outlet is None
                  else {outlet: CASCADE_CAPABILITY.get(outlet, frozenset())})
        if not any(hosted.values()):
            return (f"{context}: pipeline cascade is not implemented in this bridge — "
                    "only 'realtime' runs here")
        direct = is_hermes_direct(doc)
        if direct and not hermes_direct_enabled(env):
            return (f"{context}: the direct Hermes lane is switched off "
                    f"({ENV_HERMES_DIRECT}=false) - agent '{aid}' cannot run until it "
                    "is switched back on")
        if direction != "outbound" and not direct:
            return (f"{context}: pipeline cascade is outbound-only unless Hermes itself "
                    f"is the llm stage (providers.llm: {HERMES_LLM_PROVIDER}) - an "
                    "outside-vendor cascade has no inbound lane")
        if not any(direction in directions for directions in hosted.values()):
            return (f"{context}: pipeline cascade does not run {direction} calls on "
                    f"{'outlet ' + repr(outlet) if outlet else 'this bridge'}")
        return None
    provider = doc["providers"]["realtime"]
    if provider != IMPLEMENTED_REALTIME_PROVIDER:
        return (f"{context}: realtime provider '{provider}' not implemented in s1 — only "
                f"'{IMPLEMENTED_REALTIME_PROVIDER}' is wired (refusing to dial OpenAI "
                "silently in its place)")
    return None


def _activate(aid: str, env, direction: str = "inbound",
              outlet: "str | None" = None) -> "ActiveProfile":
    """Load + validate + activation-check one agent id (shared by the VOICE_AGENT env
    path and the s3 active.yaml pointer path — identical errors either way)."""
    cdir = config_dir(env)
    agents_dir = cdir / "agents"
    context = f"agent '{aid}' (searched {agents_dir})"
    if not _ID_RE.match(aid):
        raise ProfileError(
            f"{context}: invalid agent id — must match {_ID_RE.pattern} "
            "(path separators and traversal are rejected)")
    docs, sources = _scan_agents_dir(agents_dir, context=context)
    if aid not in docs:
        raise ProfileError(f"{context}: not found (available: {sorted(docs) or 'none'})")
    registry = load_registry(cdir, env)
    errors = validate_profile(docs[aid], registry, sources[aid])
    if errors:
        raise ProfileError(f"{context}: invalid profile:\n" + "\n".join(str(e) for e in errors))
    doc = docs[aid]
    problem = activation_problem(doc, context, direction, outlet, env)
    if problem is not None:
        raise ProfileError(problem)
    return ActiveProfile(agent_id=aid, source=str(sources[aid]), doc=doc, registry=registry)


# ---------------------------------------------------------------------------
# active.yaml pointer (dashboard-owned owner intent, per outlet and direction)
# ---------------------------------------------------------------------------

ACTIVE_DIRECTIONS = ("inbound", "outbound")

# s16: the outlet axis. An Outlet is a first-class channel calls arrive on and
# leave from: 'phone' (the Twilio number) and 'talk' (Nextcloud Talk) exist today;
# a third outlet is one more entry in this tuple, not a model rework. Each outlet
# resolves its own Agent per direction.
OUTLETS = ("phone", "talk")
OUTLET_PHONE = "phone"
OUTLET_TALK = "talk"


def active_path(directory: "Path | None" = None, env=None) -> Path:
    base = Path(directory) if directory is not None else config_dir(env)
    return base / "active.yaml"


def _empty_pointer() -> "tuple[dict, dict]":
    return ({o: {d: None for d in ACTIVE_DIRECTIONS} for o in OUTLETS},
            {o: {d: None for d in ACTIVE_DIRECTIONS} for o in OUTLETS})


def _read_slot(path: Path, value, where: str) -> "tuple[str | None, str | None]":
    """One (outlet, direction) slot -> (id|None, problem|None)."""
    if value is None:
        return None, None
    if not isinstance(value, str) or not _ID_RE.match(value.strip()):
        return None, (f"{path}: field path '{where}': {value!r} is not a valid agent id "
                      f"(must match {_ID_RE.pattern}) - slot '{where}' treated as unset")
    return value.strip(), None


def _read_outlet_shape(path: Path, outlets: dict, pointer: dict, problems: dict) -> None:
    """The canonical s16 shape: outlets: {<outlet>: {inbound, outbound}}. Unknown
    outlet keys (a third outlet, say) and unknown keys inside an outlet entry are
    ignored - forward-compatible, never a model rework."""
    for outlet in OUTLETS:
        entry = outlets.get(outlet)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            msg = (f"{path}: field path 'outlets.{outlet}': must be a map shaped "
                   "{inbound: <id|null>, outbound: <id|null>}")
            for d in ACTIVE_DIRECTIONS:
                problems[outlet][d] = msg
            continue
        for direction in ACTIVE_DIRECTIONS:
            if direction not in entry:
                continue
            aid, problem = _read_slot(path, entry.get(direction),
                                      f"outlets.{outlet}.{direction}")
            pointer[outlet][direction] = aid
            problems[outlet][direction] = problem


def read_active_pointer(directory: "Path | None" = None, env=None) -> "tuple[dict, dict]":
    """Parse active.yaml -> ({outlet: {direction: id|None}}, {outlet: {direction: problem|None}}).

    There is exactly ONE shape (s17): ``outlets: {phone: {inbound: <id|null>,
    outbound: <id|null>}, talk: {inbound: <id|null>, outbound: <id|null>}}``.

    The pre-s16 flat shape - a top-level ``inbound`` / ``outbound`` applied to BOTH
    outlets - is gone, and a file that still carries one of those keys raises
    ProfileError rather than being read past. That is the point of s17: an
    assignment that names a direction but no Outlet cannot be honored per Outlet,
    so it used to be silently applied to every Outlet at once, which is how a
    single click on the old screen undid a per-outlet split. Refusing the shape
    is what makes that impossible rather than merely discouraged; ignoring the
    keys instead would turn a routing file into "nothing is assigned" with
    nobody told.

    ``problems`` maps each (outlet, direction) to None or a message describing a
    structurally invalid value (non-string / bad id shape / non-map entry) - the
    dashboard reports those as null with a warning; the bridges loud-fail ONLY that
    outlet+direction slot. Unknown extra keys are ignored. An absent file is exactly
    all-None (the s1 no-profile posture). Unparseable YAML / a non-map document
    raises ProfileError - there is no honest per-slot reading of a corrupt pointer
    file.
    """
    path = active_path(directory, env)
    pointer, problems = _empty_pointer()
    try:
        exists = path.is_file()
    except OSError:
        # s1 doctrine survives s3/s16: with no VOICE_AGENT, a missing/unreadable
        # config DIR means exactly env-derived defaults - an unstatable pointer
        # path is "absent".
        exists = False
    if not exists:
        return pointer, problems
    try:
        doc = _yaml().safe_load(path.read_text())
    except Exception as e:  # noqa: BLE001 — a pointer file that EXISTS but cannot be
        # read/parsed is owner intent we cannot honor — loud, never a silent fallback.
        raise ProfileError(f"{path}: invalid active pointer: {e}") from e
    if doc is None:
        return pointer, problems
    if not isinstance(doc, dict):
        raise ProfileError(
            f"{path}: active pointer must be a YAML map shaped "
            "{outlets: {<outlet>: {inbound: <id|null>, outbound: <id|null>}}}")
    flat = [d for d in ACTIVE_DIRECTIONS if d in doc]
    if flat:
        raise ProfileError(
            f"{path}: top-level {flat} is not an assignment - every assignment "
            f"names an Outlet (one of {list(OUTLETS)}). Move each value under the "
            "Outlet it belongs to: {outlets: {<outlet>: {inbound: <id|null>, "
            "outbound: <id|null>}}}")
    if "outlets" not in doc:
        return pointer, problems
    outlets = doc.get("outlets")
    if not isinstance(outlets, dict):
        msg = (f"{path}: field path 'outlets': must be a map of outlet -> "
               "{inbound: <id|null>, outbound: <id|null>}")
        for o in OUTLETS:
            for d in ACTIVE_DIRECTIONS:
                problems[o][d] = msg
    else:
        _read_outlet_shape(path, outlets, pointer, problems)
    return pointer, problems


def load_effective_profile(direction: str, outlet: "str | None" = None,
                           env=None) -> "ActiveProfile | None":
    """The ONE activation resolution a call setup must use (s3 + s16).

    ``outlet`` selects which Outlet's assignment resolves: 'phone' (the Twilio
    number) or 'talk' (Nextcloud Talk). Every bridge call site passes its own
    outlet explicitly; the default exists so pre-outlet call sites and tests keep
    resolving the phone outlet. Precedence: a non-blank VOICE_AGENT always wins
    (the pointer is inert while env is set; the env var lives in each bridge
    container, so it is inherently per-outlet); otherwise the active.yaml pointer
    for (outlet, direction). A null/absent pointer means exactly no-profile
    (returns None). A pointer naming a missing, invalid or disabled agent - or
    carrying a structurally invalid value - raises ProfileError for THIS
    outlet+direction only (every other slot resolves independently). Callers load
    ONCE per call setup and thread the returned snapshot through URL and session
    building (rule 3: no re-reads mid-setup, no cross-call caching).
    """
    env = os.environ if env is None else env
    if agent_id_from_env(env) is not None:
        return load_active_profile(env, direction)
    if direction not in ACTIVE_DIRECTIONS:
        raise ProfileError(
            f"unknown call direction {direction!r} (expected one of {list(ACTIVE_DIRECTIONS)})")
    outlet = OUTLET_PHONE if outlet is None else outlet
    if outlet not in OUTLETS:
        raise ProfileError(f"unknown outlet {outlet!r} (expected one of {list(OUTLETS)})")
    pointer, problems = read_active_pointer(None, env)
    if problems[outlet][direction]:
        raise ProfileError(problems[outlet][direction])
    aid = pointer[outlet][direction]
    if aid is None:
        return None
    return _activate(aid, env, direction, outlet)


# ---------------------------------------------------------------------------
# CLI: python -m voicecore.profiles validate <config-dir>
# ---------------------------------------------------------------------------

def validate_dir(directory: Path) -> list:
    """Validate every agent file in <dir>/agents/ against the resolved registry."""
    directory = Path(directory)
    try:
        registry = load_registry(directory)
    except ProfileError as e:
        return [str(e)]
    try:
        docs, sources = _scan_agents_dir(directory / "agents",
                                         context=f"config dir {directory}")
    except ProfileError as e:
        return [str(e)]
    errors: list = []
    for did in sorted(docs):
        errors.extend(validate_profile(docs[did], registry, sources[did]))
    return errors


def _cli(argv) -> int:
    if len(argv) != 2 or argv[0] != "validate":
        print("usage: python -m voicecore.profiles validate <config-dir>", file=sys.stderr)
        return 2
    errors = validate_dir(Path(argv[1]))
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        print(f"FAIL: {len(errors)} error(s)", file=sys.stderr)
        return 1
    print(f"OK: {Path(argv[1])} validates")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
