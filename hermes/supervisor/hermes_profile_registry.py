#!/usr/bin/env python3
"""Dynamic Hermes profile discovery and the gateway registry file.

Two halves of one contract:

WRITER (runs inside hermes-agent, driven by hermes-supervisor.sh)
    Scans ``${HERMES_HOME}/profiles/`` and decides which profiles are startable,
    which port each gateway should own, and which of those are already running.
    The answer is written atomically to a JSON registry file.

READER (voice bridges, dashboard, anything that needs to reach a profile)
    ``gateway_url_for_profile()`` resolves a profile name to a base URL by reading
    that registry file, falling back to the legacy ``HERMES_PROFILE_GATEWAY_URLS``
    environment variable when the registry is missing. The two halves may see the
    same directory at two different mount points (containers), so the reader locates
    the file via ``default_registry_path()`` rather than a single hardcoded path.

Design rules this file exists to enforce:

*   Discovery is by *looking at the directory*, never by reading a comma-separated
    environment variable. Creating a profile directory is the whole act of
    creating a profile.
*   The DEFAULT profile deterministically owns the shared API port (18789). No
    discovered profile may ever claim it, so nothing can race the process that
    answers the phone. The same protection covers every OTHER port that may be
    listening in the same network namespace (``NAMESPACE_PORTS``): when the
    VoiceMaster bridges share a host or container namespace with Hermes, the phone
    bridge's 3336 is exactly as stealable as 18789 and losing it kills the inbound
    number.
*   A broken or half-created profile is isolated and *reported*, never fatal. Every
    failure mode here downgrades one profile to a non-running status with an
    ``error`` string in the registry; it never raises past ``reconcile()`` and never
    changes what the other profiles do. "Reported" includes entries that are not
    profiles at all: anything under ``profiles/`` that holds a ``config.yaml``, or
    that is a dangling pointer, reaches the registry and the log even though it can
    never start. Broken is never invisible.
*   Absence degrades to stock behaviour. No profiles directory or no readable
    profiles gives an empty profile set, which is exactly the single-default-gateway
    setup. No PyYAML degrades *validation* only: profiles are still discovered,
    and block-style ``api_server.port`` declarations are still read (textually) so
    the reserved-port rule holds for standard configs without PyYAML.

The registry is deliberately a *file*, not an environment variable, because an
environment variable can only change by restarting the process that answers the
phone, and that drops live calls.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
import time

# --- Paths and pools -------------------------------------------------------

#: Hermes's home directory. Stock hermes-agent uses ``HERMES_HOME`` and defaults
#: to ``~/.hermes``.
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")

DEFAULT_PROFILES_DIR = os.path.join(HERMES_HOME, "profiles")
# The WRITER's path. Share this directory with the VoiceMaster services (they
# read ${VOICE_CONFIG_DIR}/gateways/gateways.json), or override it with
# HERMES_GATEWAY_REGISTRY. If nothing reads it, it is harmless: the readers fall
# back to the legacy env var.
DEFAULT_REGISTRY_PATH = os.path.join(HERMES_HOME, "voice-config", "gateways", "gateways.json")

# The READER's path is NOT necessarily the writer's path. In the reference
# container deployment every bridge mounts the same host directory at
# /app/voice-config and is given VOICE_CONFIG_DIR, so the registry is at
# ${VOICE_CONFIG_DIR}/gateways/gateways.json there. Deriving it from that variable
# is what lets a bridge find the file with no extra configuration, and so without
# a restart that bounces the phone line.
REGISTRY_BASENAME = os.path.join("gateways", "gateways.json")

# Auto-assigned gateway ports live above the default's 18789. The pool starts
# at 18790, and a sticky assignment keeps a profile on the port it first got.
PORT_POOL_START = 18790
PORT_POOL_END = 18849

# The shared API port the default profile owns, and the webui port. Both are
# reserved against discovery; overridable from the environment (API_SERVER_PORT,
# HERMES_WEBUI_PORT) because the deployment is what actually sets them.
DEFAULT_API_PORT = 18789
DEFAULT_WEBUI_PORT = 8787

#: The default set of ports a discovered profile may never take, and who owns
#: each. These are the default ports of Hermes itself and of the VoiceMaster
#: services. When those services share one network namespace with the Hermes
#: gateways (same host, or containers joined to one namespace), there is one flat
#: port space: a discovered profile that binds the phone bridge's port takes the
#: inbound phone number down the next time the bridge restarts. Reserving a port
#: nothing listens on costs nothing, so the defaults stay reserved everywhere.
#:
#: If your deployment runs anything else in that namespace (sshd, a reverse
#: proxy, a bridge on a non-default port), add its port at runtime with
#: ``HERMES_RESERVED_PORTS`` (comma separated).
NAMESPACE_PORTS = {
    3336: "the VoiceMaster phone bridge",
    3338: "the VoiceMaster Talk voice bridge",
    3737: "the VoiceMaster dashboard",
    8787: "hermes-webui",
    9119: "hermes dashboard (on-demand, loopback)",
    18789: "the default profile's gateway API",
}

#: The container's own profile. A ``profiles/default/`` directory would be started
#: as a SECOND gateway over the default profile's home - same sessions, same state
#: db, two writers - while readers still resolve "default" to the top-level gateway,
#: so the second process is unreachable as well as unsafe.
RESERVED_PROFILE_NAMES = frozenset(["default"])

# Hermes profile naming convention: [a-zA-Z0-9_-]+. Anything else on disk under
# profiles/ is somebody's scratch directory, not a profile.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")

# A directory carrying this marker is being written right now. The Agent creation
# wizard drops it, then removes it as the last step.
INCOMPLETE_MARKER = ".incomplete"

# A profile is "complete" once it has a config.yaml. `hermes profile create` writes
# one; a bare `mkdir` does not.
CONFIG_NAME = "config.yaml"

REGISTRY_VERSION = 1

# --- Statuses --------------------------------------------------------------

OK = "ok"                  # eligible, gateway should be running
INCOMPLETE = "incomplete"  # half-created, retried on the next scan, not an error
INVALID = "invalid"        # present but unusable (bad YAML, bad port), reported
CONFLICT = "conflict"      # wants a port somebody else deterministically owns
IGNORED = "ignored"        # not a profile at all (bad name, not a directory)

#: Statuses that mean "this profile is not reachable". Readers must fail honestly
#: rather than fall through to the default profile's backend.
NOT_RUNNABLE = (INCOMPLETE, INVALID, CONFLICT, IGNORED)


class ProfileRecord:
    """One profile's discovered state. Never raises; carries its own error text."""

    def __init__(self, name, path, status, port=None, host=None, url=None,
                 error=None, inject=None, explicit_port=False, reportable=True):
        self.name = name
        self.path = path
        self.status = status
        self.port = port
        self.host = host
        self.url = url
        self.error = error
        # Environment variables the supervisor must inject when spawning this
        # gateway, because the profile's own .env does not supply them.
        self.inject = list(inject or [])
        self.explicit_port = explicit_port
        # Whether this entry is worth a human's attention. Only ever False for
        # IGNORED: `profiles/README.md` is noise, but `profiles/My Agent/` with a
        # config.yaml in it is somebody's profile that will never start, and
        # dropping THAT silently is the exact broken-and-invisible failure this
        # whole file exists to prevent.
        self.reportable = reportable

    def to_json(self):
        return {
            "status": self.status,
            "gateway_url": self.url,
            "port": self.port,
            "host": self.host,
            "path": self.path,
            "error": self.error,
        }

    def __repr__(self):  # pragma: no cover - debugging aid
        return "ProfileRecord(%r, %s, port=%r, error=%r)" % (
            self.name, self.status, self.port, self.error)


# --- .env parsing ----------------------------------------------------------


def parse_env_file(path):
    """Parse a dotenv file into a dict. Unparseable lines are skipped, not fatal.

    A profile's .env is written by hand often enough that one stray line must not
    take the profile (let alone the container) out.
    """
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except (OSError, UnicodeError):
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def have_yaml():
    """Whether PyYAML is importable. Its absence degrades validation, not safety."""
    try:
        import yaml  # noqa: F401,PLC0415 - optional dependency, checked at call time
    except Exception:
        return False
    return True


#: A top-level ``api_server:`` block, and a ``port:`` one level inside it. Used
#: ONLY when PyYAML is missing (see ``_scan_api_server_port``); deliberately narrow
#: so it cannot invent a port that is not really declared.
_API_BLOCK_RE = re.compile(r"^api_server\s*:\s*(?:#.*)?$")
_PORT_LINE_RE = re.compile(r"^[ \t]+port\s*:\s*[\"']?(\d{1,5})[\"']?\s*(?:#.*)?$")


def _scan_api_server_port(path):
    """Best-effort ``api_server.port`` from config.yaml WITHOUT a YAML parser.

    Without this, a container whose venv lost PyYAML stops seeing config.yaml-declared
    ports at all - including a declared 18789, the one port nothing may ever claim.
    The guarantee in this file's docstring is unconditional, so the check has to be
    too. Returns the port as a string, or ``None``.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    in_block = False
    base_indent = None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[:1].isspace():
            in_block = bool(_API_BLOCK_RE.match(line))
            base_indent = None
            continue
        if not in_block:
            continue
        indent = len(line) - len(line.lstrip())
        if base_indent is None:
            base_indent = indent
        elif indent != base_indent:
            # A deeper mapping (api_server.tls.port) is not api_server.port, and a
            # shallower one has left the block. Neither is ours.
            continue
        match = _PORT_LINE_RE.match(line)
        if match:
            return match.group(1)
    return None


#: Maximum size for config.yaml (1 MB).
# 1 MB is orders of magnitude larger than any plausible Hermes config.yaml
# (typically < 10 KB), but small enough that PyYAML safe_load finishes in < 1ms
# without CPU or memory stalls on oversized files.
MAX_CONFIG_SIZE = 1024 * 1024


def load_config_yaml(path):
    """Return ``(mapping, error)``.

    ``(None, None)`` means "could not validate" (no PyYAML) and is deliberately
    treated as valid: a missing parser must not condemn every profile on the box.
    Without PyYAML the file is still scanned textually for ``api_server.port``, so
    the reserved-port guarantee holds even in the degraded mode.
    ``(None, "...")`` means the file is genuinely broken.
    """
    try:
        st = os.stat(path)
        if st.st_size > MAX_CONFIG_SIZE:
            return None, "config.yaml exceeds maximum size limit (%d bytes, max %d bytes)" % (
                st.st_size, MAX_CONFIG_SIZE
            )
    except OSError as exc:
        return None, "config.yaml unreadable: %s" % exc

    if not have_yaml():
        port = _scan_api_server_port(path)
        if port is None:
            return None, None
        return {"api_server": {"port": port}}, None
    import yaml  # noqa: PLC0415 - proven importable by have_yaml()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except OSError as exc:
        return None, "config.yaml unreadable: %s" % exc
    except Exception as exc:  # yaml.YAMLError and anything a custom loader raises
        first = str(exc).strip().splitlines()
        return None, "config.yaml is not valid YAML: %s" % (first[0] if first else exc)
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, "config.yaml must be a mapping, got %s" % type(data).__name__
    return data, None


def _coerce_port(value, source):
    """Return ``(port, error)``. Ports are the one thing we refuse to guess about."""
    if value is None or value == "":
        return None, None
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return None, "%s is not an integer: %r" % (source, value)
    if not (1 <= port <= 65535):
        return None, "%s out of range: %d" % (source, port)
    return port, None


def _url_for(host, port):
    """Base URL a *sibling container in the same network namespace* should call."""
    h = (host or "").strip()
    if h in ("", "0.0.0.0", "127.0.0.1", "localhost", "::", "[::]"):
        return "http://127.0.0.1:%d" % port
    return "http://%s:%d" % (h, port)


# --- Discovery -------------------------------------------------------------


def _list_candidates(profiles_dir):
    try:
        names = sorted(os.listdir(profiles_dir))
    except OSError:
        # No profiles directory at all is the normal single-profile container.
        return []
    return names


def _holds_a_profile(path):
    """Does this entry look like somebody meant it to be a profile?

    Used to decide whether an IGNORED entry is noise (``profiles/README.md``) or
    news (``profiles/My Agent/config.yaml`` - a real profile that will never start).
    """
    try:
        return os.path.isfile(os.path.join(path, CONFIG_NAME))
    except (OSError, ValueError):
        return False


def _inspect(name, profiles_dir):
    """Classify one directory entry. Returns a ProfileRecord that is never OK yet
    (port assignment happens later), or with status OK meaning "eligible"."""
    path = os.path.join(profiles_dir, name)

    if name.startswith("."):
        return ProfileRecord(name, path, IGNORED, error="dotfile entry",
                             reportable=_holds_a_profile(path))
    if not NAME_RE.match(name):
        return ProfileRecord(name, path, IGNORED,
                             error="name does not match [A-Za-z0-9][A-Za-z0-9_-]* "
                                   "- rename the directory",
                             reportable=_holds_a_profile(path))
    if name.lower() in RESERVED_PROFILE_NAMES:
        # Reported, never started: two gateways over one profile home would share
        # its sessions and state db, and readers resolve "default" to the top-level
        # gateway anyway, so the second process is unreachable as well as unsafe.
        return ProfileRecord(name, path, INVALID,
                             error="'%s' is the container's own profile - a second "
                                   "gateway would share its home; rename the directory"
                                   % name)
    try:
        st = os.stat(path)
    except OSError as exc:
        # A bad pointer under profiles/ (broken symlink, vanished mid-scan) is
        # always news: it is the same class of dangling reference that takes a call
        # path down with nothing visible in the interface.
        return ProfileRecord(name, path, IGNORED, error="unreadable: %s" % exc)
    if not stat.S_ISDIR(st.st_mode):
        # The name is profile-shaped, so something is occupying a name a profile
        # could have used. Worth saying out loud.
        return ProfileRecord(name, path, IGNORED, error="not a directory")
    if not os.access(path, os.R_OK | os.X_OK):
        return ProfileRecord(name, path, INVALID, error="directory not readable")

    if os.path.exists(os.path.join(path, INCOMPLETE_MARKER)):
        return ProfileRecord(name, path, INCOMPLETE,
                             error="%s marker present" % INCOMPLETE_MARKER)

    config_path = os.path.join(path, CONFIG_NAME)
    if not os.path.isfile(config_path):
        return ProfileRecord(name, path, INCOMPLETE, error="no %s yet" % CONFIG_NAME)

    config, err = load_config_yaml(config_path)
    if err:
        return ProfileRecord(name, path, INVALID, error=err)

    env = parse_env_file(os.path.join(path, ".env"))

    port, perr = _coerce_port(env.get("API_SERVER_PORT"), "API_SERVER_PORT in .env")
    if perr:
        return ProfileRecord(name, path, INVALID, error=perr)

    explicit = port is not None
    host = env.get("API_SERVER_HOST") or None

    if port is None and isinstance(config, dict):
        api = config.get("api_server")
        if isinstance(api, dict):
            port, perr = _coerce_port(api.get("port"), "api_server.port in config.yaml")
            if perr:
                return ProfileRecord(name, path, INVALID, error=perr)
            explicit = port is not None
            if host is None and api.get("host"):
                host = str(api["host"])

    # Which API server variables this gateway needs injected. The supervisor
    # ALWAYS unsets the inherited ones first (that is what stops extra profiles
    # racing the default for 18789); anything the profile's own .env supplies is
    # left to .env, anything it does not is injected from the resolved values.
    inject = []
    if "API_SERVER_PORT" not in env:
        inject.append("PORT")
    if "API_SERVER_HOST" not in env:
        inject.append("HOST")
    if "API_SERVER_ENABLED" not in env:
        inject.append("ENABLED")
    if "API_SERVER_KEY" not in env:
        inject.append("KEY")

    rec = ProfileRecord(name, path, OK, port=port, host=host or "127.0.0.1",
                        inject=inject, explicit_port=explicit)
    return rec


def _assign_ports(records, reserved_ports, previous):
    """Give every eligible record a port, deterministically.

    Order of authority, highest first:

    1. Reserved ports: every port already listening in the shared network
       namespace (see NAMESPACE_PORTS). Nothing discovered may take one; a profile
       that asks for one is a CONFLICT and does not start. This is the rule that
       keeps the phone line's gateway - and the phone bridge itself - from ever
       being raced.
    2. A port the profile asked for explicitly (its own .env / config.yaml),
       processed in sorted-name order so two profiles asking for the same port
       resolve the same way on every boot: first name wins, the other is a
       CONFLICT and is skipped.
    3. The port this profile held in the previous registry (sticky), so restarting
       the supervisor does not renumber anybody.
    4. The next free port in the pool.
    """
    if isinstance(reserved_ports, dict):
        taken = dict(reserved_ports)
    else:
        taken = dict((p, "reserved") for p in reserved_ports)
    reserved = set(taken)

    eligible = [r for r in records if r.status == OK]

    for rec in sorted([r for r in eligible if r.explicit_port], key=lambda r: r.name):
        owner = taken.get(rec.port)
        if owner is not None:
            rec.status = CONFLICT
            if rec.port in reserved:
                rec.error = ("port %d is reserved for %s" % (rec.port, owner))
            else:
                rec.error = ("port %d already claimed by %s" % (rec.port, owner))
            rec.port = None
            rec.url = None
            continue
        taken[rec.port] = rec.name

    auto = sorted([r for r in eligible if not r.explicit_port and r.status == OK],
                  key=lambda r: r.name)

    # Sticky pass first, so adding a profile whose name sorts earlier does not
    # renumber the profiles already running. Renumbering would move a gateway a
    # bridge is mid-call against.
    fresh = []
    for rec in auto:
        want = previous.get(rec.name)
        if (isinstance(want, int) and want not in taken
                and PORT_POOL_START <= want <= PORT_POOL_END):
            rec.port = want
            taken[want] = rec.name
        else:
            fresh.append(rec)

    for rec in fresh:
        rec.port = None
        for candidate in range(PORT_POOL_START, PORT_POOL_END + 1):
            if candidate not in taken:
                rec.port = candidate
                break
        if rec.port is None:
            rec.status = CONFLICT
            rec.error = "no free port in %d-%d" % (PORT_POOL_START, PORT_POOL_END)
            continue
        taken[rec.port] = rec.name

    for rec in records:
        if rec.status == OK and rec.port:
            rec.url = _url_for(rec.host, rec.port)
        else:
            rec.url = None
    return records


def discover(profiles_dir=DEFAULT_PROFILES_DIR, reserved_ports=(), previous=None):
    """Scan ``profiles_dir`` and return ProfileRecords, ports assigned.

    Never raises. A profile that cannot be classified comes back as INVALID with
    the reason attached, and the rest of the list is unaffected.
    """
    previous = previous or {}
    records = []
    for name in _list_candidates(profiles_dir):
        try:
            records.append(_inspect(name, profiles_dir))
        except Exception as exc:  # a single bad entry must never end the scan
            records.append(ProfileRecord(
                name, os.path.join(profiles_dir, name), INVALID,
                error="discovery failed: %s: %s" % (type(exc).__name__, exc)))
    return _assign_ports(records, reserved_ports, previous)


def reserved_ports_from_env(env=None):
    """``{port: owner}`` for every port a discovered profile may not take.

    When the VoiceMaster services share one network namespace with the gateways,
    this is not just the gateway's port and the webui's: it is everything listening
    in that namespace. A profile that binds 3336 either crash-loops (the phone
    bridge already holds it) or, on the ordering that actually matters, holds it
    while the bridge restarts - and then the inbound number is dead, with the cause
    visible only in the bridge's log.
    """
    env = os.environ if env is None else env
    ports = dict(NAMESPACE_PORTS)
    api, _ = _coerce_port(env.get("API_SERVER_PORT"), "API_SERVER_PORT")
    ports[api or DEFAULT_API_PORT] = NAMESPACE_PORTS[DEFAULT_API_PORT]
    webui, _ = _coerce_port(env.get("HERMES_WEBUI_PORT"), "HERMES_WEBUI_PORT")
    ports[webui or DEFAULT_WEBUI_PORT] = NAMESPACE_PORTS[DEFAULT_WEBUI_PORT]
    # Escape hatch: any other port in the namespace can be reserved without a code
    # change.
    for item in (env.get("HERMES_RESERVED_PORTS") or "").split(","):
        extra, err = _coerce_port(item.strip(), "HERMES_RESERVED_PORTS")
        if extra and not err:
            ports.setdefault(extra, "HERMES_RESERVED_PORTS")
    return ports


# --- Registry file ---------------------------------------------------------


def default_registry_path(env=None):
    """Where the registry is, for whoever is asking.

    The writer and the readers may see the SAME directory at two different mount
    points, so a single hardcoded path can be wrong for one of them. Order:

    1. ``HERMES_GATEWAY_REGISTRY`` - an explicit answer always wins.
    2. ``${VOICE_CONFIG_DIR}/gateways/gateways.json`` - every VoiceMaster service
       is given ``VOICE_CONFIG_DIR``, and when that is the directory Hermes writes
       into, a bridge finds the registry with no extra configuration.
    3. The writer's own default path, ``${HERMES_HOME}/voice-config/...``.
    """
    env = os.environ if env is None else env
    explicit = (env.get("HERMES_GATEWAY_REGISTRY") or "").strip()
    if explicit:
        return explicit
    voice_config_dir = (env.get("VOICE_CONFIG_DIR") or "").strip()
    if voice_config_dir:
        return os.path.join(voice_config_dir, REGISTRY_BASENAME)
    return DEFAULT_REGISTRY_PATH


def read_registry(path=None, env=None):
    """Return the registry dict, or ``None`` if it is missing or unreadable.

    A torn or corrupt file reads as None so callers fall back rather than act on
    half a mapping.
    """
    path = path or default_registry_path(env)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        return None
    return data


def previous_ports(registry):
    """Name -> port map from a previous registry, used for sticky assignment."""
    out = {}
    if not registry:
        return out
    for name, entry in (registry.get("profiles") or {}).items():
        if isinstance(entry, dict) and isinstance(entry.get("port"), int):
            out[name] = entry["port"]
    return out


def build_registry(records, default_url, running=None, now=None):
    running = running or {}
    now = int(time.time()) if now is None else now
    profiles = {}
    for rec in records:
        if rec.status == IGNORED and not rec.reportable:
            continue  # somebody's scratch directory, not news
        entry = rec.to_json()
        pid = running.get(rec.name)
        if rec.status == OK:
            entry["running"] = pid is not None
            entry["pid"] = pid
            if pid is None:
                entry["status"] = "starting"
                # Not reachable yet. Readers must not route here.
                entry["gateway_url"] = None
        else:
            entry["running"] = False
            entry["pid"] = None
        profiles[rec.name] = entry
    return {
        "version": REGISTRY_VERSION,
        "generated_by": "hermes-supervisor",
        "generated_at_unix": now,
        "default": {
            "profile": "default",
            "status": OK,
            "gateway_url": default_url,
        },
        "profiles": profiles,
    }


def write_registry(path, registry):
    """Write the registry atomically. Readers never see a half-written file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".gateways-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(registry, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- Reader side (voice bridges) -------------------------------------------


def _legacy_env_map(value):
    out = {}
    for pair in (value or "").split(","):
        name, _, url = pair.strip().partition("=")
        if name.strip() and url.strip():
            out[name.strip()] = url.strip().rstrip("/")
    return out


def gateway_url_for_profile(profile, registry_path=None, env=None, default_url=None):
    """Base gateway URL for a profile name, or ``None`` if it is not reachable.

    ``None`` means the caller must fail honestly. Never silently falls back to the
    default profile's backend for a named profile: routing a call meant for one
    Agent into another Agent's gateway is worse than refusing it.

    Resolution order:

    1. The registry file this app writes (the source of truth). Located via
       ``default_registry_path()``, which derives it from ``VOICE_CONFIG_DIR`` in a
       bridge - so it needs no extra configuration there.
    2. ``HERMES_PROFILE_GATEWAY_URLS`` (the pre-discovery environment variable),
       so a bridge keeps working against an older Hermes container that does not
       write a registry yet.
    """
    env = os.environ if env is None else env
    name = (profile or "").strip() or "default"
    default_url = (default_url
                   or env.get("HERMES_GATEWAY_URL")
                   or "http://localhost:%d" % DEFAULT_API_PORT).rstrip("/")

    registry = read_registry(registry_path, env=env)
    if registry is not None:
        if name == "default":
            entry = registry.get("default") or {}
            return (entry.get("gateway_url") or default_url).rstrip("/")
        entry = (registry.get("profiles") or {}).get(name)
        if isinstance(entry, dict):
            url = entry.get("gateway_url")
            if entry.get("status") == OK and url:
                return url.rstrip("/")
            return None
        # Registry exists and does not know this profile. Fall through to the
        # legacy variable rather than declaring it dead: a bridge may have been
        # pointed at a gateway outside this container.

    legacy = _legacy_env_map(env.get("HERMES_PROFILE_GATEWAY_URLS", ""))
    if name in legacy:
        return legacy[name]
    if name == "default":
        return default_url
    return None


# --- CLI (driven by hermes-supervisor.sh) ----------------------------------


def _parse_running(value):
    """``"name:pid,name:pid"`` -> ``{name: pid}``. Junk entries are skipped."""
    out = {}
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, _, pid = item.partition(":")
        name = name.strip()
        if not name:
            continue
        try:
            out[name] = int(pid)
        except (TypeError, ValueError):
            out[name] = None
    return out


def cmd_reconcile(args):
    """Scan, write the registry, and print a plan for the supervisor.

    stdout is a machine-read TSV, one action per line:

        START<TAB>name<TAB>port<TAB>host<TAB>INJECT
        STOP<TAB>name<TAB>reason

    ``INJECT`` is a ``+``-joined subset of ``PORT HOST ENABLED KEY`` naming the API
    server variables the supervisor must export because the profile's own .env does
    not supply them. Secrets are never printed: the supervisor sources the key from
    its own environment.

    ``STOP`` covers a profile that is running but is no longer eligible: its
    directory was deleted, or its config went bad since it started. Leaving it up
    would hold a port the registry no longer advertises.

    Diagnostics go to stderr so the supervisor logs them without parsing them.
    """
    registry_path = args.registry
    previous = previous_ports(read_registry(registry_path))
    reserved = reserved_ports_from_env()
    records = discover(args.profiles_dir, reserved_ports=reserved, previous=previous)
    running = _parse_running(args.running)

    registry = build_registry(records, args.default_url, running=running)
    try:
        write_registry(registry_path, registry)
    except Exception as exc:
        # A registry we cannot write is a visibility failure, not a reason to stop
        # starting gateways.
        print("[registry] cannot write %s: %s" % (registry_path, exc), file=sys.stderr)

    if not have_yaml():
        print("[registry] PyYAML is not importable - config.yaml cannot be validated; "
              "api_server.port is still read by a text scan", file=sys.stderr)

    for rec in records:
        if rec.status in (INVALID, CONFLICT):
            print("[registry] profile %s %s: %s" % (rec.name, rec.status, rec.error),
                  file=sys.stderr)
        elif rec.status == INCOMPLETE:
            print("[registry] profile %s incomplete (%s) - will retry" % (rec.name, rec.error),
                  file=sys.stderr)
        elif rec.status == IGNORED and rec.reportable:
            # Not routable, but never silent: something under profiles/ is shaped
            # like a profile and will never start, and the only way anyone finds
            # that out is if we say so.
            print("[registry] entry %s ignored: %s" % (rec.name, rec.error),
                  file=sys.stderr)

    by_name = dict((r.name, r) for r in records)
    for name in sorted(running):
        rec = by_name.get(name)
        if rec is None:
            print("STOP\t%s\tprofile directory is gone" % name)
        elif rec.status != OK:
            print("STOP\t%s\t%s: %s" % (name, rec.status, rec.error or "no longer eligible"))

    for rec in sorted([r for r in records if r.status == OK], key=lambda r: r.name):
        if rec.name in running:
            continue
        print("START\t%s\t%d\t%s\t%s" % (rec.name, rec.port, rec.host,
                                         "+".join(rec.inject) or "-"))
    return 0


#: A registry older than this has not been refreshed by a discovery pass, which
#: means discovery is not running. Four scan intervals of headroom.
STALE_AFTER_SECONDS = 120


def cmd_doctor(args, now=None):
    """Answer one question: is profile discovery actually working right now?

    Written for the deploy. The dangerous state is a deployment that expects
    discovery running an OLD supervisor: it starts no extra gateways and logs
    nothing about it, so the only visible symptom is that this registry file is
    missing or has stopped being refreshed. That is what this command looks at.

        python3 hermes_profile_registry.py doctor --expect my-agent

    Exit status is 0 only if every check passes, so it can be used in a script.
    """
    now = int(time.time()) if now is None else now
    path = args.registry
    failures = []

    def check(passed, label, detail=""):
        print("%s  %s%s" % ("PASS" if passed else "FAIL", label,
                            (" - " + detail) if detail else ""))
        if not passed:
            failures.append(label)

    registry = read_registry(path)
    if registry is None:
        check(False, "registry readable", "nothing usable at %s" % path)
        print()
        print("The registry is missing or corrupt. The usual cause after a deploy is")
        print("that the supervisor running is not hermes-supervisor.sh with profile")
        print("discovery: it starts no extra gateways and says nothing about it.")
        print("Install hermes/supervisor/ from VoiceMaster, then restart Hermes.")
        return 1
    check(True, "registry readable", path)

    version = registry.get("version")
    check(version == REGISTRY_VERSION, "registry version",
          "found %r, expected %r" % (version, REGISTRY_VERSION))

    generated = registry.get("generated_at_unix")
    age = None if not isinstance(generated, int) else now - generated
    check(age is not None and age <= STALE_AFTER_SECONDS, "discovery is running",
          "last pass %s" % ("never" if age is None else "%ds ago (stale over %ds)"
                            % (age, STALE_AFTER_SECONDS)))

    default_url = (registry.get("default") or {}).get("gateway_url")
    check(bool(default_url), "default gateway advertised", str(default_url))

    profiles = registry.get("profiles") or {}
    for name in args.expect:
        entry = profiles.get(name)
        if not isinstance(entry, dict):
            check(False, "profile %s" % name, "not in the registry at all")
            continue
        status, running = entry.get("status"), entry.get("running")
        check(status == OK and running is True, "profile %s" % name,
              "status=%s running=%s url=%s error=%s"
              % (status, running, entry.get("gateway_url"), entry.get("error")))

    for name in sorted(profiles):
        entry = profiles[name]
        if isinstance(entry, dict) and entry.get("status") != OK:
            print("note  profile %s is %s: %s"
                  % (name, entry.get("status"), entry.get("error") or "-"))

    if failures:
        print()
        print("FAILED: %s" % ", ".join(failures))
        return 1
    print()
    print("OK: discovery is running and every expected profile is up.")
    return 0


def cmd_show(args):
    registry = read_registry(args.registry)
    if registry is None:
        print("no registry at %s" % args.registry, file=sys.stderr)
        return 1
    json.dump(registry, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def cmd_resolve(args):
    url = gateway_url_for_profile(args.profile, registry_path=args.registry)
    if not url:
        print("unresolved profile: %s" % args.profile, file=sys.stderr)
        return 1
    print(url)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profiles-dir", default=os.environ.get(
        "HERMES_PROFILES_DIR", DEFAULT_PROFILES_DIR))
    parser.add_argument("--registry", default=default_registry_path())
    parser.add_argument("--default-url", default=os.environ.get(
        "HERMES_GATEWAY_URL", "http://localhost:%d" % DEFAULT_API_PORT))
    sub = parser.add_subparsers(dest="command")

    p_rec = sub.add_parser("reconcile", help="scan, write registry, print start-plan")
    p_rec.add_argument("--running", default="", help="name:pid,name:pid of live gateways")
    p_rec.set_defaults(func=cmd_reconcile)

    sub.add_parser("show", help="print the current registry").set_defaults(func=cmd_show)

    p_doc = sub.add_parser("doctor", help="check that discovery is actually working")
    p_doc.add_argument("--expect", action="append", default=[], metavar="PROFILE",
                       help="a profile that must be ok AND running (repeatable)")
    p_doc.set_defaults(func=cmd_doctor)

    p_res = sub.add_parser("resolve", help="print the gateway URL for a profile")
    p_res.add_argument("profile")
    p_res.set_defaults(func=cmd_resolve)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
