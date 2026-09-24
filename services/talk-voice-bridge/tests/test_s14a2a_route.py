"""s14a-2a c2 — the readiness ROUTE: authenticated, read-only, no side effects.

Separate from test_s14a2a_readiness.py (which grades the stanza logic) because this
suite grades the HTTP surface: who may call it, and what it must never do. The bridge
already gates /call/outbound on the gateway bearer; readiness follows the same gate —
it reports on credentials, so it is owner-only by the same argument.
"""
import os

import pytest
import yaml
from fastapi.testclient import TestClient


TOKEN = "s14a2a-gateway-token"

# The route reports on the ACTIVATED OUTBOUND cascade profile, so this suite needs one
# to exist. It used to pin `VOICE_AGENT=supplier-caller` and lean on the worked-example
# YAML the repo shipped; VC18 deleted those three files in ticket 15 (they were written
# to satisfy a build criterion, not agents in use), so the profile is written here
# instead. Its providers are the LIVE-wired ones on purpose: this suite grades the HTTP
# surface, and a bench-only provider would make every report `ready: false` for a reason
# that belongs to test_s14a2a_readiness.py.
CASCADE_AGENT = {
    "id": "s14a2a-cascade",
    "description": "Cascade agent for the readiness-route suite.",
    "enabled": True,
    "hermes_profile": "default",
    "direction": "outbound",
    "pipeline": "cascade",
    "providers": {"stt": "deepgram", "llm": "openrouter", "tts": "elevenlabs"},
}


@pytest.fixture
def client(tmp_path_factory):
    """A TestClient over the real app with the browser lifespan neutered — starting a
    real Playwright browser is neither needed nor wanted to grade an HTTP gate.

    Env is managed by hand rather than with monkeypatch BECAUSE reloading `server`
    snapshots the env into its module-level `_cfg`. monkeypatch's undo runs AFTER this
    fixture's teardown, so a reload here would re-read the still-patched env and leave a
    gateway token set for the rest of the session — which silently flipped six unrelated
    tests in test_s3_overlays.py from 200/403 to 401. Restore the env FIRST, then reload,
    so the module state we leave behind matches the env we found.
    """
    import importlib
    import config
    import server as server_mod

    saved = {k: os.environ.get(k)
             for k in ("HERMES_GATEWAY_TOKEN", "VOICE_AGENT", "VOICE_CONFIG_DIR")}
    os.environ["HERMES_GATEWAY_TOKEN"] = TOKEN
    # VOICE_AGENT pins the ACTIVATED outbound profile (it outranks the active.yaml
    # pointer), so the route has a real cascade profile to report on. The config dir
    # holds only `agents/`, so `providers.yaml` still resolves to the canonical registry
    # - the route must grade this draft against the REAL provider entries.
    cdir = tmp_path_factory.mktemp("s14a2a-config")
    (cdir / "agents").mkdir()
    (cdir / "agents" / f"{CASCADE_AGENT['id']}.yaml").write_text(
        yaml.safe_dump(CASCADE_AGENT))
    os.environ["VOICE_CONFIG_DIR"] = str(cdir)
    os.environ["VOICE_AGENT"] = CASCADE_AGENT["id"]
    importlib.reload(config)
    importlib.reload(server_mod)

    class _NoBrowser:
        async def start(self):
            pass

        async def stop(self):
            pass

    server_mod._browser = _NoBrowser()
    # Config is a FROZEN dataclass — the token must arrive via the env the reload reads,
    # not by assignment. That is also the honest path: it proves the route gates on the
    # real env-derived config rather than on something a test poked in afterwards.
    assert server_mod._cfg.hermes_gateway_token == TOKEN
    try:
        with TestClient(server_mod.app) as c:
            c.server_mod = server_mod
            yield c
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(config)
        importlib.reload(server_mod)


def test_unauthenticated_is_refused(client):
    assert client.get("/readiness/cascade").status_code == 401


def test_wrong_token_is_refused(client):
    r = client.get("/readiness/cascade",
                   headers={"Authorization": "Bearer not-the-token"})
    assert r.status_code == 401


def test_authenticated_returns_a_readiness_report(client):
    r = client.get("/readiness/cascade",
                   headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    body = r.json()
    assert set(body["checks"]) >= {"live_wiring", "providers", "eventlog_writable"}
    assert isinstance(body["ready"], bool)


def test_readiness_never_places_a_call_or_creates_a_room(client, monkeypatch):
    """READ-ONLY by construction. If readiness can ever reach the dial or OCS seams, a
    monitoring poll becomes a dialer — so blow up loudly if either is touched."""
    server_mod = client.server_mod

    async def _boom_start(*a, **kw):
        raise AssertionError("readiness reached CallSession.start")

    async def _boom_resolve(*a, **kw):
        raise AssertionError("readiness reached outbound.resolve_room")

    monkeypatch.setattr(server_mod._session, "start", _boom_start)
    monkeypatch.setattr(server_mod.outbound, "resolve_room", _boom_resolve)

    r = client.get("/readiness/cascade",
                   headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_readiness_does_not_disturb_an_active_call(client):
    """s14b polls this during a campaign; it must not take the single-call slot lock or
    report a busy bridge as unready — busy is orthogonal to CREDENTIAL readiness."""
    server_mod = client.server_mod
    # active_token is a read-only property; simulate an in-progress call via its backing
    # field rather than adding a setter to production code just to make a test convenient.
    server_mod._session._active_token = "occupied-room"
    try:
        r = client.get("/readiness/cascade",
                       headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200
        assert server_mod._session.active_token == "occupied-room", \
            "readiness disturbed the in-progress call's session state"
    finally:
        server_mod._session._active_token = None
