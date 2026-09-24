"""Voice Control dashboard (s2) - FastAPI backend + the built React frontend in ``static/``.

Boot contract (frozen in .buildloop/init.sh): this module exposes ``app`` and is
launched as ``uvicorn app:app --host 0.0.0.0 --port $VOICE_DASHBOARD_PORT``.
GET /healthz answers instantly and independently of provider reachability.
Registry resolution reuses the s1 loader (``voicecore.profiles``, the same one
both bridges import):
$VOICE_CONFIG_DIR/providers.yaml when that file exists, else the canonical
services/voice-config/providers.yaml. VOICE_EVENTLOG_PATH is part of the boot
contract but carries no s2 behavior (observability is s5); it is left untouched.

Probe lifecycle: NOTHING probes at import or startup. The first
GET /api/providers primes the cache (concurrent live probes of every entry
whose secret_env is set); results are then cached until process death or a
successful POST /api/providers/refresh, which re-probes ALL probe-eligible
entries. Secrets never appear in responses or logs — probe details are
composed in probes.py from status codes/markers only.
"""
import asyncio
import json
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import hermes_profiles
import hindsight_calls
import place_call
import schedules
import scheduler as call_scheduler
import settings_catalog
import speed_dial
from voicecore import cascade_config
from voicecore import hermes_gateway
from voicecore import mission as mission_author
from voicecore import probes
from voicecore import profiles
from voicecore import recording_store

# VC24: the dashboard grades activations on behalf of the bridges that will run them, so
# it declares what BOTH bridges declare: cascade on the phone Outlet and on the Talk
# Outlet, in both directions. Without it `activation_problem` answers "pipeline cascade
# is not implemented in this bridge" for an Agent a bridge would happily run, so
# `PUT /api/active` would refuse to store an activation that works and `GET /api/active`
# would call a healthy slot broken. Inbound cascade is still refused for every Agent
# except the direct Hermes lane - that rule lives in `activation_problem`, not here.
#
# Ticket 15: the declaration used to arrive as an import side effect of `dryrun.py`,
# which only the test suite imported, so production and the tests disagreed. It belongs
# here, stated. If a bridge's own declaration changes, this one changes with it.
for _outlet in profiles.OUTLETS:
    profiles.declare_cascade_host(_outlet, ("outbound", "inbound"))

# Deepgram Aura voice catalog for the editor dropdown — every id live-verified
# (HTTP 200 on /v1/speak, 2026-07-20). Aura has no voices-list API; this is curated.
AURA_VOICES = [
    "aura-2-thalia-en", "aura-2-andromeda-en", "aura-2-apollo-en",
    "aura-2-asteria-en", "aura-2-athena-en", "aura-2-atlas-en",
    "aura-2-aurora-en", "aura-2-hermes-en", "aura-2-hyperion-en",
    "aura-2-luna-en", "aura-2-orion-en", "aura-2-zeus-en",
]

STATIC_DIR = Path(__file__).resolve().parent / "static"

# s3 preview (c20): the effective-config preview is computed by each bridge's OWN
# builder code, executed in that bridge's OWN venv (both bridges expose a flat
# ``server`` module and mode-v a flat ``config``, so they cannot both be imported into
# this process). preview_effective.py in each service dir stages the draft
# as-if-selected and captures the real builder output; this app only relays it.
SERVICES_DIR = Path(__file__).resolve().parent.parent
PREVIEW_BRIDGES = {
    "mode-c": SERVICES_DIR / "voice",
    "mode-v": SERVICES_DIR / "talk-voice-bridge",
}

_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="7" fill="#101828"/>'
    '<g fill="#7dd3fc">'
    '<rect x="7" y="12" width="3" height="8" rx="1.5"/>'
    '<rect x="12.5" y="7" width="3" height="18" rx="1.5"/>'
    '<rect x="18" y="10" width="3" height="12" rx="1.5"/>'
    '<rect x="23.5" y="13" width="3" height="6" rx="1.5"/>'
    "</g></svg>"
)


def _load_registry() -> dict:
    """Registry via the s1 loader — its resolution order + validation, verbatim.
    Raises profiles.ProfileError loudly; callers surface it, never swallow it."""
    return profiles.load_registry()


def _serialize(entry: dict, probe: probes.ProbeResult) -> dict:
    probe_obj: dict = {"detail": probe.detail}
    if probe.checked_at is not None:
        probe_obj["checked_at"] = probe.checked_at
    if probe.http_status is not None:
        probe_obj["http_status"] = probe.http_status
    # s14: the honest proven/wired label travels with every row, so a Settings
    # screen never has to invent it. Derived from voicecore's wiring facts via
    # settings_catalog - never a copy that can drift.
    label = settings_catalog.provider_label(entry)
    return {
        "id": entry["id"],
        "role": entry["role"],
        "display_name": entry["display_name"],
        "secret_env": entry["secret_env"],
        "capabilities": entry.get("capabilities") or [],
        "cost_hint": entry.get("cost_hint"),
        "latency_hint": entry.get("latency_hint"),
        # Registry knob fill-ins (model/voice/temperature/...) — the editor renders
        # these as "inherit (<value>)" placeholders so blank knobs show what actually
        # runs. Names/values only, never credentials (the registry carries none).
        "default_knobs": dict(entry.get("default_knobs") or {}),
        "status": probe.status,
        "probe": probe_obj,
        # s14: honest proven/untested + wired facts, with the evidence string.
        "proven": label["proven"],
        "wired": label["wired"],
        "evidence": label["evidence"],
    }


def _canonical_pointer_doc(pointer: dict) -> dict:
    """The one on-disk shape: outlets: {<outlet>: {inbound, outbound}}. Every
    write lands exactly this; s17 left no other shape to normalize from."""
    return {"outlets": {o: {d: pointer[o][d] for d in profiles.ACTIVE_DIRECTIONS}
                        for o in profiles.OUTLETS}}


def _agents_dir() -> Path:
    return profiles.config_dir() / "agents"


def _atomic_write_yaml(path: Path, doc: dict) -> None:
    """Every YAML mutation goes through here: temp file in the SAME directory +
    os.replace — a crash between the two leaves the old file (or nothing), never a
    truncated one, and a successful save leaves no temp litter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# The s1 id rule plus the dashboard's refusal of extension-smuggling ids, from
# the module that validates a placement — one implementation, so the Agents
# screen and the Place/Schedule paths cannot disagree about what an id is.
_safe_agent_id = place_call.safe_agent_id


def _agent_path(agent_id: str) -> Path:
    """Where a *new* Agent document is written. Existing Agents are never
    addressed this way - use ``_lookup_agent``."""
    return _agents_dir() / f"{agent_id}.yaml"


def _lookup_agent(agent_id: str) -> "list[tuple[Path, object]]":
    """Files whose document keys as this id.

    Same identity as the roster and ``profiles._scan_agents_dir``: every
    ``*.yaml`` and ``*.yml``, keyed by the ``id:`` inside (filename stem
    only when the document has no usable id). The filename is never an
    address. Unreadable files are skipped so one stray file cannot hide
    another Agent.
    """
    matches: list[tuple[Path, object]] = []
    adir = _agents_dir()
    if not adir.is_dir():
        return matches
    for path in sorted(adir.glob("*.yaml")) + sorted(adir.glob("*.yml")):
        try:
            doc = yaml.safe_load(path.read_text())
        except Exception:  # noqa: BLE001 - one bad file must not hide another
            continue
        if isinstance(doc, dict) and isinstance(doc.get("id"), str):
            key = doc["id"]
        else:
            key = path.stem
        if key == agent_id:
            matches.append((path, doc))
    return matches


def _scan_agents() -> "tuple[dict, dict, str | None]":
    """({id: doc}, {id: path}, directory warning|None) - the agents directory read
    EXACTLY the way the bridges read it, through the same ``profiles._scan_agents_dir``.

    Two silences close here, both of them older than the outlet axis:

    G1 - ``_activate`` parses the WHOLE directory to resolve any one agent, and
    refuses all of it when ANY file in it is unparseable, is not a YAML map, or
    duplicates an id - including files no slot names. One stray file therefore takes
    down every Outlet at once, while a per-slot check, which only ever looks at the
    slot's own agent, reads perfectly healthy. The warning returned here names the
    offending file and why, so the dashboard says what to go and fix.

    What "takes down" means changed with ticket 08 and the warning says so: the
    assigned configuration stops resolving for every Outlet, and each Outlet then
    either answers from its last-known-good snapshot (``voicecore/lkg.py``, loudly)
    or refuses outright if it has none. Either way nothing is serving what the
    operator assigned, which is what makes this worth a dashboard warning.

    G3 - the id a slot names is a DOCUMENT id (the ``id:`` inside the file), not a
    filename: the bridges also accept ``*.yml`` and never assume ``<id>.yaml``.
    Resolving by filename made the health check disagree with the thing it reports on
    in both directions - a false alarm on an agent stored as ``<id>.yml`` or under a
    different filename, and silence on a file whose ``id:`` does not match its name.

    A missing or empty agents directory is deliberately NOT a directory fault: every
    slot that names an agent already says so on its own card, and with no slot filled
    there is nothing broken to report.

    MUST never raise - ``GET /api/active`` is what gets read when something is wrong.
    """
    try:
        agents_dir = _agents_dir()
        if not any(agents_dir.glob("*.yaml")) and not any(agents_dir.glob("*.yml")):
            return {}, {}, None
        docs, sources = profiles._scan_agents_dir(agents_dir, context="agents/")
    except profiles.ProfileError as exc:
        # Whitespace-collapsed: a YAML parse error is several lines with a caret
        # diagram, and this lands in a one-line banner.
        return {}, {}, (
            " ".join(str(exc).split()) + " - this breaks EVERY Outlet, not only the "
            "slots naming this agent: the bridges parse the whole agents directory "
            "to resolve any one agent. Until it is fixed or removed no Outlet serves "
            "its assigned configuration - each one either answers from its "
            "last-known-good snapshot (ticket 08, logged as a fallback) or refuses "
            "outright if it has none")
    except Exception as exc:  # noqa: BLE001 - a warning, never a raised endpoint
        return {}, {}, (f"agents/: the agents directory could not be read ({exc}) - "
                        "calls may refuse to start")
    return docs, sources, None


def _slot_health_warning(aid: str, outlet: str, direction: str,
                         docs: dict, sources: dict) -> "str | None":
    """The dashboard-side visibility check for ONE stored slot (s16 addendum): a
    slot that went stale AFTER storage - the agent file removed out of band, the
    agent later disabled, or edited invalid - must be surfaced by GET /api/active,
    never silently. Returns a warning naming the canonical field path, or None.

    ``docs``/``sources`` come from ``_scan_agents`` and are keyed by document id, so
    this resolves the slot the way the bridge that serves it resolves it (G3).

    MUST never raise: GET /api/active is what the owner looks at when something is
    wrong, so it has to survive a broken configuration.
    """
    where = f"outlets.{outlet}.{direction}"
    doc = docs.get(aid)
    if doc is None:
        return (f"{where}: agent '{aid}' does not exist (no file in the agents "
                f"directory carries id: {aid}) - every call on this slot will "
                "refuse to start")
    # The REAL file this agent resolved from, never reconstructed from the id: the
    # two differ exactly in the G3 case this check was taught to resolve, and a
    # label naming a file that is not there is shown to someone hunting for the
    # file that is.
    source = sources[aid]
    try:
        registry = profiles.load_registry()
        errors = profiles.validate_profile(doc, registry, source.name)
    except profiles.ProfileError as exc:
        return (f"{where}: agent '{aid}' could not be checked against the registry "
                f"({exc})")
    except Exception as exc:  # noqa: BLE001 - a warning, never a raised endpoint
        return (f"{where}: agent '{aid}' could not be checked ({exc}) - calls on "
                "this slot may refuse to start")
    if errors:
        return (f"{where}: agent '{aid}' is invalid - calls on this slot will "
                f"refuse to start ({len(errors)} validation error(s))")
    if doc.get("enabled") is False:
        return (f"{where}: agent '{aid}' has enabled: false - calls on this slot "
                "will refuse to start while it is disabled")
    # The same activation refusal ``_validate_active_slot`` makes on the PUT side
    # (cascade is outbound-only, unimplemented realtime providers refuse). Without
    # it the dashboard refuses to CREATE a state it then cannot SEE: an agent that
    # was healthy when assigned and is later edited into cascade-on-inbound, or
    # onto an unwired provider, is a hard bridge refusal reported as no warning at
    # all. Safe to call here: the doc passed ``validate_profile`` above, which is
    # the schema-valid precondition ``activation_problem`` documents.
    try:
        problem = profiles.activation_problem(doc, f"agents/{source.name}",
                                              direction=direction, outlet=outlet)
    except Exception as exc:  # noqa: BLE001 - a warning, never a raised endpoint
        return (f"{where}: agent '{aid}' could not be activation-checked ({exc}) - "
                "calls on this slot may refuse to start")
    if problem is not None:
        return (f"{where}: agent '{aid}' cannot serve this slot - every {direction} "
                f"call on it will refuse to start: {problem}")
    return None


def _empty_slot_map():
    return {o: {d: None for d in profiles.ACTIVE_DIRECTIONS} for o in profiles.OUTLETS}


def _read_pointer():
    """({outlet: {direction: id|None}}, [warning strings], {outlet: {direction: warning|None}})

    Warnings come from three sources: the structural problems ``read_active_pointer``
    reports (non-string/bad-shape values, non-map entries); the directory-level probe
    ``_scan_agents`` (a stray file that refuses the whole agents directory, and with
    it every Outlet at once); and the per-slot health check (s16 addendum): a slot
    naming a missing, disabled or invalid agent is surfaced here with its canonical
    field path, so a configuration that went stale after storage is never both broken
    and invisible.

    Both views are computed in ONE pass, from one read, on purpose (s02): the flat
    list is what the API has always returned, and the per-slot map is what the
    Agents screen paints onto the card the fault belongs to. Two passes would be
    two chances for the banner and the card to disagree about which outlet is dead.
    Never raises."""
    try:
        pointer, problems = profiles.read_active_pointer()
    except profiles.ProfileError as exc:
        # The whole file is unreadable: nothing is attributable to a slot, so the
        # message stays page-level.
        return _empty_slot_map(), [str(exc)], _empty_slot_map()
    slots = {o: {d: problems[o][d] for d in profiles.ACTIVE_DIRECTIONS}
             for o in profiles.OUTLETS}
    # One warning per DISTINCT problem is what a human needs in the flat list,
    # while every slot it actually breaks still carries it on its own card (a
    # malformed ``outlets`` map, say, kills every slot with one message).
    warnings = list(dict.fromkeys(
        msg for o in profiles.OUTLETS
        for msg in problems[o].values() if msg))
    # The directory-level probe (G1) runs BEFORE the per-slot checks because it
    # decides whether a per-slot answer means anything: while the agents directory
    # is refused as a whole, no slot resolves, so a per-slot "does not exist" would
    # be a false attribution. Listed once in the flat view, painted on every filled
    # card, because every filled slot really is dead.
    docs, sources, dir_warning = _scan_agents()
    if dir_warning is not None:
        warnings.append(dir_warning)
    for outlet in profiles.OUTLETS:
        for direction in profiles.ACTIVE_DIRECTIONS:
            aid = pointer[outlet][direction]
            if aid is None:
                continue
            if dir_warning is not None:
                slots[outlet][direction] = dir_warning
                continue
            warning = _slot_health_warning(aid, outlet, direction, docs, sources)
            if warning is not None:
                slots[outlet][direction] = warning
                warnings.append(warning)
    return pointer, warnings, slots


def _referencing_directions(agent_id: str) -> list:
    """The slots that name this agent, as 'direction (outlet)' labels."""
    pointer, _, _ = _read_pointer()
    refs = []
    for outlet in profiles.OUTLETS:
        for d in profiles.ACTIVE_DIRECTIONS:
            if pointer[outlet][d] == agent_id:
                refs.append(f"{d} ({outlet})")
    return refs


def create_app() -> FastAPI:
    # Ticket 11: the scheduler is this app's, and it is the app's whole memory of
    # a Schedule (VC14) — Hermes may create one but is never asked to remember it.
    # It starts with the process and stops with it; everything that decides
    # whether a Call has already been placed is on the disk, not in here, so a
    # restart mid-window is an ordinary tick and not a special case.
    @asynccontextmanager
    async def lifespan(instance: FastAPI):
        # A scheduler knob that cannot work refuses the boot, the way a missing
        # required value would: an operator who set VOICE_SCHEDULE_GRACE_S=0
        # would otherwise get a dashboard that looks configured and never
        # places a Call. The message names the variable and the value.
        call_scheduler.validate_knobs()
        instance.state.scheduler = call_scheduler.Scheduler(
            transport_get=lambda: instance.state.transport)
        if call_scheduler.enabled():
            await instance.state.scheduler.start()
        try:
            yield
        finally:
            await instance.state.scheduler.stop()

    app = FastAPI(title="voice-control", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    state = app.state
    state.scheduler = None        # replaced on startup; None until the app runs

    # s6 (D7): HTTP Basic auth, enforced app-side so the raw tailnet port carries the
    # same gate as the HAProxy vhost. Active only when BOTH env vars are set (local
    # dev stays open). /healthz stays exempt — docker healthcheck + HAProxy probe.
    auth_user = os.environ.get("VOICE_DASHBOARD_USER") or ""
    auth_password = os.environ.get("VOICE_DASHBOARD_PASSWORD") or ""
    if auth_user and auth_password:
        import base64
        import secrets as _secrets

        expected = base64.b64encode(
            f"{auth_user}:{auth_password}".encode()).decode()

        @app.middleware("http")
        async def _basic_auth(request: Request, call_next):
            if request.url.path == "/healthz":
                return await call_next(request)
            header = request.headers.get("authorization") or ""
            scheme, _, credential = header.partition(" ")
            if (scheme.lower() == "basic"
                    and _secrets.compare_digest(credential.strip(), expected)):
                return await call_next(request)
            return Response(status_code=401, content="unauthorized",
                            headers={"WWW-Authenticate": 'Basic realm="voice-control"'})
    state.transport = None        # tests inject an httpx transport; None = real network
    state.cache = {}              # provider id -> ProbeResult (probed entries only)
    state.lock = asyncio.Lock()   # one probe cycle at a time

    def _eligible(registry: dict) -> list:
        return [e for e in registry.values() if probes.resolve_key(e) is not None]

    async def _prime_missing(registry: dict) -> None:
        """Cold path: probe only entries with no cached result. No-op when warm."""
        eligible = _eligible(registry)
        if all(e["id"] in state.cache for e in eligible):
            return
        async with state.lock:
            missing = [e for e in eligible if e["id"] not in state.cache]
            if missing:
                fresh = await probes.probe_batch(missing, transport=state.transport)
                state.cache = {**state.cache, **fresh}

    async def _reprobe_all(registry: dict) -> None:
        """Refresh: re-probe EVERY probe-eligible entry — never a partial subset."""
        async with state.lock:
            fresh = await probes.probe_batch(_eligible(registry),
                                             transport=state.transport)
            state.cache = fresh

    def _rows(registry: dict) -> list:
        rows = []
        for entry in registry.values():
            if probes.resolve_key(entry) is None:
                result = probes.needs_key_result(entry)
            else:
                result = state.cache.get(entry["id"]) or probes.ProbeResult(
                    "error",
                    "probe result unavailable — POST /api/providers/refresh to re-probe",
                    probes._now(), None)
            rows.append(_serialize(entry, result))
        return rows

    def _registry_error(exc: profiles.ProfileError) -> JSONResponse:
        return JSONResponse(status_code=500,
                            content={"error": "registry_error", "detail": str(exc)})

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "service": "voice-control"}

    @app.get("/api/providers")
    async def list_providers():
        try:
            registry = _load_registry()
        except profiles.ProfileError as exc:
            return _registry_error(exc)
        await _prime_missing(registry)
        return _rows(registry)

    @app.post("/api/providers/refresh")
    async def refresh_providers():
        try:
            registry = _load_registry()
        except profiles.ProfileError as exc:
            return _registry_error(exc)
        await _reprobe_all(registry)
        return _rows(registry)

    @app.get("/api/providers/{provider_id}/voices")
    async def provider_voices(provider_id: str):
        """Voice catalog for a TTS provider's editor dropdown. ElevenLabs voices come
        from the ACCOUNT via the real API (a failed/keyless fetch is `source:
        unavailable` — the editor falls back to visible free text, never a fake
        one-item list); Aura ships a curated list (each id live-verified against
        /v1/speak). `default` echoes the registry default_knobs voice."""
        try:
            registry = _load_registry()
        except profiles.ProfileError as exc:
            return _registry_error(exc)
        entry = registry.get(provider_id)
        if entry is None or entry.get("role") != "tts":
            return _json_error(404, [f"no TTS provider '{provider_id}' in the registry"])
        default = (entry.get("default_knobs") or {}).get("voice")
        if provider_id == "deepgram-aura":
            return {"source": "curated", "default": default,
                    "voices": [{"id": v, "name": v.split("-")[2].capitalize()}
                               for v in AURA_VOICES]}
        if provider_id == "elevenlabs":
            key = probes.resolve_key(entry)
            if key is None:
                return {"source": "unavailable", "default": default, "voices": None,
                        "detail": f"no {entry.get('secret_env')} in the environment — "
                                  "enter a voice id manually"}
            voices, detail = await probes.fetch_elevenlabs_voices(
                key, transport=state.transport)
            if voices is None:
                return {"source": "unavailable", "default": default, "voices": None,
                        "detail": detail}
            return {"source": "account", "default": default, "voices": voices}
        return {"source": "unavailable", "default": default, "voices": None,
                "detail": "no voice catalog wired for this provider — enter a voice "
                          "id manually"}

    # -- s3: agents CRUD + active pointer ----------------------------------------

    def _json_error(status: int, detail) -> JSONResponse:
        return JSONResponse(status_code=status, content={"detail": detail})

    def _require_agent(agent_id: str):
        """The file and document for this Agent, or an error response.

        Identity is the document ``id:``, the same key the roster and the
        bridges use. A URL id that names no document, or that names two, is
        refused - the filename is never consulted as an address.
        """
        if not _safe_agent_id(agent_id):
            return None, _json_error(404, f"no such agent: {agent_id!r}")
        matches = _lookup_agent(agent_id)
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            names = " and ".join(path.name for path, _ in matches)
            return None, _json_error(
                422, f"duplicate agent id '{agent_id}' ({names})")
        return None, _json_error(404, f"no such agent: '{agent_id}'")

    def _validate(doc, source: str) -> list:
        registry = profiles.load_registry()
        return profiles.validate_profile(doc, registry, source)

    def _held_slots(agent_id: str) -> list:
        """Every (outlet, direction) the pointer assigns to this Agent right now. A
        pointer that cannot be read holds nothing we can name, and GET /api/active is
        where that fault is reported."""
        try:
            pointer, _problems = profiles.read_active_pointer()
        except profiles.ProfileError:
            return []
        return [(o, d) for o in profiles.OUTLETS for d in profiles.ACTIVE_DIRECTIONS
                if pointer[o][d] == agent_id]

    def _slot_break_error(agent_id: str, doc: dict, source: str):
        """VC24: refuse a narrow edit that would break a slot the Agent already holds.

        The same ``activation_problem`` the Outlet assignment runs, asked for each held
        slot. The agent-type switch on the Settings page turns "set pipeline: cascade on
        the Agent answering the phone" from an obscure YAML edit into one click, so the
        two endpoints that screen writes through must not be able to do it silently.
        Deliberately NOT added to the whole-document ``PUT /api/agents/{id}``: no screen
        calls it, and services/voice-control/CLAUDE.md records why that route is left
        alone. The way through is the one the delete path already forces: unassign
        the slot, edit, assign again.
        """
        broken = []
        for outlet, direction in _held_slots(agent_id):
            problem = profiles.activation_problem(
                doc, f"agents/{source}", direction=direction, outlet=outlet)
            if problem is not None:
                broken.append(f"outlets.{outlet}.{direction}: every {direction} call "
                              f"would refuse to start: {problem}")
        if not broken:
            return None
        return _json_error(409, [
            f"agent '{agent_id}' is assigned to the slot(s) below, and this change "
            "would break them. Unassign it on the Agents screen first, or pick a "
            "setting those slots can run."] + broken)

    def _summary(agent_id: str, doc, errors: list, pointer: dict) -> dict:
        get = doc.get if isinstance(doc, dict) else (lambda *_: None)
        return {
            "id": agent_id,
            "description": get("description") or "",
            # ADR 0001: an Agent IS a Hermes profile. Which one it drives is part
            # of recognising it on the roster, so it travels with the row.
            "hermes_profile": get("hermes_profile") or None,
            "enabled": get("enabled") is not False,
            "pipeline": get("pipeline"),
            "direction": get("direction"),
            # s14: the Agent's own voice settings, so the Settings editor can seed
            # its fields from THIS Agent (never from the proven default) without a
            # second fetch. Same shape as the document: providers is the map, knobs
            # the map.
            "providers": dict(get("providers") or {}),
            "knobs": dict(get("knobs") or {}),
            "guardrails": get("guardrails", {}),
            # VC24: what the person is talking to, and whether the Hermes profile this
            # Agent drives can be reached RIGHT NOW by the one resolver the bridges use.
            # False is a fact worth showing, not an error: on the direct lane it means
            # every call falls back to Realtime; on the Realtime lane, that tools fail.
            "agent_type": settings_catalog.agent_type_of(doc),
            "hermes_routable": hermes_gateway.gateway_url_for_profile(
                hermes_gateway.hermes_profile_of(doc)) is not None,
            # s16: per-outlet truth, and (s17) the only truth. The flattened
            # ``active`` view this row used to carry mirrored the PHONE outlet,
            # which meant a row on a split configuration reported half the answer
            # as if it were the whole one.
            "outlets": {o: {d: pointer[o][d] == agent_id
                            for d in profiles.ACTIVE_DIRECTIONS}
                        for o in profiles.OUTLETS},
            "valid": not errors,
            "errors": errors,
        }

    @app.get("/api/agents")
    async def list_agents():
        adir = _agents_dir()
        pointer, _, _ = _read_pointer()
        rows = []
        if adir.is_dir():
            for f in sorted(adir.glob("*.yaml")) + sorted(adir.glob("*.yml")):
                source = f.name
                try:
                    doc = yaml.safe_load(f.read_text())
                except Exception as exc:  # noqa: BLE001 — out-of-band garbage stays honest
                    rows.append(_summary(f.stem, None,
                                         [f"{source}: invalid YAML: {exc}"], pointer))
                    continue
                errors = _validate(doc, source)
                aid = doc.get("id") if isinstance(doc, dict) else None
                rows.append(_summary(aid if isinstance(aid, str) else f.stem,
                                     doc, errors, pointer))
        return rows

    @app.post("/api/agents", status_code=201)
    async def create_agent(request: Request):
        try:
            doc = await request.json()
        except Exception:  # noqa: BLE001
            return _json_error(422, ["request body must be a JSON agent document"])
        if not isinstance(doc, dict):
            return _json_error(422, ["agent document must be a JSON object"])
        aid = doc.get("id")
        if not _safe_agent_id(aid):
            return _json_error(422, [
                f"id: {aid!r} is rejected — must match {profiles._ID_RE.pattern} and "
                "not end in .yaml/.yml (path separators, traversal and extension "
                "smuggling are refused, never sanitized)"])
        errors = _validate(doc, f"agents/{aid}.yaml")
        if errors:
            return _json_error(422, errors)
        path = _agent_path(aid)
        if path.exists() or _lookup_agent(aid):
            return _json_error(409, f"agent '{aid}' already exists")
        _atomic_write_yaml(path, doc)
        return doc

    @app.post("/api/agents/preview")
    async def preview_agent(request: Request):
        """c20/c33: effective-config preview — the given draft AS-IF-SELECTED, computed
        by the named bridge's real builders in that bridge's venv (see
        PREVIEW_BRIDGES). Never a reimplementation here or in JS."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json_error(422, ["request body must be a JSON object"])
        if not isinstance(body, dict):
            return _json_error(422, ["request body must be a JSON object"])
        doc = body.get("doc")
        if not isinstance(doc, dict):
            return _json_error(422, ["doc: must be a JSON agent document"])
        aid = doc.get("id")
        if not _safe_agent_id(aid):
            return _json_error(422, [
                f"id: {aid!r} is rejected — must match {profiles._ID_RE.pattern} and "
                "not end in .yaml/.yml (path separators, traversal and extension "
                "smuggling are refused, never sanitized)"])
        # Same shared validate_profile the CRUD endpoints use — field-path errors
        # here render inline in the editor without a subprocess round-trip.
        errors = _validate(doc, f"agents/{aid}.yaml")
        if errors:
            return _json_error(422, errors)
        # c1: cascade is previewed through the shared ``voicecore.cascade_config``
        # builder - the SAME one the live cascade lane consumes, so the preview is
        # byte-equal to the dispatched pipeline (the mode-v bridge refuses cascade
        # inbound, so no subprocess).
        if doc.get("pipeline") == "cascade":
            try:
                registry = profiles.load_registry()
                config = cascade_config.build_cascade_config(doc, registry, os.environ)
            except cascade_config.CascadeConfigError as exc:
                return _json_error(422, [str(exc)])
            return {"bridge": "cascade", "pipeline": "cascade", "config": config}
        bridge = body.get("bridge")
        if bridge not in PREVIEW_BRIDGES:
            return _json_error(
                422, [f"bridge: {bridge!r} is not one of {sorted(PREVIEW_BRIDGES)}"])
        service_dir = PREVIEW_BRIDGES[bridge]
        python = service_dir / ".venv" / "bin" / "python"
        if not python.is_file():
            return _json_error(503, [
                f"preview unavailable: the {bridge} bridge venv is not built "
                f"(expected {python})"])
        proc = await asyncio.create_subprocess_exec(
            str(python), str(service_dir / "preview_effective.py"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=str(service_dir))
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(json.dumps({"doc": doc}).encode()), timeout=90)
        except asyncio.TimeoutError:
            proc.kill()
            return _json_error(504, [f"preview timed out running the {bridge} builder"])
        if proc.returncode != 0:
            return _json_error(502, [
                f"the {bridge} preview helper failed (exit {proc.returncode}): "
                + err.decode(errors="replace")[-2000:]])
        try:
            result = json.loads(out.decode())
        except Exception:  # noqa: BLE001
            return _json_error(502, [f"the {bridge} preview helper returned non-JSON"])
        if "error" in result:
            return _json_error(422, [result["error"]])
        return result

    @app.get("/api/agents/{agent_id}")
    async def get_agent(agent_id: str):
        found, err = _require_agent(agent_id)
        if err:
            return err
        path, doc = found
        if not isinstance(doc, dict):
            return _json_error(422, [f"{path.name}: agent document must be a YAML map"])
        errors = _validate(doc, path.name)
        if errors:
            return JSONResponse(status_code=422,
                                content={"detail": errors, "doc": doc})
        return doc

    @app.put("/api/agents/{agent_id}")
    async def update_agent(agent_id: str, request: Request):
        found, err = _require_agent(agent_id)
        if err:
            return err
        path, _current = found
        try:
            doc = await request.json()
        except Exception:  # noqa: BLE001
            return _json_error(422, ["request body must be a JSON agent document"])
        if not isinstance(doc, dict):
            return _json_error(422, ["agent document must be a JSON object"])
        if doc.get("id") != agent_id:
            return _json_error(422, [
                f"id: document id {doc.get('id')!r} must equal the path id "
                f"'{agent_id}' (renames go through create + delete)"])
        errors = _validate(doc, path.name)
        if errors:
            return _json_error(422, errors)
        _atomic_write_yaml(path, doc)
        body = {"agent": doc}
        if doc.get("enabled") is False:
            refs = _referencing_directions(agent_id)
            if refs:
                body["warning"] = (
                    f"agent '{agent_id}' is still named by active.yaml for "
                    f"{' and '.join(refs)} - calls in that slot will refuse to "
                    "start while it is disabled (no env fallback). Unset it via "
                    "PUT /api/active or re-enable the agent.")
        return body

    @app.delete("/api/agents/{agent_id}")
    async def delete_agent(agent_id: str):
        found, err = _require_agent(agent_id)
        if err:
            return err
        path, _current = found
        # Reference re-checked at REQUEST time (a confirm dialog is not a lock).
        # The id here is the document id, so a file named for someone else
        # cannot bypass the in-use 409 by being addressed as its filename.
        refs = _referencing_directions(agent_id)
        if refs:
            return _json_error(409, (
                f"refusing to delete agent '{agent_id}': it is referenced by "
                f"active.yaml as the {' and '.join(refs)} agent — "
                "unset it via PUT /api/active first"))
        path.unlink()
        return {"deleted": agent_id}

    # -- s14: per-Agent voice settings ------------------------------------------
    #
    # The wizard writes pipeline/provider/voice at creation; this is the edit
    # surface for an EXISTING Agent, and it is per-Agent by construction: it
    # reads that Agent's own document, replaces only the voice settings (the
    # pipeline, the providers, the knobs), validates the merged document with
    # the SAME validator the bridges resolve with, and writes that one file.
    # Nothing here touches any other Agent's document or any global state, so
    # the change applies to this Agent's next call and to no other Agent's.

    @app.put("/api/agents/{agent_id}/voice")
    async def update_agent_voice(agent_id: str, request: Request):
        found, err = _require_agent(agent_id)
        if err:
            return err
        path, current = found
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json_error(422, ["request body must be a JSON object"])
        if not isinstance(body, dict):
            return _json_error(422, ["request body must be a JSON object"])

        unknown = [k for k in body if k not in (
            "pipeline", "providers", "knobs", "guardrails")]
        if unknown:
            return _json_error(422, [
                f"unknown voice-setting key(s): {unknown} - this endpoint replaces "
                "only pipeline, providers, knobs and guardrails.on_call_tools"])

        if not isinstance(current, dict):
            return _json_error(422, [f"{path.name}: agent document must be a YAML map"])

        # Merge per-key, never wholesale: the Settings editor changes ONE thing
        # at a time, and a voice edit must not silently drop a temperature the
        # Agent already carries. Present keys update their slot; absent keys are
        # untouched. An explicit null in `providers` is a DELETION instruction:
        # it drops that key, which is what switching to cascade requires (the
        # cascade lane resolves {stt, llm, tts} and must not see providers.realtime).
        merged = dict(current)
        if "pipeline" in body:
            merged["pipeline"] = body["pipeline"]
        if "providers" in body:
            provided = body["providers"] or {}
            # Keep a key unless the request explicitly nulls it (null = delete);
            # keys the request does not mention are untouched.
            next_providers = {k: v for k, v in (current.get("providers") or {}).items()
                              if k not in provided or provided[k] is not None}
            for key, value in provided.items():
                if value is not None:
                    next_providers[key] = value
            merged["providers"] = next_providers
        if "knobs" in body:
            # Same rule as `providers`: a present key updates its slot, an explicit
            # null DELETES it. Without a delete, a keyterm list or a language could be
            # set from the Listening card and never cleared again.
            provided_knobs = body["knobs"] or {}
            merged["knobs"] = {
                **{k: v for k, v in (current.get("knobs") or {}).items()
                   if k not in provided_knobs or provided_knobs[k] is not None},
                **{k: v for k, v in provided_knobs.items() if v is not None}}
        if not merged.get("knobs"):
            merged.pop("knobs", None)
        if "guardrails" in body:
            guardrails = body["guardrails"]
            if (not isinstance(guardrails, dict)
                    or set(guardrails) != {"on_call_tools"}
                    or not isinstance(guardrails["on_call_tools"], bool)):
                return _json_error(422, [
                    "guardrails must contain exactly one boolean: on_call_tools"])
            if not profiles.is_hermes_direct(merged):
                return _json_error(422, [
                    "This tools control belongs to Hermes Direct Agents only"])
            existing_guardrails = current.get("guardrails")
            if existing_guardrails is not None and not isinstance(existing_guardrails, dict):
                return _json_error(422, ["stored guardrails must be a map"])
            merged["guardrails"] = {**(existing_guardrails or {}), **guardrails}

        errors = _validate(merged, path.name)
        if errors:
            return _json_error(422, errors)
        broken = _slot_break_error(agent_id, merged, path.name)
        if broken is not None:
            return broken
        _atomic_write_yaml(path, merged)
        return {"agent": merged}

    # -- VC24: which Hermes profile an Agent drives --------------------------------
    #
    # An Agent IS a Hermes profile (ADR 0001), and until now the only way to bind one
    # was to create a new profile in the wizard. This is the narrow write the profile
    # picker uses: ONE field, never the whole document.

    def _selectable_profiles() -> list:
        """`default` first (it is the container's own home, not a directory under
        profiles/, so no listing would ever contain it), then every profile directory,
        each with whether the one resolver can route to it right now."""
        rows = [{"name": "default", "complete": True, "gateway_status": None}]
        rows += [r for r in hermes_profiles.list_profiles(
            config_dir=profiles.config_dir()) if r["name"] != "default"]
        out = []
        for row in rows:
            url = hermes_gateway.gateway_url_for_profile(row["name"])
            out.append({"name": row["name"], "complete": bool(row.get("complete")),
                        "gateway_status": row.get("gateway_status"),
                        "gateway_url": url, "routable": url is not None})
        return out

    @app.put("/api/agents/{agent_id}/hermes-profile")
    async def update_agent_hermes_profile(agent_id: str, request: Request):
        found, err = _require_agent(agent_id)
        if err:
            return err
        path, current = found
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json_error(422, ["request body must be a JSON object"])
        if not isinstance(body, dict) or set(body) != {"hermes_profile"}:
            return _json_error(422, [
                "this endpoint sets exactly one field: {\"hermes_profile\": \"<name>\"}"])
        if not isinstance(current, dict):
            return _json_error(422, [f"{path.name}: agent document must be a YAML map"])
        name = body["hermes_profile"]
        if not isinstance(name, str) or not name.strip():
            return _json_error(422, ["hermes_profile: must be a non-empty profile name"])
        name = name.strip()
        chosen = next((r for r in _selectable_profiles() if r["name"] == name), None)
        if chosen is None:
            return _json_error(422, [
                f"hermes_profile: '{name}' is not a Hermes profile this dashboard can "
                "see (it lists `default` and every directory under "
                "HERMES_PROFILES_DIR)"])
        if not chosen["complete"]:
            return _json_error(422, [
                f"hermes_profile: '{name}' is still being created (it carries the "
                ".incomplete marker or has no config.yaml), so Hermes will not start it"])
        held = _held_slots(agent_id)
        if held and not chosen["routable"]:
            # An unroutable profile on a HELD slot is a quiet outage: the direct lane
            # would fall back to Realtime on every call and the Realtime lane's tools
            # would all fail, with the assignment still looking healthy.
            slots = ", ".join(f"outlets.{o}.{d}" for o, d in held)
            return _json_error(409, [
                f"hermes_profile: '{name}' has no running gateway right now "
                f"(registry status: {chosen['gateway_status'] or 'not listed'}), and "
                f"agent '{agent_id}' is assigned to {slots}. Start the profile or "
                "unassign the Agent first."])
        merged = dict(current)
        merged["hermes_profile"] = name
        errors = _validate(merged, path.name)
        if errors:
            return _json_error(422, errors)
        broken = _slot_break_error(agent_id, merged, path.name)
        if broken is not None:
            return broken
        _atomic_write_yaml(path, merged)
        return {"agent": merged, "routable": chosen["routable"],
                "gateway_url": chosen["gateway_url"]}

    # -- s14: the Settings catalog -------------------------------------------------
    #
    # One consolidated answer for the Settings page: the proven default, the two
    # pipelines each with its honest label, every provider with its label, the
    # realtime-lane voice options with their labels, and the speed dial. Outlet
    # assignment and the roster are the SAME endpoints the Agents screen reads
    # (GET /api/active, GET /api/agents), so Settings and Agents can never
    # disagree about who answers which Outlet.
    #
    # Speed dial is ticket 09's (PR #18) - the Place-a-call screen dials from
    # it. The Settings card reads the same shape through the same module, so
    # the two pages cannot disagree about the list a call path depends on.

    @app.get("/api/settings")
    async def settings_catalog_view():
        try:
            registry = _load_registry()
        except profiles.ProfileError as exc:
            return _registry_error(exc)
        await _prime_missing(registry)
        pipelines = [{"id": pid, **settings_catalog.pipeline_label(pid)}
                     for pid in profiles.PIPELINES]
        # One bad speed-dial file must degrade ONE card, never the whole page:
        # the rest of Settings stays answerable even when the speed dial cannot
        # be read. The error travels in the payload; the card renders it.
        try:
            speed_dial_payload = speed_dial.load()
        except Exception as exc:  # noqa: BLE001 - any read failure degrades one card
            speed_dial_payload = {"owner": None, "numbers": [],
                                  "error": f"speed dial could not be read: {exc}"}
        return {
            "proven": settings_catalog.proven_defaults(),
            "pipelines": pipelines,
            # VC24: what the person is talking to. Presets over pipeline/providers.
            "agent_types": settings_catalog.agent_types(),
            "providers": _rows(registry),
            "realtime_voices": settings_catalog.realtime_voice_options(),
            "speed_dial": speed_dial_payload,
        }

    # -- s13: the Agent creation wizard -------------------------------------------
    #
    # Creating an Agent creates a BEING (ADR 0001): a real Hermes profile
    # directory, plus the voice-config document that makes it reachable on an
    # Outlet. Both halves are written by ONE request, at the end of the wizard.
    # Nothing is persisted while the operator is still answering questions, so a
    # closed tab leaves nothing at all -- and the write itself is staged behind
    # ticket 12's `.incomplete` marker, so even a crash mid-write leaves a
    # directory the Hermes side reports and refuses rather than starts.

    def _wizard_error(exc: hermes_profiles.ProfileCreateError) -> JSONResponse:
        return _json_error(exc.status, [str(exc)])

    @app.get("/api/hermes")
    async def hermes_state():
        """Can this dashboard create a real profile, and what already exists.

        `available: false` is a first-class answer, not an error: on a deploy
        where the profiles directory is not mounted into this service there is
        no honest way to create one, and the screen says which compose line is
        missing instead of writing a profile nothing will ever run.
        """
        state_doc = dict(hermes_profiles.availability())
        state_doc["profiles"] = hermes_profiles.list_profiles(
            config_dir=profiles.config_dir())
        state_doc["registry_path"] = str(
            profiles.config_dir() / hermes_profiles.REGISTRY_BASENAME)
        # VC24: what the profile picker offers. Served even when creation is
        # unavailable: binding an Agent to `default` needs no profiles directory.
        state_doc["selectable"] = _selectable_profiles()
        state_doc["memory_modes"] = list(hermes_profiles.MEMORY_MODES)
        state_doc["tool_profiles"] = list(hermes_profiles.DEFAULT_TOOL_PROFILES)
        state_doc["proven"] = {"pipeline": hermes_profiles.PROVEN_PIPELINE,
                               "realtime_provider":
                                   hermes_profiles.PROVEN_REALTIME_PROVIDER}
        # s14: the realtime-lane voice options, served from the ONE source
        # (settings_catalog) so the wizard and the Settings page offer the same
        # list. The wizard used to hardcode its own copy; that copy drifted.
        state_doc["realtime_voices"] = settings_catalog.realtime_voice_options()
        return state_doc

    @app.get("/api/hermes/profiles/{name}")
    async def hermes_profile_detail(name: str):
        """What "inherit from this one" would take. Never reads a `.env`, so no
        answer here can carry a credential."""
        try:
            hermes_profiles.validate_name(name)
        except hermes_profiles.ProfileCreateError as exc:
            return _wizard_error(exc)
        state_doc = hermes_profiles.availability()
        if not state_doc["available"]:
            return _json_error(503, [f"{state_doc['reason']} "
                                     f"{state_doc['deploy_hint']}"])
        if name not in {row["name"] for row in hermes_profiles.list_profiles()}:
            return _json_error(404, [f"no Hermes profile '{name}'"])
        return hermes_profiles.inheritable(name)

    @app.post("/api/agents/create", status_code=201)
    async def create_agent_wizard(request: Request):
        """The wizard's ONE write: a Hermes profile plus its voice document.

        Order matters and is the abandonment guarantee:
          - everything that can be refused is refused BEFORE anything is created
            (the name, the voice document against the same validator the bridges
            resolve with, and both existing-name collisions);
          - the profile is then built behind `.incomplete`, and the agent
            document is written while that marker is still down;
          - the marker is removed last, in one atomic unlink.
        A failure anywhere removes both halves, so a refused creation leaves the
        roster exactly as it found it.
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json_error(422, ["request body must be a JSON object"])
        if not isinstance(body, dict):
            return _json_error(422, ["request body must be a JSON object"])

        try:
            name = hermes_profiles.validate_name(body.get("name"))
        except hermes_profiles.ProfileCreateError as exc:
            return _wizard_error(exc)

        state_doc = hermes_profiles.availability()
        if not state_doc["available"]:
            return _json_error(503, [f"{state_doc['reason']} "
                                     f"{state_doc['deploy_hint']}"])

        agent_path = _agent_path(name)
        if agent_path.exists() or _lookup_agent(name):
            return _json_error(409, [f"an Agent called '{name}' already exists"])

        spec = {
            "name": name,
            "description": body.get("description") or "",
            "inherit_from": body.get("inherit_from") or None,
            "inherit_skills": bool(body.get("inherit_skills")),
            "identity_name": body.get("identity_name") or None,
            "soul": body.get("soul") or None,
            "model": body.get("model") or None,
            "model_provider": body.get("model_provider") or None,
            "tools_profile": body.get("tools_profile") or None,
            "memory": body.get("memory") or "shared",
            "telegram_connect": bool(body.get("telegram_connect")),
            # Stripped here so surrounding whitespace is not written into the
            # profile's .env verbatim, and so the check below sees what would be
            # stored rather than what was typed around it.
            "telegram_bot_token": (body.get("telegram_bot_token") or "").strip() or None,
            "telegram_dm_policy": body.get("telegram_dm_policy") or None,
            "pipeline": body.get("pipeline") or hermes_profiles.PROVEN_PIPELINE,
            "providers": body.get("providers") or {},
            "knobs": body.get("knobs") or {},
            "persona": body.get("persona") or None,
        }
        if spec["memory"] not in hermes_profiles.MEMORY_MODES:
            return _json_error(422, [
                f"memory: {spec['memory']!r} is not one of "
                f"{list(hermes_profiles.MEMORY_MODES)}"])
        if spec["telegram_connect"]:
            # Absent and malformed are one refusal, from one function: an accepted
            # criterion was "empty OR malformed is rejected", and checking only
            # truthiness met half of it while writing the other half to disk.
            problem = hermes_profiles.telegram_token_problem(spec["telegram_bot_token"])
            if problem:
                return _json_error(422, [problem])

        doc = hermes_profiles.build_agent_doc(spec)
        # The same validator the bridges resolve with: a document this refuses is
        # one a call path would refuse, and refusing it here costs nothing,
        # whereas refusing it after the profile exists leaves a half-made Agent.
        errors = _validate(doc, f"agents/{name}.yaml")
        if errors:
            return _json_error(422, errors)

        wrote_agent = {"done": False}

        def _stage_agent_document():
            _atomic_write_yaml(agent_path, doc)
            wrote_agent["done"] = True

        try:
            created = hermes_profiles.create_profile(
                spec, on_staged=_stage_agent_document)
        except hermes_profiles.ProfileCreateError as exc:
            if wrote_agent["done"]:
                agent_path.unlink(missing_ok=True)
            return _wizard_error(exc)

        # Never echo the token back, in any shape. `created` carries a boolean.
        return {"agent": doc, "profile": created}

    @app.get("/api/active")
    async def get_active():
        return await _active_response()

    @app.put("/api/active")
    async def put_active(request: Request):
        """Store the outlet assignments. ONE accepted request shape (s17):

            {outlets: {phone|talk: {inbound: <id|null>, outbound: <id|null>}}}

        The flat ``{inbound: <id>}`` / ``{outbound: <id>}`` body the old screen
        sent is refused (422), not translated. It named a direction and no
        Outlet, so the only thing it could mean was "every Outlet at once", and
        one click of it silently undid a per-outlet split. There is nowhere for
        that shape to land any more - the hazard is gone rather than guarded.

        Every non-null slot must name an existing, valid, enabled agent whose
        activation that direction would accept - a pointer the live call path would
        refuse is refused here (422), never stored.
        """
        try:
            changes = await request.json()
        except Exception:  # noqa: BLE001
            return _json_error(422, ["request body must be a JSON object"])
        if not isinstance(changes, dict):
            return _json_error(422, ["request body must be a JSON object"])
        unknown = [k for k in changes if k != "outlets"]
        if unknown:
            return _json_error(
                422, [f"unknown key(s): {unknown} - an assignment names the Outlet "
                      f"it is for (one of {list(profiles.OUTLETS)}). Send "
                      "{'outlets': {<outlet>: {inbound: <id|null>, "
                      "outbound: <id|null>}}}"])
        if "outlets" not in changes:
            return _json_error(
                422, ["request body must carry an 'outlets' map: "
                      "{'outlets': {<outlet>: {inbound: <id|null>, "
                      "outbound: <id|null>}}}"])
        return await _put_active_outlets(changes.get("outlets"))

    async def _put_active_outlets(outlets):
        if not isinstance(outlets, dict):
            return _json_error(422, ["outlets: must be a map of outlet -> "
                                     "{inbound: <id|null>, outbound: <id|null>}"])
        unknown_outlets = [k for k in outlets if k not in profiles.OUTLETS]
        if unknown_outlets:
            return _json_error(422, [f"unknown outlet key(s): {unknown_outlets} "
                                     f"(allowed: {list(profiles.OUTLETS)})"])
        pointer, _, _ = _read_pointer()
        for outlet in profiles.OUTLETS:
            if outlet not in outlets:
                continue
            entry = outlets[outlet]
            if entry is None:
                # A null entry reads like "clear the outlet" but clears nothing -
                # that silent no-op is exactly the ambiguity to refuse. The clear
                # form is an explicit map: {"inbound": null, "outbound": null}.
                return _json_error(
                    422, [f"outlets.{outlet}: null clears nothing - send a map "
                          "{inbound: <id|null>, outbound: <id|null>} instead"])
            if not isinstance(entry, dict):
                return _json_error(422, [f"outlets.{outlet}: must be a map shaped "
                                         "{inbound: <id|null>, outbound: <id|null>}"])
            unknown_dirs = [k for k in entry if k not in profiles.ACTIVE_DIRECTIONS]
            if unknown_dirs:
                return _json_error(
                    422, [f"outlets.{outlet}: unknown direction key(s): {unknown_dirs} "
                          f"(allowed: {list(profiles.ACTIVE_DIRECTIONS)})"])
            for direction in profiles.ACTIVE_DIRECTIONS:
                if direction not in entry:
                    continue
                error = _validate_active_slot(f"{outlet}.{direction}",
                                              entry[direction], direction, outlet)
                if error is not None:
                    return error
                pointer[outlet][direction] = entry[direction]
        _atomic_write_yaml(profiles.active_path(), _canonical_pointer_doc(pointer))
        return await _active_response()

    async def _active_response():
        pointer, warnings, slot_warnings = _read_pointer()
        return {
            # s16: ``outlets`` is the truth, per outlet and direction - and since
            # s17 the only thing on the wire. The flat keys that used to sit
            # beside it mirrored the PHONE outlet, so on a split configuration
            # they were a wrong answer to a question nobody needed to ask.
            "outlets": pointer,
            # The outlet axis itself, in model order, so a screen renders the
            # outlets that EXIST rather than a hardcoded pair (s02: a third outlet
            # is one tuple entry in ``profiles.OUTLETS`` and must appear here
            # without a frontend change).
            "outlet_order": list(profiles.OUTLETS),
            "warnings": warnings,
            # s02: the same warnings, addressed to the slot they belong to, so the
            # Agents screen can mark the card that is dead instead of stacking an
            # unattributed banner above a row of calm-looking outlets. A message
            # here is always also in ``warnings``; the flat list stays the whole
            # truth for any client that does not read this map.
            "slot_warnings": slot_warnings,
            # c33 honesty: report whether VOICE_AGENT overrides the pointer at
            # runtime (it does so for EVERY outlet - the pointer is inert then).
            "voice_agent_env": profiles.agent_id_from_env(),
        }

    def _validate_active_slot(where: str, aid, direction: str, outlet: str):
        """The PUT-side guard for one slot: unset-to-null is always allowed; a
        non-null value must be an existing, valid, enabled agent whose activation
        that direction accepts (the same refusal the live bridges raise)."""
        if aid is None:
            return None
        if not _safe_agent_id(aid):
            return _json_error(422, [f"{where}: {aid!r} is not a valid agent id"])
        matches = _lookup_agent(aid)
        if not matches:
            return _json_error(
                422, [f"{where}: agent '{aid}' does not exist - refusing to "
                      "point active.yaml at a missing agent"])
        if len(matches) > 1:
            names = " and ".join(path.name for path, _ in matches)
            return _json_error(
                422, [f"{where}: agent '{aid}' is duplicated ({names})"])
        path, doc = matches[0]
        if not isinstance(doc, dict):
            return _json_error(
                422, [f"{where}: agent '{aid}' is unreadable: not a YAML map"])
        errors = _validate(doc, path.name)
        if errors:
            return _json_error(422, [f"{where}: agent '{aid}' is invalid - fix "
                                     "it before activating"] + errors)
        if isinstance(doc, dict) and doc.get("enabled") is False:
            return _json_error(
                422, [f"{where}: agent '{aid}' has enabled: false - refusing "
                      "to activate a disabled agent (calls would refuse to start)"])
        # Mirror the bridges' per-direction activation refusals (cascade is
        # outbound-only, unimplemented realtime providers refuse) - an activation
        # the live call would refuse must not be storable from the dashboard
        # (the `cunt` incident: cascade activated INBOUND silently broke the DID).
        problem = profiles.activation_problem(doc, f"agents/{path.name}",
                                              direction=direction, outlet=outlet)
        if problem is not None:
            return _json_error(
                422, [f"{where}: refusing to activate - every {direction} "
                      f"call would refuse to start: {problem}"])
        return None

    # -- ticket 05 call archive + ticket 09 place-a-call ---------------------------

    @app.get("/api/calls")
    async def list_calls(page: int = 1, page_size: int = 20, q: str = None,
                         agent: str = None, outlet: str = None):
        """Calls API endpoint returning a paged list of Calls or free-text search results
        from the call archive (the SQLite store, or Hindsight - `voicecore.call_store` decides).
        If the archive is empty or unreachable, renders an honest empty state.

        s5: `agent` and `outlet` filter the list. Both accept
        `hindsight_calls.UNKNOWN_FILTER` to select the calls where that field was not
        retained - the whole pre-ticket-05 archive, which would otherwise be
        unreachable through the filters. The response carries the values available."""
        return await hindsight_calls.list_calls(page=page, page_size=page_size, q=q,
                                                agent=agent, outlet=outlet)

    @app.get("/api/calls/{call_id}")
    async def call_detail(call_id: str):
        """Single Call verbatim transcript and metadata from the call archive.

        Ticket 07: the audio is answered from the recordings VOLUME, not from the
        document store. The Call's metadata carries a reference, but a reference is not
        evidence the file survived — the screen must show a player only when there is
        something to play, so availability is resolved against the disk here.
        """
        res = await hindsight_calls.get_call(call_id)
        res["recording"] = recording_store.describe(call_id)
        # `partial` means a bank could not be read, so "not found" is unknown
        # rather than false: a 404 here would tell the owner a call he made does
        # not exist. Answer 200 and let the screen relay the reason.
        if res.get("unreachable") or res.get("partial"):
            return JSONResponse(res, status_code=200)
        if res.get("call") is None:
            return JSONResponse(res, status_code=404)
        return res

    @app.get("/api/calls/{call_id}/recording")
    async def call_recording(call_id: str):
        """Serve a Call's audio, with RANGE support so the player can scrub.

        Scrubbing an ``<audio>`` element is a ``Range: bytes=…`` request; a server that
        ignores Range hands back the whole body and the scrub bar goes dead. Starlette's
        ``FileResponse`` implements RFC 7233 (206 + Content-Range, 416 past the end,
        Accept-Ranges on the full body), so there is no hand-rolled second
        implementation here — but the behaviour is load-bearing for this ticket, so
        ``tests/test_recording_api.py`` asserts it directly rather than trusting the
        dependency to keep it.

        The path is resolved from the CALL ID under the recordings root, never from a
        path held in a document store — see voicecore/recording_store.
        """
        path = recording_store.find(call_id)
        if path is None:
            return _json_error(404, [f"no recording for call '{call_id}'"])
        return FileResponse(path, media_type=recording_store.CONTENT_TYPE,
                            headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/speed-dial")
    async def get_speed_dial():
        """Saved numbers plus the owner's own number (VC19)."""
        return speed_dial.load()

    @app.put("/api/speed-dial")
    async def put_speed_dial(request: Request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json_error(422, ["request body must be a JSON object"])
        if not isinstance(body, dict):
            return _json_error(422, ["request body must be a JSON object"])
        entries = body.get("numbers")
        try:
            return speed_dial.save(entries)
        except ValueError as exc:
            return _json_error(422, [str(exc)])

    async def _json_body(request: Request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return None
        return body if isinstance(body, dict) else None

    @app.post("/api/calls/place")
    async def place_a_call(request: Request):
        """One-shot outbound: Agent + number + Mission + disclose. Rings the phone.

        Does not read or write the pointer. Two fires with different Agents
        cannot interfere because nothing global is mutated. There is no dry-run
        and no allow-list; a number the owner typed is the number that is dialled.

        Ticket 11: everything this route knows about placing a Call is in
        ``place_call.place_from_request``, because a Schedule that comes due
        calls exactly that. This handler is deliberately nothing but HTTP —
        anything added here would be a manual-only behaviour, which is the
        divergence the ticket exists to prevent.
        """
        body = await _json_body(request)
        if body is None:
            return _json_error(422, ["request body must be a JSON object"])
        try:
            return await place_call.place_from_request(
                body, transport=state.transport)
        except place_call.PlaceRejected as exc:
            return _json_error(exc.status, exc.detail)

    def _author_agent(agent_id):
        """``(agent_id, hermes_profile, gateway_url)`` or a JSON error.

        Same load as place (the Agent must be able to run outbound) and the
        same gateway seam as ticket 06. An unknown hermes_profile is a
        refusal, not a borrow of another Agent's backend.
        """
        if not isinstance(agent_id, str) or not agent_id.strip():
            return _json_error(422, ["agent: required — the Agent that will write the Mission"])
        agent_id = agent_id.strip()
        if not _safe_agent_id(agent_id):
            return _json_error(422, [f"agent: {agent_id!r} is not a valid agent id"])
        try:
            profile, gateway_url = mission_author.resolve_agent_gateway(agent_id)
        except profiles.ProfileError as exc:
            return _json_error(409, [f"cannot author: agent '{agent_id}' cannot "
                                     f"run outbound: {exc}"])
        if not gateway_url:
            return _json_error(409, [
                f"cannot author: agent '{agent_id}' hermes_profile "
                f"'{profile}' has no gateway URL — it will not borrow another"])
        return agent_id, profile, gateway_url

    @app.post("/api/missions/expand")
    async def expand_a_mission(request: Request):
        """The named Agent turns a short prompt into a Mission. Never dials.

        The result is returned for the operator to review in the Mission
        field. This route does not place a call and does not write the
        pointer. A failure is an error body with no ``mission`` key, so the
        form cannot be cleared by applying the response.
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json_error(422, ["request body must be a JSON object"])
        if not isinstance(body, dict):
            return _json_error(422, ["request body must be a JSON object"])
        resolved = _author_agent(body.get("agent"))
        if isinstance(resolved, JSONResponse):
            return resolved
        agent_id, profile, gateway_url = resolved
        prompt = body.get("prompt")
        if not isinstance(prompt, str):
            return _json_error(422, ["prompt: required — the short line to expand"])
        text, err = await mission_author.expand_mission(
            prompt, gateway_url=gateway_url,
            token=hermes_gateway.gateway_token(),
            timeout_s=mission_author.timeout_from_env(),
            transport=state.transport)
        if text is None:
            return _json_error(502, [err or "the Agent did not write a Mission"])
        return {"mission": text, "agent": agent_id, "hermes_profile": profile}

    @app.post("/api/missions/dictate")
    async def dictate_a_mission(request: Request):
        """The named Agent listens to a browser recording and writes a Mission.

        Multipart: ``agent`` + ``audio``. Never dials. Never speaks back.
        A failure is an error body with no ``mission`` key.
        """
        try:
            form = await request.form()
        except Exception:  # noqa: BLE001
            return _json_error(422, ["expected a multipart form with 'agent' and 'audio'"])
        resolved = _author_agent(form.get("agent"))
        if isinstance(resolved, JSONResponse):
            return resolved
        agent_id, profile, gateway_url = resolved
        upload = form.get("audio")
        if upload is None or not hasattr(upload, "read"):
            return _json_error(422, ["audio: required recording (form field)"])
        audio = await upload.read()
        filename = getattr(upload, "filename", None) or "recording.webm"
        content_type = getattr(upload, "content_type", None) or "audio/webm"
        text, err = await mission_author.dictate_mission(
            audio, gateway_url=gateway_url,
            token=hermes_gateway.gateway_token(),
            timeout_s=mission_author.timeout_from_env(),
            content_type=content_type, filename=filename,
            transport=state.transport)
        if text is None:
            return _json_error(502, [err or "the Agent did not write a Mission"])
        return {"mission": text, "agent": agent_id, "hermes_profile": profile}

    # -- ticket 11: Schedules — a Call that has not happened yet -------------

    # A Schedule created for a moment already past is a typo, not an
    # instruction; the tolerance covers a browser clock that runs ahead of the
    # NAS, which shifts a local time the browser computed.
    CREATE_PAST_TOLERANCE_S = 120

    @app.get("/api/schedules")
    async def list_schedules():
        """Upcoming Calls first, then the settled ones, newest settled first."""
        return {
            "schedules": schedules.load_all(),
            "now": schedules.to_iso(schedules.now_utc()),
            "timezone": schedules.default_timezone(),
            "grace_s": schedules.grace_s(),
        }

    @app.post("/api/schedules", status_code=201)
    async def create_schedule(request: Request):
        """Write a Schedule: a time plus the Call it will place.

        The Call half is graded by the SAME code that grades a manual place,
        including the 409 for an Agent that cannot run outbound — so a Schedule
        that could never be placed is refused now, to whoever is asking, rather
        than at 3am to nobody. (It is graded again when it fires: an Agent can
        be deleted in between.)
        """
        body = await _json_body(request)
        if body is None:
            return _json_error(422, ["request body must be a JSON object"])
        try:
            call = place_call.validate_place_request(body)
            place_call.check_agent_can_run(call)
            due = schedules.resolve_due(body.get("at"), body.get("tz"))
        except place_call.PlaceRejected as exc:
            return _json_error(exc.status, exc.detail)
        except schedules.ScheduleError as exc:
            return _json_error(exc.status, exc.detail)

        due_at = schedules.parse_iso(due["due_at"])
        late = (schedules.now_utc() - due_at).total_seconds()
        if late > CREATE_PAST_TOLERANCE_S:
            return _json_error(422, [
                f"at: {due['local_time']} ({due['timezone']}) is {int(late)}s in "
                f"the past — a Schedule is a Call that has not happened yet"])

        record = schedules.create(call, due, label=body.get("label"))
        if state.scheduler is not None:
            state.scheduler.nudge()
        return record

    @app.get("/api/schedules/{schedule_id}")
    async def get_schedule(schedule_id: str):
        record = schedules.load(schedule_id)
        if record is None:
            return _json_error(404, [f"no Schedule '{schedule_id}'"])
        return record

    @app.delete("/api/schedules/{schedule_id}")
    async def cancel_schedule(schedule_id: str):
        """Cancel an upcoming Call — by racing the scheduler for the same claim.

        Cancelling something whose time has just arrived is a real race, and it
        is decided here rather than papered over: cancelling takes the same
        atomic claim firing takes, so exactly one of them happens. Lose it and
        the answer is 409 with what the Schedule actually did, not a 200 that
        would leave the owner believing a phone that is ringing was stopped.
        """
        record = schedules.load(schedule_id)
        if record is None:
            return _json_error(404, [f"no Schedule '{schedule_id}'"])
        if record.get("status") != schedules.STATUS_PENDING:
            return _json_error(409, [
                f"this Schedule is already {record.get('status')} — "
                f"there is nothing left to cancel"])
        if not schedules.claim(schedule_id, schedules.INTENT_CANCEL):
            current = schedules.load(schedule_id) or record
            return _json_error(409, [
                f"too late — this Call is already being placed "
                f"(now {current.get('status')})"])
        cancelled = schedules.settle(
            schedule_id, schedules.STATUS_CANCELLED,
            reason="cancelled before it was placed")
        if state.scheduler is not None:
            state.scheduler.nudge()
        return cancelled or record

    # Ticket 15: the hand-written ``static-legacy/`` site, the ``/legacy`` and
    # ``/old`` routes and their mount are gone. The built React bundle in
    # ``static/`` is the only site, and it is served at the root.
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(content=_FAVICON_SVG, media_type="image/svg+xml")

    if (STATIC_DIR / "assets").exists():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(404)
    async def spa_fallback(request: Request, exc):
        if request.method == "GET" and not request.url.path.startswith(
            ("/api", "/healthz", "/static", "/assets", "/favicon.ico")
        ):
            index_path = STATIC_DIR / "index.html"
            if index_path.exists():
                return FileResponse(index_path)
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    return app


app = create_app()
