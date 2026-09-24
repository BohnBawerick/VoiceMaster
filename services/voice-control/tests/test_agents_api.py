"""s3 dashboard API tests: agents CRUD (c02-c07), enable/disable + delete guards
(c08/c09/c34), and the active pointer (c10/c31). All file I/O under a tmp
VOICE_CONFIG_DIR; SentinelTransport proves no endpoint touches the network."""
import copy
import os

import pytest
import yaml
from fastapi.testclient import TestClient

from voicecore import profiles
from conftest import SentinelTransport

VALID_DOC = {
    "id": "probe-agent",
    "description": "s3 probe",
    "enabled": True,
    "pipeline": "realtime",
    "providers": {"realtime": "openai-gpt-realtime"},
    "knobs": {"voice": "marin", "model": "gpt-realtime-probe"},
}


def doc(**overrides):
    d = copy.deepcopy(VALID_DOC)
    d.update(overrides)
    return d


@pytest.fixture
def client(make_app, monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "agents").mkdir()
    return TestClient(make_app(SentinelTransport()))


@pytest.fixture
def cfg_dir(tmp_path):
    return tmp_path


# -- c02/c03/c04: list + read + create/update/delete round-trips ---------------

def test_crud_round_trip(client, cfg_dir):
    assert client.get("/api/agents").json() == []           # honest empty state
    r = client.post("/api/agents", json=doc())
    assert r.status_code == 201
    on_disk = yaml.safe_load((cfg_dir / "agents" / "probe-agent.yaml").read_text())
    assert on_disk == doc()
    assert client.get("/api/agents/probe-agent").json() == doc()

    rows = client.get("/api/agents").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "probe-agent" and row["enabled"] is True
    assert row["pipeline"] == "realtime" and row["valid"] is True
    assert row["outlets"] == {o: {"inbound": False, "outbound": False}
                              for o in profiles.OUTLETS}

    updated = doc(knobs={"voice": "cedar", "model": "gpt-realtime-probe"})
    r = client.put("/api/agents/probe-agent", json=updated)
    assert r.status_code == 200 and "warning" not in r.json()
    assert client.get("/api/agents/probe-agent").json() == updated
    assert yaml.safe_load(
        (cfg_dir / "agents" / "probe-agent.yaml").read_text()) == updated

    assert client.delete("/api/agents/probe-agent").status_code == 200
    assert not (cfg_dir / "agents" / "probe-agent.yaml").exists()
    assert client.get("/api/agents/probe-agent").status_code == 404
    assert client.get("/api/agents/nope").status_code == 404
    assert "detail" in client.get("/api/agents/nope").json()


def test_duplicate_create_conflicts(client):
    assert client.post("/api/agents", json=doc()).status_code == 201
    assert client.post("/api/agents", json=doc()).status_code == 409


# -- c05: server-side validation IS the shared validate_profile ----------------

INVALID_DOCS = [
    ("bad-direction", doc(direction="sideways")),
    ("unknown-key", doc(surprise_key=1)),
    ("bad-vad-type", doc(knobs={"vad": {"silence_ms": "fast"}})),
    ("non-bool-on-call-tools", doc(guardrails={"on_call_tools": "yes"})),
    ("non-list-allow", doc(number_policy={"allow": "+15550001111"})),
    ("non-e164-allow-member", doc(number_policy={"allow": ["15550001111"]})),
    ("non-bool-retain", doc(memory={"retain": "yes"})),
    ("missing-realtime-provider", doc(providers={})),
]


@pytest.mark.parametrize("name,bad", INVALID_DOCS, ids=[c[0] for c in INVALID_DOCS])
def test_validation_is_shared_validate_profile(client, name, bad):
    """The 422 body is EXACTLY the vendored profiles.validate_profile error list —
    message equality against a direct call, not a parallel schema."""
    r = client.post("/api/agents", json=bad)
    assert r.status_code == 422
    direct = profiles.validate_profile(
        bad, profiles.load_registry(), f"agents/{bad['id']}.yaml")
    assert direct, "fixture must actually be invalid"
    assert r.json()["detail"] == direct
    assert not (profiles.config_dir() / "agents" / f"{bad['id']}.yaml").exists()


# -- c06: id traversal matrix — nothing written anywhere -----------------------

BAD_IDS = ["../x", "a/b", "/etc/passwd", "x.yaml", "x.yml", "%2e%2e%2f", "", "  ",
           "UPPER", ".hidden"]


@pytest.mark.parametrize("bad_id", BAD_IDS)
def test_id_traversal_matrix(client, cfg_dir, bad_id, tmp_path):
    def snapshot():
        return sorted(str(p) for p in tmp_path.rglob("*"))

    before = snapshot()
    r = client.post("/api/agents", json=doc(id=bad_id))
    assert r.status_code == 422, f"POST body id {bad_id!r} must be rejected"
    r2 = client.put(f"/api/agents/{bad_id}", json=doc(id=bad_id))
    assert r2.status_code in (404, 405, 422)
    if bad_id.strip():
        r3 = client.get(f"/api/agents/{bad_id}")
        assert r3.status_code in (404, 405, 422)
    else:
        # "/api/agents/" + empty id resolves to the COLLECTION route (a list, not an
        # agent read) — the load-bearing claim is below: no file was created anywhere.
        assert client.get(f"/api/agents/{bad_id}").json() != doc(id=bad_id)
    assert snapshot() == before, f"id {bad_id!r} caused a filesystem write"


# -- c07: atomic writes with injected failure between temp write and replace ---

def test_yaml_writes_are_atomic(client, cfg_dir, monkeypatch):
    assert client.post("/api/agents", json=doc()).status_code == 201
    path = cfg_dir / "agents" / "probe-agent.yaml"
    original = path.read_text()

    real_replace = os.replace

    def boom(src, dst):
        raise OSError("injected crash between temp write and os.replace")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        client.put("/api/agents/probe-agent",
                   json=doc(knobs={"voice": "verse", "model": "m2"}))
    monkeypatch.setattr(os, "replace", real_replace)

    assert path.read_text() == original, "target must be old version, never truncated"
    litter = [p for p in (cfg_dir / "agents").iterdir() if p.suffix == ".tmp"]
    assert litter == [], f"temp litter left behind: {litter}"

    # Successful save: content replaced, still no litter anywhere in the config dir.
    assert client.put("/api/agents/probe-agent",
                      json=doc(knobs={"voice": "verse", "model": "m2"})).status_code == 200
    assert yaml.safe_load(path.read_text())["knobs"]["voice"] == "verse"
    assert [p for p in cfg_dir.rglob("*.tmp")] == []


# -- c08: disable persists; pinned warning when active.yaml names the agent ----

def test_disable_warning_when_active(client, cfg_dir):
    client.post("/api/agents", json=doc())
    assert client.put("/api/active", json={"outlets": {
        "phone": {"inbound": "probe-agent"}}}).status_code == 200
    r = client.put("/api/agents/probe-agent", json=doc(enabled=False))
    assert r.status_code == 200
    warning = r.json().get("warning", "")
    assert "active.yaml" in warning and "will refuse" in warning and "inbound" in warning
    # plain disable (unreferenced) carries no warning
    client.put("/api/active", json={"outlets": {"phone": {"inbound": None}}})
    r2 = client.put("/api/agents/probe-agent", json=doc(enabled=False))
    assert r2.status_code == 200 and "warning" not in r2.json()


# -- c09 + c34b: delete guarded by a request-time reference check --------------

def test_delete_refuses_when_active(client, cfg_dir):
    client.post("/api/agents", json=doc())
    client.put("/api/active", json={"outlets": {"phone": {"outbound": "probe-agent"}}})
    pointer_before = (cfg_dir / "active.yaml").read_text()
    r = client.delete("/api/agents/probe-agent")
    assert r.status_code == 409
    detail = r.json()["detail"]
    for needle in ("probe-agent", "outbound", "active.yaml",
                   "unset it via PUT /api/active"):
        assert needle in detail, f"pinned substring {needle!r} missing: {detail}"
    assert (cfg_dir / "active.yaml").read_text() == pointer_before  # untouched
    assert (cfg_dir / "agents" / "probe-agent.yaml").exists()
    # After unsetting, the same DELETE succeeds.
    client.put("/api/active", json={"outlets": {"phone": {"outbound": None}}})
    assert client.delete("/api/agents/probe-agent").status_code == 200


def test_delete_recheck_at_request_time(client, cfg_dir):
    """The UI confirm is not a lock: a PUT /api/active that lands between confirm and
    DELETE must still yield 409."""
    client.post("/api/agents", json=doc())
    assert client.get("/api/active").json()[
        "outlets"]["phone"]["inbound"] is None                   # confirm-time: free
    client.put("/api/active", json={"outlets": {                 # concurrent re-point
        "phone": {"inbound": "probe-agent"}}})
    assert client.delete("/api/agents/probe-agent").status_code == 409


# -- c10: active-pointer API ---------------------------------------------------

def test_active_api_set_unset_refuse(client, cfg_dir):
    empty = client.get("/api/active").json()["outlets"]
    assert empty["phone"] == {"inbound": None, "outbound": None}
    assert empty["talk"] == {"inbound": None, "outbound": None}

    client.post("/api/agents", json=doc())
    r = client.put("/api/active", json={"outlets": {
        "phone": {"inbound": "probe-agent"}, "talk": {"inbound": "probe-agent"}}})
    assert r.status_code == 200
    assert r.json()["outlets"]["phone"]["inbound"] == "probe-agent"
    on_disk = yaml.safe_load((cfg_dir / "active.yaml").read_text())
    assert on_disk == {"outlets": {
        "phone": {"inbound": "probe-agent", "outbound": None},
        "talk": {"inbound": "probe-agent", "outbound": None}}}

    assert client.put("/api/active", json={"outlets": {
        "phone": {"outbound": "ghost"}}}).status_code == 422       # missing agent
    client.put("/api/agents/probe-agent", json=doc(enabled=False))
    assert client.put("/api/active", json={"outlets": {
        "phone": {"outbound": "probe-agent"}}}).status_code == 422  # disabled
    r = client.put("/api/active", json={"outlets": {                # unset always ok
        "phone": {"inbound": None}}})
    assert r.status_code == 200 and r.json()["outlets"]["phone"]["inbound"] is None

    # GET reflects out-of-band file edits (no cache).
    (cfg_dir / "active.yaml").write_text(
        "outlets:\n  phone:\n    inbound: probe-agent\n"
        "  talk:\n    inbound: probe-agent\n")
    body = client.get("/api/active").json()
    assert body["outlets"]["phone"]["inbound"] == "probe-agent"
    assert body["outlets"]["talk"]["inbound"] == "probe-agent"


# -- c31: malformed/partial active.yaml normalized per direction ---------------

def test_partial_active_yaml_fixtures(client, cfg_dir):
    client.post("/api/agents", json=doc())
    fixtures = [
        ("outlets:\n  phone:\n    inbound: probe-agent\n",
         {"inbound": "probe-agent", "outbound": None}, 0),
        ("outlets:\n  phone:\n    inbound: probe-agent\nmystery: 7\n",
         {"inbound": "probe-agent", "outbound": None}, 0),
        ("outlets:\n  phone:\n    inbound: [not, a, string]\n"
         "    outbound: probe-agent\n",
         {"inbound": None, "outbound": "probe-agent"}, 1),
    ]
    for raw, expected, n_warnings in fixtures:
        (cfg_dir / "active.yaml").write_text(raw)
        body = client.get("/api/active").json()
        assert body["outlets"]["phone"] == expected, raw
        assert len(body["warnings"]) == n_warnings, raw
        if n_warnings:
            # the warning names the SLOT, outlet included - never a bare direction
            assert "outlets.phone.inbound" in body["warnings"][0]

    # Unknown keys are dropped on the next write, which lands the CANONICAL shape.
    (cfg_dir / "active.yaml").write_text(
        "outlets:\n  phone:\n    inbound: probe-agent\nmystery: 7\n")
    client.put("/api/active", json={"outlets": {"phone": {"outbound": "probe-agent"}}})
    on_disk = yaml.safe_load((cfg_dir / "active.yaml").read_text())
    assert set(on_disk) == {"outlets"}
    assert set(on_disk["outlets"]) == {"phone", "talk"}


# -- c34a: out-of-band invalid agent file stays honest -------------------------

def test_out_of_band_invalid_agent(client, cfg_dir):
    client.post("/api/agents", json=doc())
    (cfg_dir / "agents" / "broken.yaml").write_text("id: broken\npipeline: nope\n")
    r = client.get("/api/agents")
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()}
    assert rows["probe-agent"]["valid"] is True          # others unaffected
    assert rows["broken"]["valid"] is False
    assert any("pipeline" in e for e in rows["broken"]["errors"])
    r2 = client.get("/api/agents/broken")
    assert r2.status_code == 422                          # never a 500
    assert any("pipeline" in e for e in r2.json()["detail"])

    (cfg_dir / "agents" / "garbage.yaml").write_text("id: [unclosed\n  ]]]junk: {")
    r3 = client.get("/api/agents")
    assert r3.status_code == 200
    garbage = [row for row in r3.json() if row["id"] == "garbage"]
    assert garbage and garbage[0]["valid"] is False


# -- c23: VAD numeric bounds are load-bearing server-side ----------------------

VAD_BOUNDS = [
    ("threshold-over", {"vad": {"threshold": 7}}, "knobs.vad.threshold"),
    ("threshold-negative", {"vad": {"threshold": -0.1}}, "knobs.vad.threshold"),
    ("silence-over", {"vad": {"silence_ms": 999999}}, "knobs.vad.silence_ms"),
    ("prefix-negative", {"vad": {"prefix_padding_ms": -1}},
     "knobs.vad.prefix_padding_ms"),
    ("silence-float", {"vad": {"silence_ms": 1.5}}, "knobs.vad.silence_ms"),
]


@pytest.mark.parametrize("name,knobs,field", VAD_BOUNDS, ids=[c[0] for c in VAD_BOUNDS])
def test_vad_bounds_rejected(client, name, knobs, field):
    r = client.post("/api/agents", json=doc(knobs=knobs))
    assert r.status_code == 422
    assert any(field in e for e in r.json()["detail"]), r.json()

    bypass = client.put("/api/agents/probe-agent", json=doc(knobs=knobs))
    assert bypass.status_code in (404, 422)   # never a silent save

    # In-range values round-trip.
    ok = client.post("/api/agents", json=doc(
        id="vad-ok", knobs={"vad": {"threshold": 0.6, "silence_ms": 400,
                                    "prefix_padding_ms": 200}}))
    assert ok.status_code == 201
    assert client.get("/api/agents/vad-ok").json()["knobs"]["vad"]["threshold"] == 0.6
