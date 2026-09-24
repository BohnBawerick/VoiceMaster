"""Creating a real Hermes profile from the dashboard (ticket 13).

ADR 0001: an Agent **is** a Hermes profile - a home directory under
``~/.hermes/profiles/<name>/`` with its own ``config.yaml``, ``SOUL.md``, ``.env``,
memory, sessions, skills and cron. This module writes that directory. It is not a
VoiceMaster-local imitation of one: the bytes are the ones the Hermes supervisor's
own discovery pass consumes, at the path it scans.

The contract is ticket 12's, and it is not ours to invent. It lives in
``hermes/supervisor/hermes_profile_registry.py`` (first merged into Hermes on
2026-08-17), which every scan interval classifies each directory under
``profiles/`` and writes the answer to ``gateways.json``. The parts this module is
bound by, quoted from that file:

    NAME_RE = ^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$   anything else is IGNORED
    RESERVED_PROFILE_NAMES = {"default"}         INVALID: it is the container's own home
    INCOMPLETE_MARKER = ".incomplete"            "The Agent creation wizard (ticket 13)
                                                  drops it, then removes it as the last step."
    CONFIG_NAME = "config.yaml"                  no config.yaml yet => INCOMPLETE
    a profile carrying API_SERVER_PORT in .env   must not name a reserved namespace port

``INCOMPLETE`` and ``INVALID`` are never started and never routed to; they are
reported in the registry and the log. That is the whole abandonment guarantee on
the Hermes side, and this module is the half that keeps its end: a directory this
module is still writing carries the marker, so the worst case a crash can leave is
a profile that is reported and refused, never a half-built being that answers a
call.

WHERE IT WRITES, AND WHY THAT IS A DEPLOY QUESTION
--------------------------------------------------
``profiles/`` lives inside the ``hermes-data`` docker volume, which today is
mounted into the ``hermes`` service and NOTHING else - the dashboard has no view of
it at all. So this module refuses to guess: ``HERMES_PROFILES_DIR`` must name an
existing directory, and with it unset every creation path answers 503 naming the
one compose line that is missing. Defaulting to ``~/.hermes/profiles`` would
"succeed" by creating a directory inside the dashboard's own container that no
supervisor will ever scan - a profile only this app believes in, which is the exact
failure ticket 13's first acceptance line forbids. The repository's
``docker-compose.yml`` shows the mount.

SECRETS
-------
A profile's ``.env`` is written 0600 and is write-only from this app's point of
view: a Telegram bot token goes in and is never read back, never returned by an
endpoint, and never logged. LLM credentials are NOT written - the container
exports ``~/.hermes/.env`` into every gateway's process environment
(``entrypoint.sh``), so a new profile inherits them without this app ever handling
one.
"""
import os
import re
import shutil
from pathlib import Path

import yaml

from voicecore import hermes_gateway

# --------------------------------------------------------------------------
# Ticket 12's contract, mirrored. Changing a value here without changing it in
# hermes/supervisor/hermes_profile_registry.py breaks creation silently, which
# is why each one carries the consequence of getting it wrong.
# --------------------------------------------------------------------------

ENV_PROFILES_DIR = "HERMES_PROFILES_DIR"

#: hermes_profile_registry.NAME_RE. A directory that fails it is classified
#: IGNORED and never becomes a gateway.
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")

#: hermes_profile_registry.RESERVED_PROFILE_NAMES. `profiles/default/` would be a
#: SECOND gateway over the default profile's own home - two writers on one
#: sessions db - and readers resolve "default" to the top-level gateway anyway.
RESERVED_PROFILE_NAMES = frozenset({"default"})

#: hermes_profile_registry.INCOMPLETE_MARKER. Present => status INCOMPLETE =>
#: not started, not routed to, reported. Dropped first, removed last.
INCOMPLETE_MARKER = ".incomplete"

#: hermes_profile_registry.CONFIG_NAME. Its absence is what makes a bare mkdir
#: read as half-created rather than as a broken profile.
CONFIG_NAME = "config.yaml"

#: hermes_profile_registry.REGISTRY_BASENAME, resolved under VOICE_CONFIG_DIR -
#: the dashboard and the bridges see the writer's directory at their own mount
#: point, so the file is found from that variable, never from a hardcoded path.
REGISTRY_BASENAME = hermes_gateway.REGISTRY_BASENAME

#: Subdirectories `hermes profile create` lays down. Empty is correct: sessions,
#: memory and cron are the profile's own and start empty; skills are copied only
#: when the operator asks to inherit them.
PROFILE_SUBDIRS = ("sessions", "skills", "memory", "cron")

#: The intersection of the two naming rules a created Agent has to satisfy at
#: once: voicecore's agent id (`profiles._ID_RE`, lowercase, dots allowed) and
#: the Hermes profile name (mixed case, no dots). One Agent is one name, so the
#: name has to be legal in both worlds - lowercase, no dots.
AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

#: What the wizard preselects. Realtime on OpenAI is the only lane the repo
#: treats as proven (voicecore.profiles.IMPLEMENTED_REALTIME_PROVIDER); cascade
#: and every other provider stay selectable with honest labels (VC7), under
#: Advanced, which is ticket 14.
PROVEN_PIPELINE = "realtime"
PROVEN_REALTIME_PROVIDER = "openai-gpt-realtime"

#: The shape @BotFather issues: <bot_id>:<auth_token> - a run of digits, a colon,
#: then 35 characters of letters, digits, hyphen and underscore. Checked
#: STRUCTURALLY and never against Telegram: the create path makes no network call,
#: and a live check would turn creating an Agent into something that can fail
#: because a third party is down.
TELEGRAM_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{35}$")

#: Offered as memory homes. A dedicated Hindsight BANK is deliberately not
#: offered: it has to be provisioned in the Hindsight deployment first, so a dropdown here would
#: promise something this app cannot deliver.
MEMORY_MODES = ("shared", "private")

DEFAULT_TOOL_PROFILES = ("coding", "general", "none")


class ProfileCreateError(RuntimeError):
    """A refusal with a human-readable reason. Never carries a secret."""

    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# Where the profiles live (or why they do not)
# --------------------------------------------------------------------------

DEPLOY_HINT = (
    "The dashboard cannot see the Hermes profiles directory. It lives in the "
    "Hermes data volume, which is usually mounted into the Hermes container "
    "only. Give the `voice-control` service a read-write view of it and set "
    "HERMES_PROFILES_DIR to that path in the deployment's docker-compose.yml, then "
    "redeploy. "
    "The voice-control service in the repository's docker-compose.yml shows the mount."
)


def profiles_dir(env=None) -> "Path | None":
    """The configured profiles directory, or None when it is not configured.

    Deliberately has NO default. A default would be a path inside this
    container that no Hermes supervisor scans, so creation would report success
    and produce a profile that does not exist as far as the phone line is
    concerned.
    """
    env = os.environ if env is None else env
    raw = (env.get(ENV_PROFILES_DIR) or "").strip()
    return Path(raw) if raw else None


def availability(env=None) -> dict:
    """Can this dashboard create a real profile right now, and if not, why not."""
    directory = profiles_dir(env)
    if directory is None:
        return {"available": False, "profiles_dir": None,
                "reason": f"{ENV_PROFILES_DIR} is not set in this service's "
                          "environment.",
                "deploy_hint": DEPLOY_HINT}
    if not directory.is_dir():
        return {"available": False, "profiles_dir": str(directory),
                "reason": f"{ENV_PROFILES_DIR}={directory} is not a directory that "
                          "exists in this container.",
                "deploy_hint": DEPLOY_HINT}
    if not os.access(directory, os.W_OK | os.X_OK):
        return {"available": False, "profiles_dir": str(directory),
                "reason": f"{directory} is not writable by this service (a "
                          "read-only mount creates nothing).",
                "deploy_hint": DEPLOY_HINT}
    return {"available": True, "profiles_dir": str(directory),
            "reason": None, "deploy_hint": None}


def require_dir(env=None) -> Path:
    state = availability(env)
    if not state["available"]:
        raise ProfileCreateError(f"{state['reason']} {state['deploy_hint']}", status=503)
    return Path(state["profiles_dir"])


# --------------------------------------------------------------------------
# Reading what already exists
# --------------------------------------------------------------------------

def read_gateway_registry(config_dir: Path) -> dict:
    """``gateways.json`` as the Hermes side last wrote it, or {} when absent. The
    reader is voicecore's (VC24): the bridges route by this file, so this screen and
    the phone line cannot read it two different ways."""
    return hermes_gateway.read_gateway_registry(config_dir)


_gateway_entry = hermes_gateway.registry_entry


def list_profiles(env=None, config_dir=None) -> list:
    """Every profile directory on disk, with the registry's verdict where there
    is one. Never raises: an unreadable entry is reported, not hidden."""
    directory = profiles_dir(env)
    if directory is None or not directory.is_dir():
        return []
    registry = read_gateway_registry(config_dir) if config_dir is not None else {}
    rows = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    for name in names:
        path = directory / name
        if name.startswith(".") or not PROFILE_NAME_RE.match(name):
            continue
        try:
            is_dir = path.is_dir()
        except OSError:
            continue
        if not is_dir:
            continue
        incomplete = (path / INCOMPLETE_MARKER).exists()
        has_config = (path / CONFIG_NAME).is_file()
        entry = _gateway_entry(registry, name)
        rows.append({
            "name": name,
            # What the Hermes side would make of it, computed the same way
            # _inspect does, so this screen and the registry cannot disagree.
            "complete": has_config and not incomplete,
            "incomplete": incomplete or not has_config,
            "gateway_status": entry.get("status"),
            "gateway_url": entry.get("gateway_url"),
            "gateway_error": entry.get("error"),
        })
    return rows


def read_soul(name: str, env=None) -> str:
    directory = profiles_dir(env)
    if directory is None:
        return ""
    try:
        return (directory / name / "SOUL.md").read_text()
    except OSError:
        return ""


def read_profile_config(name: str, env=None) -> dict:
    """A profile's ``config.yaml``, or {}. Used to show what inheritance takes."""
    directory = profiles_dir(env)
    if directory is None:
        return {}
    try:
        doc = yaml.safe_load((directory / name / CONFIG_NAME).read_text())
    except (OSError, yaml.YAMLError):
        return {}
    return doc if isinstance(doc, dict) else {}


def inheritable(name: str, env=None) -> dict:
    """What "inherit from this one" would actually take, secrets excluded.

    Nothing here is read from a ``.env``: the answer names what the new profile
    would think with and reach for, not what it would authenticate as.
    """
    config = read_profile_config(name, env)
    model = config.get("model") if isinstance(config.get("model"), dict) else {}
    tools = config.get("tools") if isinstance(config.get("tools"), dict) else {}
    identity = config.get("identity") if isinstance(config.get("identity"), dict) else {}
    telegram = config.get("telegram") if isinstance(config.get("telegram"), dict) else {}
    servers = config.get("mcp_servers")
    server_names = sorted(servers) if isinstance(servers, dict) else []
    directory = profiles_dir(env)
    skills = []
    if directory is not None:
        try:
            skills = sorted(p.name for p in (directory / name / "skills").iterdir())
        except OSError:
            skills = []
    return {
        "profile": name,
        "exists": bool(config) or name in {row["name"] for row in list_profiles(env)},
        "identity_name": identity.get("name"),
        "model": model.get("default"),
        "model_provider": model.get("provider"),
        "tools_profile": tools.get("profile"),
        "mcp_servers": server_names,
        "memory_shared": "hindsight" in server_names,
        "telegram_enabled": bool(telegram.get("enabled")),
        "soul": read_soul(name, env),
        "skills": skills,
    }


# --------------------------------------------------------------------------
# Writing one
# --------------------------------------------------------------------------

def telegram_token_problem(token) -> "str | None":
    """Why this bot token cannot be used, or None if it can.

    One lane for both refusals - absent and malformed - because they are the same
    mistake seen at two moments, and a caller that checks only one of them is the
    defect this function exists to prevent.

    A malformed token is not a security hole: the file is still 0600 and owned by
    the gateway's user. It is a failure that surfaces FAR from its cause - the
    operator learns about it when the bot never comes online, not when they typed
    it - which is the same "broken and invisible" class the rest of this module is
    written against.

    **The token is never repeated in the return value.** These strings reach an
    HTTP response and a log; echoing the value back would put a credential in both.
    """
    text = "" if token is None else str(token).strip()
    if not text:
        return ("telegram_bot_token: connecting to Telegram needs the new bot's own "
                "token from @BotFather. A profile with telegram enabled and no token "
                "of its own polls with nothing, or shares another Agent's bot")
    if not TELEGRAM_TOKEN_RE.match(text):
        return ("telegram_bot_token: that is not the shape of a Telegram bot token. "
                "@BotFather issues <bot_id>:<auth_token> - digits, a colon, then 35 "
                "characters of letters, digits, - and _. Checked structurally here, "
                "not against Telegram, so a wrong one is refused now instead of "
                "surfacing later as a bot that never comes online. The value you "
                "sent is not repeated here or written anywhere")
    return None


def validate_name(name) -> str:
    """The one name an Agent gets, legal as BOTH a voicecore agent id and a
    Hermes profile directory. Refused loudly, never sanitized into another name."""
    if not isinstance(name, str) or not AGENT_NAME_RE.match(name):
        raise ProfileCreateError(
            f"name: {name!r} is refused - an Agent's name is its Hermes profile "
            f"directory as well as its agent id, so it must match "
            f"{AGENT_NAME_RE.pattern} (lowercase, no dots: a dot is legal in an "
            "agent id and illegal in a profile name, and one Agent is one name)")
    if name.lower() in RESERVED_PROFILE_NAMES:
        raise ProfileCreateError(
            f"name: '{name}' is the container's own profile. A second gateway over "
            "that home would share its sessions and state db, so the Hermes side "
            "refuses it as invalid - pick another name")
    return name


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.chmod(path, mode)


def build_config(spec: dict, inherited: dict) -> dict:
    """The new profile's ``config.yaml``, as a document.

    Inheritance is a real starting point, not a label: the source profile's own
    config is the base and each answered question overwrites one key of it.
    """
    base = dict(inherited or {})
    # Never inherited: they belong to the profile that had them, and a copy
    # would give two beings one gateway port or one bot.
    for key in ("api_server", "gateway"):
        base.pop(key, None)

    # A new being gets its own name. Inheriting the source's `identity.name`
    # would put two Agents on the phone introducing themselves as the same
    # person, which is the one field "take everything" must not take.
    identity = dict(base.get("identity") or {})
    identity["name"] = spec.get("identity_name") or spec["name"]
    base["identity"] = identity

    if spec.get("model"):
        model = dict(base.get("model") or {})
        model["default"] = spec["model"]
        if spec.get("model_provider"):
            model["provider"] = spec["model_provider"]
        base["model"] = model

    tools_profile = spec.get("tools_profile")
    if tools_profile:
        if tools_profile == "none":
            base.pop("tools", None)
        else:
            tools = dict(base.get("tools") or {})
            tools["profile"] = tools_profile
            base["tools"] = tools

    # Memory. "shared" keeps the inherited Hindsight MCP server, which is what
    # makes the new being able to consult the same banks; "private" drops it, so
    # it remembers only in its own profile-local memory/ and sessions/.
    servers = dict(base.get("mcp_servers") or {})
    if spec.get("memory") == "private":
        servers.pop("hindsight", None)
    if servers:
        base["mcp_servers"] = servers
    else:
        base.pop("mcp_servers", None)

    telegram = dict(base.get("telegram") or {})
    if spec.get("telegram_connect"):
        telegram["enabled"] = True
        telegram["dm_policy"] = spec.get("telegram_dm_policy") or "pairing"
    else:
        # An inherited `telegram.enabled: true` with no token of its own would
        # make the new gateway poll with nothing, or worse, share a bot.
        telegram["enabled"] = False
    base["telegram"] = telegram
    base.pop("TELEGRAM_HOME_CHANNEL", None)

    return base


def build_soul(spec: dict, inherited_soul: str) -> str:
    soul = spec.get("soul")
    if isinstance(soul, str) and soul.strip():
        return soul.rstrip() + "\n"
    if inherited_soul.strip():
        return inherited_soul
    return f"# {spec.get('identity_name') or spec['name']}\n"


def create_profile(spec: dict, env=None, on_staged=None) -> dict:
    """Create one real Hermes profile directory. All of it, or none of it.

    The order is the whole abandonment guarantee, and it is ticket 12's:

    1. ``mkdir`` the real directory. It either claims the name or raises
       FileExistsError - there is no check-then-create window for two wizards to
       race through.
    2. Write ``.incomplete`` immediately. From here the Hermes side classifies it
       INCOMPLETE: reported in ``gateways.json``, never started, never routed to.
       Between 1 and 2 there is no ``config.yaml`` either, which the same code
       reads as INCOMPLETE for the same reason - so at no instant is a
       half-written profile eligible to answer a call.
    3. Write the contents.
    4. Run ``on_staged`` - the caller's chance to write the other half of the
       Agent (its voice-config document) while the profile is still marked
       incomplete, so one guarantee covers both files.
    5. Remove ``.incomplete`` LAST. That single unlink is the moment the profile
       becomes real, and it is atomic.

    Any failure in 2-4 removes the whole tree. If even that fails, the marker is
    still there, so the worst case is a reported, refused directory rather than
    a broken being.
    """
    directory = require_dir(env)
    name = validate_name(spec.get("name"))
    path = directory / name

    inherited_config = {}
    inherited_soul = ""
    source = spec.get("inherit_from")
    if source:
        validate_name(source)
        if not (directory / source).is_dir():
            raise ProfileCreateError(
                f"inherit_from: there is no Hermes profile '{source}' in "
                f"{directory} to inherit from")
        inherited_config = read_profile_config(source, env)
        inherited_soul = read_soul(source, env)

    try:
        os.mkdir(path)
    except FileExistsError:
        raise ProfileCreateError(
            f"name: a Hermes profile directory '{name}' already exists. Creating an "
            "Agent creates a being; it never writes into an existing one",
            status=409) from None
    except OSError as exc:
        raise ProfileCreateError(
            f"could not create {path}: {exc}. {DEPLOY_HINT}", status=503) from None

    marker = path / INCOMPLETE_MARKER
    try:
        _write(marker, f"created by the Voice Control agent wizard for '{name}'\n")
        for sub in PROFILE_SUBDIRS:
            (path / sub).mkdir(exist_ok=True)
        if source and spec.get("inherit_skills"):
            _copy_skills(directory / source / "skills", path / "skills")
        _write(path / "SOUL.md", build_soul(spec, inherited_soul))
        config = build_config(spec, inherited_config)
        _write(path / CONFIG_NAME,
               yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
        token = spec.get("telegram_bot_token")
        if spec.get("telegram_connect") and token:
            # 0600, write-only from this app: the token never comes back out of
            # here through any endpoint, and is never logged.
            _write(path / ".env", f"TELEGRAM_BOT_TOKEN={token}\n", mode=0o600)
        _match_ownership(path, directory)
        if on_staged is not None:
            on_staged()
        # Last, and atomic: this unlink is what makes the profile real.
        os.unlink(marker)
    except Exception as exc:  # noqa: BLE001 - every failure leaves nothing behind
        _abandon(path)
        if isinstance(exc, ProfileCreateError):
            raise
        raise ProfileCreateError(
            f"creating the profile at {path} failed and it was removed: {exc}",
            status=500) from exc

    return {"name": name, "path": str(path),
            "inherited_from": source or None,
            "telegram_connected": bool(spec.get("telegram_connect")
                                       and spec.get("telegram_bot_token"))}


def _owner_of(directory: Path) -> "tuple[int, int]":
    """(uid, gid) of the profiles directory - i.e. of the user the Hermes
    gateway runs as. Its own function so the mismatch case is reachable in a
    test on a machine where everything is one uid."""
    stat_result = directory.stat()
    return stat_result.st_uid, stat_result.st_gid


def _match_ownership(path: Path, directory: Path) -> None:
    """Give everything just created to whoever owns the profiles directory.

    That user is, by construction, the one the Hermes gateway runs as - and the
    gateway writes sessions and a state db INTO the profile on every message. On
    the real deploy the two containers do not agree about who that is: the
    dashboard image sets no ``USER`` and runs as root, while the agent image runs
    as ``pn``. A profile created without this is root-owned and unusable by the
    process that is supposed to be it: a broken being made by a successful click,
    which is the failure class this whole ticket is written against.

    Derived from the directory rather than configured, so it is right on any
    deploy and cannot drift from one. A failure here propagates: the caller
    abandons the profile rather than leaving one the gateway cannot run.
    """
    uid, gid = _owner_of(directory)
    if (uid, gid) == (os.getuid(), os.getgid()):
        return  # already ours - the ordinary single-user case, including tests
    for target in [path, *sorted(path.rglob("*"))]:
        os.chown(target, uid, gid)


def _copy_skills(src: Path, dest: Path) -> None:
    try:
        entries = sorted(src.iterdir())
    except OSError:
        return
    for entry in entries:
        target = dest / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)


def _abandon(path: Path) -> None:
    """Remove a profile this module was in the middle of writing.

    The order is the point, and it is not tidiness. ``.incomplete`` is already
    on disk (step 2 writes it before any content), so while this runs the Hermes
    side already refuses the directory. What that marker does NOT survive is a
    recursive delete that fails HALFWAY: ``rmtree`` walks the directory in
    whatever order the filesystem gives it, so it can remove the marker and then
    fail on something else - leaving a ``config.yaml`` behind with nothing
    marking it, which is precisely a startable profile.

    So ``config.yaml`` goes first, on its own. Its absence is the same
    "INCOMPLETE, never started" verdict as the marker
    (``hermes_profile_registry._inspect``), and it does not depend on the delete
    below getting anywhere.
    """
    try:
        os.unlink(path / CONFIG_NAME)
    except OSError:
        pass
    try:
        shutil.rmtree(path)
    except OSError:
        # Reported and refused beats silently partial: the marker, the missing
        # config, or both are still there for the next discovery pass to see.
        pass


# --------------------------------------------------------------------------
# The voice half of an Agent
# --------------------------------------------------------------------------

def build_agent_doc(spec: dict) -> dict:
    """The voice-config document that makes the new profile reachable on an
    Outlet. The profile is the being; this is the voice it gets (CONTEXT.md).

    Validated by ``voicecore.profiles.validate_profile`` before anything is
    written - the same validator the bridges resolve with, so a document this
    app accepts is one a call path accepts.
    """
    doc = {
        "id": spec["name"],
        "description": spec.get("description") or "",
        "enabled": True,
        "hermes_profile": spec["name"],
        "direction": "both",
        "pipeline": spec.get("pipeline") or PROVEN_PIPELINE,
    }
    providers = {k: v for k, v in (spec.get("providers") or {}).items() if v}
    if not providers and doc["pipeline"] == PROVEN_PIPELINE:
        providers = {"realtime": PROVEN_REALTIME_PROVIDER}
    doc["providers"] = providers
    knobs = {k: v for k, v in (spec.get("knobs") or {}).items() if v not in (None, "")}
    if knobs:
        doc["knobs"] = knobs
    if spec.get("persona"):
        doc["persona"] = spec["persona"]
    return doc


def temp_marker_path(path: Path) -> Path:
    """Exposed for the tests that prove the marker is what the Hermes side sees."""
    return Path(path) / INCOMPLETE_MARKER


def profile_is_startable(path: Path) -> bool:
    """The Hermes side's own eligibility test, in one line: a directory is
    startable only when it has a config.yaml and no ``.incomplete`` marker
    (hermes_profile_registry._inspect). Used by the tests that abandon a
    creation halfway and assert nothing startable was left behind."""
    path = Path(path)
    if not path.is_dir():
        return False
    if (path / INCOMPLETE_MARKER).exists():
        return False
    return (path / CONFIG_NAME).is_file()


__all__ = [
    "ENV_PROFILES_DIR", "PROFILE_NAME_RE", "AGENT_NAME_RE", "INCOMPLETE_MARKER",
    "CONFIG_NAME", "REGISTRY_BASENAME", "RESERVED_PROFILE_NAMES", "MEMORY_MODES",
    "PROVEN_PIPELINE", "PROVEN_REALTIME_PROVIDER", "DEFAULT_TOOL_PROFILES",
    "ProfileCreateError", "availability", "profiles_dir", "require_dir",
    "list_profiles", "inheritable", "read_profile_config", "read_soul",
    "read_gateway_registry", "validate_name", "telegram_token_problem",
    "TELEGRAM_TOKEN_RE", "build_config", "build_soul",
    "build_agent_doc", "create_profile", "profile_is_startable",
    "temp_marker_path", "DEPLOY_HINT",
]
