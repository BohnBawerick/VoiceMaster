"""s11b-1 c3/c4: the REAL /call/outbound pre-dial gate, driven through the FastAPI app.

The Talk bridge gates the dial target on talk_policy.allow (kind-scoped, matched on the
DIALED FORM BEFORE any OCS resolve), replacing the old number_policy/E.164 gate for Talk:
  - a token target is used VERBATIM (never resolved) — a group room token dials as-is;
  - a username target resolves via OCS ONLY after it passes the gate;
  - a denied dial returns 403 and NEVER calls resolve_room (zero OCS side effect).
resolve_room + the CallSession are stubbed spies, so the run places no real Talk call.
"""
import pytest
import yaml
from fastapi.testclient import TestClient

import outbound as outbound_mod
from voicecore import profiles
import server

import parity_env as pe
from profile_helpers import profile_doc, write_config_dir


def _scrub(monkeypatch):
    for var in pe.CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


def _point_env(monkeypatch, d, voice_agent="t-agent"):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(d))
    monkeypatch.setenv("VOICE_AGENT", voice_agent)


class _OkSession:
    def __init__(self):
        self.started = 0

    async def start(self, *a, **kw):
        self.started += 1
        return True


@pytest.fixture
def gate(monkeypatch, tmp_path):
    """Wire the app with a spy session + a resolve_room counter, pointed at a config dir
    whose sole agent carries ``talk_policy``. Returns a dial(...) helper + the spies."""
    _scrub(monkeypatch)
    session = _OkSession()
    monkeypatch.setattr(server, "_session", session)
    resolved = []

    async def _spy_resolve(cfg, target_user):
        resolved.append(target_user)
        return f"resolved-token-for-{target_user}"

    monkeypatch.setattr(outbound_mod, "resolve_room", _spy_resolve)

    def build(talk_policy=None, number_policy=None):
        overrides = {}
        if talk_policy is not None:
            overrides["talk_policy"] = talk_policy
        if number_policy is not None:
            overrides["number_policy"] = number_policy
        doc = profile_doc(**overrides)
        d = write_config_dir(tmp_path, [doc])
        _point_env(monkeypatch, d)
        client = TestClient(server.app, headers={
        "Authorization": f"Bearer {server._cfg.hermes_gateway_token}"})

        def dial(**target):
            body = {"brief": "hi", **target}
            return client.post("/call/outbound", json=body)

        return dial

    return build, resolved, session


# -- c3: talk_policy.allow gates the dial, pre-resolve, zero OCS on deny --------

def test_username_listed_passes_and_resolves(gate):
    build, resolved, session = gate
    dial = build(talk_policy={"allow": ["sam"]})
    r = dial(target="sam")
    assert r.status_code == 200
    assert resolved == ["sam"]              # resolve happens AFTER the gate passes
    assert session.started == 1


def test_username_not_listed_403_without_resolving(gate):
    build, resolved, session = gate
    dial = build(talk_policy={"allow": ["sam"]})
    r = dial(target="eve")
    assert r.status_code == 403
    assert resolved == []                     # denied BEFORE any OCS resolve
    assert session.started == 0


def test_deny_all_empty_list(gate):
    build, resolved, session = gate
    dial = build(talk_policy={"allow": []})
    assert dial(target="sam").status_code == 403
    assert dial(token="a1b2c3d4").status_code == 403
    assert resolved == [] and session.started == 0


def test_allow_any_when_absent(gate):
    build, resolved, session = gate
    dial = build()                            # no talk_policy → allow-any
    assert dial(target="anyone").status_code == 200
    assert resolved == ["anyone"]


def test_number_policy_does_not_gate_talk(gate):
    """A number_policy.allow (E.164) list must NOT gate a Talk dial — Talk is allow-any
    unless talk_policy is present."""
    build, resolved, session = gate
    dial = build(number_policy={"allow": ["+15550001111"]})
    assert dial(target="sam").status_code == 200
    assert resolved == ["sam"]


# -- c4: typed target honesty — token verbatim (no resolve), username resolves --

def test_token_target_used_verbatim_no_resolve(gate):
    build, resolved, session = gate
    dial = build(talk_policy={"allow": ["a1b2c3d4"]})
    r = dial(token="a1b2c3d4")
    assert r.status_code == 200
    assert resolved == []                      # a token is NEVER OCS-resolved
    assert r.json()["token"] == "a1b2c3d4"     # dialed verbatim
    assert session.started == 1


def test_username_target_resolves_once(gate):
    build, resolved, session = gate
    dial = build(talk_policy={"allow": ["sam"]})
    r = dial(target="sam")
    assert r.status_code == 200
    assert resolved == ["sam"]
    assert r.json()["token"] == "resolved-token-for-sam"
