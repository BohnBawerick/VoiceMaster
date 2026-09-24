"""s16 dashboard tests: the outlet axis on the active-pointer API, the agents
listing, and the two fire arms.

The shared resolution matrix lives in the phone suite (voice/tests/test_outlet_axis.py).
What lives HERE is the dashboard contract: the canonical outlet write shape as the
ONLY write shape (s17), per-outlet validation refusals, and the fire arms resolving
the right outlet.
"""
import copy

import pytest
import yaml
from fastapi.testclient import TestClient

from voicecore import profiles
from conftest import SentinelTransport

VALID_DOC = {
    "id": "probe-agent",
    "description": "s16 probe",
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


def _agent_on_disk(cfg_dir, d):
    (cfg_dir / "agents" / f"{d['id']}.yaml").write_text(yaml.safe_dump(d))


# -- GET/PUT round trips in the outlet shape ----------------------------------

def test_put_outlet_shape_round_trip(client, cfg_dir):
    _agent_on_disk(cfg_dir, doc())
    r = client.put("/api/active", json={"outlets": {
        "phone": {"inbound": "probe-agent", "outbound": None},
        "talk": {"inbound": None, "outbound": "probe-agent"}}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outlets"] == {
        "phone": {"inbound": "probe-agent", "outbound": None},
        "talk": {"inbound": None, "outbound": "probe-agent"}}
    # s17: no flat mirror of the phone outlet rides along - on a split
    # configuration it was a wrong answer wearing the shape of a right one.
    assert "inbound" not in body and "outbound" not in body
    on_disk = yaml.safe_load((cfg_dir / "active.yaml").read_text())
    assert on_disk == {"outlets": {
        "phone": {"inbound": "probe-agent", "outbound": None},
        "talk": {"inbound": None, "outbound": "probe-agent"}}}
    assert client.get("/api/active").json()["outlets"] == body["outlets"]


def test_outlet_put_is_partial(client, cfg_dir):
    """A partial outlet write leaves every unmentioned slot alone."""
    _agent_on_disk(cfg_dir, doc())
    client.put("/api/active", json={"outlets": {
        "phone": {"inbound": "probe-agent"},
        "talk": {"outbound": "probe-agent"}}})
    r = client.put("/api/active", json={"outlets": {"talk": {"outbound": None}}})
    assert r.status_code == 200, r.text
    assert r.json()["outlets"] == {
        "phone": {"inbound": "probe-agent", "outbound": None},
        "talk": {"inbound": None, "outbound": None}}


# -- s17: the flat write has nowhere to land ----------------------------------
#
# The hazard these three close is not hypothetical: it is the exact request the
# deleted ``static-legacy/agents.js`` sent from its set/unset buttons (ticket 15
# removed that screen; ticket 17 had already removed the shape). It named a
# direction and no Outlet, so the API applied it to every Outlet at once and
# answered 200 - one click, a split silently collapsed, nothing warned. The
# shape is refused now, so no future screen can reintroduce the click.

OLD_SCREEN_WRITE = {"inbound": "probe-agent"}     # verbatim: agents.js:313, now deleted


def test_the_old_screens_write_is_refused_and_the_split_survives(client, cfg_dir):
    """The whole ticket, in one test: set up the split the old screen used to
    destroy, send the old screen's exact request, and show that nothing moved."""
    _agent_on_disk(cfg_dir, doc())
    _agent_on_disk(cfg_dir, doc(id="talk-agent"))
    client.put("/api/active", json={"outlets": {
        "phone": {"inbound": "probe-agent"},
        "talk": {"inbound": "talk-agent"}}})
    before = (cfg_dir / "active.yaml").read_bytes()

    r = client.put("/api/active", json=OLD_SCREEN_WRITE)

    assert r.status_code == 422, r.text
    # the refusal has to teach the shape, or the operator just retries it
    detail = " ".join(r.json()["detail"])
    assert "outlets" in detail and "phone" in detail and "talk" in detail
    assert (cfg_dir / "active.yaml").read_bytes() == before   # not even rewritten
    assert client.get("/api/active").json()["outlets"] == {
        "phone": {"inbound": "probe-agent", "outbound": None},
        "talk": {"inbound": "talk-agent", "outbound": None}}


def test_a_flat_key_beside_an_outlets_map_is_refused_too(client, cfg_dir):
    """The half-migrated client: sends the new shape and keeps the old key for
    good measure. Accepting the body and dropping the key would tell it the flat
    write worked."""
    _agent_on_disk(cfg_dir, doc())
    r = client.put("/api/active", json={
        "inbound": "probe-agent",
        "outlets": {"phone": {"inbound": "probe-agent"}}})
    assert r.status_code == 422
    assert not (cfg_dir / "active.yaml").exists()


def test_a_flat_file_on_disk_is_refused_rather_than_routed(client, cfg_dir):
    """The other direction: a file already carrying the flat shape. It used to
    route both Outlets; now GET reports it as unreadable owner intent. It stays
    a 200 - `GET /api/active` is what the owner looks at when something is
    wrong (A1) - with the fault in `warnings`."""
    _agent_on_disk(cfg_dir, doc())
    (cfg_dir / "active.yaml").write_text(
        "inbound: probe-agent\noutbound: probe-agent\n")
    r = client.get("/api/active")
    assert r.status_code == 200
    body = r.json()
    assert body["outlets"] == {
        "phone": {"inbound": None, "outbound": None},
        "talk": {"inbound": None, "outbound": None}}
    assert len(body["warnings"]) == 1
    assert "outlets" in body["warnings"][0]
    # page-level, not attributed to a slot: no slot owns a keyless assignment
    for outlet in profiles.OUTLETS:
        for direction in profiles.ACTIVE_DIRECTIONS:
            assert body["slot_warnings"][outlet][direction] is None


# -- validation refusals, per outlet ------------------------------------------

def test_outlet_put_validation_refusals(client, cfg_dir):
    _agent_on_disk(cfg_dir, doc())
    assert client.put("/api/active", json={"outlets": {
        "sms": {"inbound": "probe-agent"}}}).status_code == 422   # unknown outlet
    assert client.put("/api/active", json={"outlets": {
        "phone": {"sideways": "probe-agent"}}}).status_code == 422  # unknown dir
    assert client.put("/api/active", json={"outlets": {
        "phone": {"inbound": "ghost"}}}).status_code == 422        # missing agent
    assert client.put("/api/active", json={"outlets": {
        "phone": None}}).status_code == 422                        # null entry: no-op
    client.put("/api/agents/probe-agent", json=doc(enabled=False))
    assert client.put("/api/active", json={"outlets": {
        "talk": {"inbound": "probe-agent"}}}).status_code == 422   # disabled
    assert client.put("/api/active", json={"outlets": {
        "talk": {"inbound": None}}}).status_code == 200            # unset always ok


def test_outlet_put_refuses_inbound_cascade_on_any_outlet(client, cfg_dir):
    """Cascade is outbound-only on EVERY outlet - storing it as a talk inbound
    answer is refused exactly like the phone one."""
    cascade = doc(id="cas-agent", pipeline="cascade",
                  providers={"stt": "deepgram", "llm": "nvidia-nemotron",
                             "tts": "elevenlabs"})
    _agent_on_disk(cfg_dir, cascade)
    r = client.put("/api/active", json={"outlets": {
        "talk": {"inbound": "cas-agent"}}})
    assert r.status_code == 422
    assert "outbound-only" in yaml.safe_dump(r.json())
    assert client.put("/api/active", json={"outlets": {
        "talk": {"outbound": "cas-agent"}}}).status_code == 200


# -- agents listing carries the per-outlet truth ------------------------------

def test_agents_listing_carries_outlet_flags(client, cfg_dir):
    _agent_on_disk(cfg_dir, doc())
    _agent_on_disk(cfg_dir, doc(id="talk-agent"))
    client.put("/api/active", json={"outlets": {
        "phone": {"inbound": "probe-agent"},
        "talk": {"inbound": "talk-agent"}}})
    rows = {r["id"]: r for r in client.get("/api/agents").json()}
    assert rows["probe-agent"]["outlets"] == {
        "phone": {"inbound": True, "outbound": False},
        "talk": {"inbound": False, "outbound": False}}
    assert rows["talk-agent"]["outlets"] == {
        "phone": {"inbound": False, "outbound": False},
        "talk": {"inbound": True, "outbound": False}}
    # s17: no flattened `active` flag rides along. It mirrored the PHONE outlet,
    # so on this very configuration it reported talk-agent as active nowhere.
    assert "active" not in rows["probe-agent"]
    assert "active" not in rows["talk-agent"]


def test_disable_warning_names_the_outlet(client, cfg_dir):
    _agent_on_disk(cfg_dir, doc())
    client.put("/api/active", json={"outlets": {
        "talk": {"inbound": "probe-agent"}}})
    r = client.put("/api/agents/probe-agent", json=doc(enabled=False))
    warning = r.json().get("warning", "")
    assert "active.yaml" in warning and "will refuse" in warning
    assert "inbound (talk)" in warning


def test_delete_refusal_names_the_outlet(client, cfg_dir):
    _agent_on_disk(cfg_dir, doc())
    client.put("/api/active", json={"outlets": {
        "phone": {"outbound": "probe-agent"}}})
    r = client.delete("/api/agents/probe-agent")
    assert r.status_code == 409
    assert "outbound (phone)" in r.json()["detail"]


def test_new_shape_warning_names_the_field_path(client, cfg_dir):
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {"phone": {"inbound": "[bad]"}, "talk": {"inbound": None}}}))
    body = client.get("/api/active").json()
    assert len(body["warnings"]) == 1
    assert "outlets.phone.inbound" in body["warnings"][0]
    assert body["outlets"]["phone"]["inbound"] is None
    assert body["outlets"]["talk"]["inbound"] is None


# -- stale-after-storage visibility (s16 addendum A) -------------------------

def test_stale_slot_agent_file_removed_out_of_band_is_warned(client, cfg_dir):
    """The realistic outage route: the pointer was stored fine, then the agent file
    disappears (out of band). GET /api/active must name the slot, never silently."""
    _agent_on_disk(cfg_dir, doc())
    client.put("/api/active", json={"outlets": {
        "phone": {"inbound": "probe-agent", "outbound": "probe-agent"}}})
    (cfg_dir / "agents" / "probe-agent.yaml").unlink()
    body = client.get("/api/active").json()
    assert len(body["warnings"]) == 2  # one per slot naming the missing agent
    for warning in body["warnings"]:
        assert "probe-agent" in warning and "does not exist" in warning
    assert "outlets.phone.inbound" in body["warnings"][0]
    assert "outlets.phone.outbound" in body["warnings"][1]


def test_stale_slot_disabled_agent_is_warned_persistently(client, cfg_dir):
    """The dashboard's own disable button: PUT /api/agents/{id} with enabled: false
    lands 200, and from then on GET /api/active carries the warning permanently."""
    _agent_on_disk(cfg_dir, doc())
    client.put("/api/active", json={"outlets": {
        "talk": {"inbound": "probe-agent"}}})
    r = client.put("/api/agents/probe-agent", json=doc(enabled=False))
    assert r.status_code == 200
    body = client.get("/api/active").json()
    assert len(body["warnings"]) == 1
    assert "outlets.talk.inbound" in body["warnings"][0]
    assert "enabled: false" in body["warnings"][0]


def test_stale_slot_invalid_agent_is_warned(client, cfg_dir):
    _agent_on_disk(cfg_dir, doc())
    client.put("/api/active", json={"outlets": {
        "phone": {"outbound": "probe-agent"}}})
    (cfg_dir / "agents" / "probe-agent.yaml").write_text(
        "id: probe-agent\npipeline: nope\n")
    body = client.get("/api/active").json()
    assert len(body["warnings"]) == 1
    assert "outlets.phone.outbound" in body["warnings"][0]
    assert "invalid" in body["warnings"][0]


def test_stale_slot_cascade_edited_onto_an_inbound_slot_is_warned(client, cfg_dir):
    """G2: the dashboard REFUSES to store cascade on an inbound slot, so the only way
    into this state is an edit after storage - which is exactly the state that used to
    be invisible. A cascade agent on an inbound slot is a hard bridge refusal, so
    `GET /api/active` must name it."""
    _agent_on_disk(cfg_dir, doc())
    client.put("/api/active", json={"outlets": {"talk": {"inbound": "probe-agent"}}})
    _agent_on_disk(cfg_dir, doc(pipeline="cascade",
                                providers={"stt": "deepgram", "llm": "nvidia-nemotron",
                                           "tts": "elevenlabs"}))
    body = client.get("/api/active").json()
    assert len(body["warnings"]) == 1
    assert "outlets.talk.inbound" in body["warnings"][0]
    assert "outbound-only" in body["warnings"][0]


def test_cascade_on_an_outbound_slot_is_not_warned(client, cfg_dir):
    """The other half of the same rule: cascade OUTBOUND activates on this host, so it
    is healthy and must not be warned about. Without this, the check above could pass
    by warning on cascade unconditionally."""
    _agent_on_disk(cfg_dir, doc())
    client.put("/api/active", json={"outlets": {"phone": {"outbound": "probe-agent"}}})
    _agent_on_disk(cfg_dir, doc(pipeline="cascade",
                                providers={"stt": "deepgram", "llm": "nvidia-nemotron",
                                           "tts": "elevenlabs"}))
    assert client.get("/api/active").json()["warnings"] == []


def test_stale_slot_edited_onto_an_unwired_provider_is_warned(client, cfg_dir):
    """The second G2 route: a registry-VALID realtime provider that is not the one
    wired here. It passes schema validation, so only the activation check catches it."""
    _agent_on_disk(cfg_dir, doc())
    client.put("/api/active", json={"outlets": {"phone": {"outbound": "probe-agent"}}})
    _agent_on_disk(cfg_dir, doc(providers={"realtime": "google-gemini-live"}))
    body = client.get("/api/active").json()
    assert len(body["warnings"]) == 1
    assert "outlets.phone.outbound" in body["warnings"][0]
    assert "not implemented" in body["warnings"][0]


def test_healthy_configuration_carries_no_warnings(client, cfg_dir):
    _agent_on_disk(cfg_dir, doc())
    client.put("/api/active", json={"outlets": {
        "phone": {"inbound": "probe-agent", "outbound": "probe-agent"}}})
    body = client.get("/api/active").json()
    assert body["warnings"] == []


def test_get_active_survives_a_broken_configuration(client, cfg_dir):
    """Constraint A1: the endpoint the owner looks at must never die because a
    slot cannot be resolved - even when the whole agents dir is gone."""
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump({"outlets": {
        "phone": {"inbound": "ghost-agent", "outbound": None},
        "talk": {"inbound": None, "outbound": None}}}))
    r = client.get("/api/active")
    assert r.status_code == 200
    body = r.json()
    assert body["outlets"]["phone"]["inbound"] == "ghost-agent"
    assert len(body["warnings"]) == 1
    assert "outlets.phone.inbound" in body["warnings"][0]
    assert "does not exist" in body["warnings"][0]


# -- the agents DIRECTORY, and resolving the way the bridges resolve -----------
#
# Two silences older than the outlet axis (findings G1 and G3 of the independent
# axis check). Both are the same class the per-slot health check was built for: a
# configuration that is broken for the bridges and healthy on the dashboard.
#
# Every test in this section asserts BOTH halves - what GET /api/active says and
# what the real resolution the bridges use does with the same directory. Asserting
# only the warning would let the check drift back out of agreement with the thing
# it reports on, which is the whole of G3.


def _bridge(outlet, direction="inbound"):
    """Resolve one slot's ASSIGNED configuration exactly as that Outlet's bridge
    does. Returns the agent id, None for an unassigned slot, or the ProfileError
    message.

    Ticket 08 wraps this at the bridges' answering call sites: a raised error there
    is answered from the last-known-good snapshot when one exists. That is a
    separate layer and it never changes what resolves here - which is exactly what
    the dashboard reports on, and why a fallback answer does not make the dashboard
    look healthy."""
    try:
        active = profiles.load_effective_profile(direction, outlet=outlet)
    except profiles.ProfileError as exc:
        return f"ERR {exc}"
    return None if active is None else active.agent_id


def test_g1_one_unparseable_stray_file_is_named(client, cfg_dir):
    """G1: `_activate` parses the WHOLE agents directory to resolve any one agent,
    so a stray unparseable file that no slot names takes every Outlet down. The
    per-slot check only ever looked at the slot's own agent and saw nothing."""
    _agent_on_disk(cfg_dir, doc(id="phone-agent"))
    _agent_on_disk(cfg_dir, doc(id="talk-agent"))
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump({"outlets": {
        "phone": {"inbound": "phone-agent", "outbound": None},
        "talk": {"inbound": "talk-agent", "outbound": None}}}))
    (cfg_dir / "agents" / "junk.yaml").write_text("key: [unclosed\n")

    assert "ERR" in _bridge("phone") and "ERR" in _bridge("talk")  # both dead
    body = client.get("/api/active").json()
    assert len(body["warnings"]) == 1
    warning = body["warnings"][0]
    assert "junk.yaml" in warning          # WHICH file
    assert "invalid YAML" in warning       # and WHY
    # Every filled slot is dead, so every filled card says so; the empty ones do not.
    assert body["slot_warnings"]["phone"]["inbound"] == warning
    assert body["slot_warnings"]["talk"]["inbound"] == warning
    assert body["slot_warnings"]["phone"]["outbound"] is None


def test_g1_the_warning_holds_when_ticket_08_answers_from_a_snapshot(
        client, cfg_dir, monkeypatch, tmp_path):
    """The G1 warning is the ONLY signal on a dashboard that ticket 08 deliberately
    does not quieten. With a last-known-good snapshot recorded, a stray file does not
    make the Outlet go dead - it answers from the snapshot, serving a configuration
    nobody assigned. The warning must still be there, and must not claim a refusal
    that is not happening."""
    from voicecore import lkg
    events = tmp_path / "events"
    events.mkdir()
    monkeypatch.setenv("VOICE_LKG_DIR", str(events))
    monkeypatch.setenv("VOICE_FALLBACK_LOG_PATH", str(events / "fallback.jsonl"))
    _agent_on_disk(cfg_dir, doc(id="phone-agent"))
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {"phone": {"inbound": "phone-agent"}}}))
    lkg.record("phone", "inbound", profiles.load_effective_profile("inbound", "phone"),
               call_id="c1")

    (cfg_dir / "agents" / "junk.yaml").write_text("key: [unclosed\n")

    # The assigned configuration is gone for every Outlet...
    assert "ERR" in _bridge("phone")
    # ...but ticket 08 answers anyway, from a snapshot nobody assigned.
    assert lkg.resolve("inbound", "phone").agent_id == "phone-agent"

    warning = client.get("/api/active").json()["warnings"][0]
    assert "junk.yaml" in warning
    assert "last-known-good" in warning     # says what really happens now
    assert "EVERY call on EVERY outlet will refuse" not in warning   # and not this


def test_g1_a_duplicate_agent_id_is_named(client, cfg_dir):
    """The second G1 shape: two files carrying the same `id:`. Neither file is
    broken on its own; the directory is."""
    _agent_on_disk(cfg_dir, doc(id="phone-agent"))
    (cfg_dir / "agents" / "copy.yaml").write_text(
        yaml.safe_dump(doc(id="phone-agent")))
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {"phone": {"inbound": "phone-agent"}}}))

    assert "duplicate agent id" in _bridge("phone")
    warnings = client.get("/api/active").json()["warnings"]
    assert len(warnings) == 1
    assert "duplicate agent id 'phone-agent'" in warnings[0]
    assert "copy.yaml" in warnings[0] and "phone-agent.yaml" in warnings[0]


def test_g1_a_file_that_is_not_a_map_is_named(client, cfg_dir):
    """The third refusal `_scan_agents_dir` makes. Used to read as a per-slot
    "not a valid agent document" on the one slot, which named the wrong file and
    understated the blast radius."""
    _agent_on_disk(cfg_dir, doc(id="phone-agent"))
    (cfg_dir / "agents" / "notamap.yaml").write_text("- a\n- b\n")
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {"phone": {"inbound": "phone-agent"}}}))

    assert "ERR" in _bridge("phone")
    warnings = client.get("/api/active").json()["warnings"]
    assert len(warnings) == 1
    assert "notamap.yaml" in warnings[0] and "not a YAML map" in warnings[0]


def test_g1_a_healthy_directory_carries_no_directory_warning(client, cfg_dir):
    """The other side of G1: two well-formed files, two Outlets, no warning at all.
    Without this, the probe could pass every test above by warning unconditionally."""
    _agent_on_disk(cfg_dir, doc(id="phone-agent"))
    _agent_on_disk(cfg_dir, doc(id="talk-agent"))
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump({"outlets": {
        "phone": {"inbound": "phone-agent", "outbound": None},
        "talk": {"inbound": "talk-agent", "outbound": None}}}))

    assert _bridge("phone") == "phone-agent" and _bridge("talk") == "talk-agent"
    body = client.get("/api/active").json()
    assert body["warnings"] == []
    assert body["slot_warnings"] == {o: {d: None for d in ("inbound", "outbound")}
                                     for o in ("phone", "talk")}


def test_g1_an_empty_agents_directory_is_not_a_directory_fault(client, cfg_dir):
    """An empty (or absent) agents directory is not reported as a directory fault:
    a slot naming an agent already says so on its own card, and with nothing
    assigned there is nothing broken. Guards the probe against being noise on a
    fresh install."""
    assert client.get("/api/active").json()["warnings"] == []
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {"phone": {"inbound": "ghost-agent"}}}))
    warnings = client.get("/api/active").json()["warnings"]
    assert len(warnings) == 1                      # the slot's own, not the dir's
    assert "outlets.phone.inbound" in warnings[0]
    assert "does not exist" in warnings[0]


def test_g3_an_agent_stored_as_yml_is_not_a_false_alarm(client, cfg_dir):
    """G3, direction one: `_scan_agents_dir` accepts *.yml, so the bridge resolves
    this fine. Resolving by filename (`agents/<id>.yaml`) reported a healthy slot
    as missing."""
    (cfg_dir / "agents" / "phone-agent.yml").write_text(
        yaml.safe_dump(doc(id="phone-agent")))
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {"phone": {"inbound": "phone-agent"}}}))

    assert _bridge("phone") == "phone-agent"       # the bridge is happy
    body = client.get("/api/active").json()
    assert body["warnings"] == []                  # so the dashboard must be too
    assert body["slot_warnings"]["phone"]["inbound"] is None


def test_g3_an_agent_filed_under_another_name_is_not_a_false_alarm(client, cfg_dir):
    """G3, direction one again: the id a slot names is the `id:` INSIDE the
    document, not the filename. `wrongname.yaml` carrying `id: real-agent` resolves
    as `real-agent` for the bridges."""
    (cfg_dir / "agents" / "wrongname.yaml").write_text(
        yaml.safe_dump(doc(id="real-agent")))
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {"talk": {"outbound": "real-agent"}}}))

    assert _bridge("talk", "outbound") == "real-agent"
    body = client.get("/api/active").json()
    assert body["warnings"] == []
    assert body["slot_warnings"]["talk"]["outbound"] is None


def test_g3_a_warning_names_the_file_the_agent_really_came_from(client, cfg_dir):
    """F-01: every filename in a warning must be a file that is actually there.

    The activation label used to be rebuilt from the agent id (`agents/<id>.yaml`).
    That was unreachable while the check resolved by filename - a differently-named
    file failed the `is_file()` test first - and resolving by id (G3) made it
    reachable in exactly the case where it is wrong: an agent found under one name
    and labelled with another, shown to someone hunting for the broken file.

    Asserted against the directory listing rather than against a literal, so it
    cannot pass by agreeing with a hard-coded name."""
    (cfg_dir / "agents" / "totally-different-filename.yaml").write_text(
        yaml.safe_dump(doc(id="phone-agent",
                           providers={"realtime": "google-gemini-live"})))
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {"phone": {"inbound": "phone-agent"}}}))

    assert "not implemented" in _bridge("phone")        # a real refusal, correctly seen
    warnings = client.get("/api/active").json()["warnings"]
    assert len(warnings) == 1
    assert "not implemented" in warnings[0]

    on_disk = [f.name for f in (cfg_dir / "agents").iterdir()]
    assert on_disk == ["totally-different-filename.yaml"]
    for name in on_disk:
        assert name in warnings[0], f"warning does not name the real file: {warnings[0]}"
    assert "phone-agent.yaml" not in warnings[0]        # the file it is NOT


def test_g3_a_file_whose_id_does_not_match_its_name_is_warned(client, cfg_dir):
    """G3, direction two - the silent one. `phone-agent.yaml` carrying `id: other`
    means there IS no agent `phone-agent`: the bridge refuses, and resolving by
    filename found the file and reported no warning at all."""
    (cfg_dir / "agents" / "phone-agent.yaml").write_text(
        yaml.safe_dump(doc(id="other")))
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {"phone": {"inbound": "phone-agent"}}}))

    assert "not found" in _bridge("phone")
    body = client.get("/api/active").json()
    warnings = body["warnings"]
    assert len(warnings) == 1
    assert "outlets.phone.inbound" in warnings[0]
    assert "agent 'phone-agent' does not exist" in warnings[0]
    assert body["slot_warnings"]["phone"]["inbound"] == warnings[0]


def test_get_active_survives_a_hostile_agents_directory(client, cfg_dir):
    """Constraint A1 again, now for the directory probe: it reads every file in
    `agents/`, so it is a new way for `GET /api/active` to die. Each of these
    directories must degrade to a 200 with a warning, never an error."""
    agents = cfg_dir / "agents"
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump({"outlets": {
        "phone": {"inbound": "phone-agent", "outbound": "phone-agent"},
        "talk": {"inbound": "phone-agent", "outbound": None}}}))

    def only(*files):
        for f in agents.iterdir():
            if f.is_symlink():          # chmod would follow it into nowhere
                f.unlink()
            elif f.is_dir():
                f.rmdir()
            else:
                f.chmod(0o644)
                f.unlink()
        _agent_on_disk(cfg_dir, doc(id="phone-agent"))
        for name, body in files:
            (agents / name).write_text(body)
        return agents

    cases = {
        "unparseable": lambda: only(("a.yaml", "key: [unclosed\n")),
        "not a map": lambda: only(("a.yaml", "- a\n- b\n")),
        "a bare scalar": lambda: only(("a.yaml", "just-a-string\n")),
        "empty file": lambda: only(("a.yaml", "")),
        "duplicate id": lambda: only(("a.yaml", yaml.safe_dump(doc(id="phone-agent")))),
        "yml duplicate of a yaml": lambda: only(
            ("phone-agent.yml", yaml.safe_dump(doc(id="phone-agent")))),
        "a NUL byte": lambda: only(("a.yaml", "id: a\x00b\n")),
        "an unreadable file": lambda: (only(("a.yaml", "id: a\n")),
                                       (agents / "a.yaml").chmod(0o000), agents)[-1],
        "a 3 MB file": lambda: only(("a.yaml", "# " + "x" * 3_000_000 + "\n")),
        "an alias bomb": lambda: only(("a.yaml", "a: &a [1,1]\nb: &b [*a,*a]\n"
                                                 "c: &c [*b,*b]\nd: [*c,*c]\n")),
        "a directory named like an agent": lambda: (only(), (agents / "d.yaml").mkdir(),
                                                    agents)[-1],
        "a dangling symlink": lambda: (only(),
                                       (agents / "s.yaml").symlink_to("nowhere.yaml"),
                                       agents)[-1],
    }
    for name, build in cases.items():
        build()
        r = client.get("/api/active")
        assert r.status_code == 200, f"{name}: {r.status_code} {r.text}"
        assert isinstance(r.json()["warnings"], list), name
    only()                              # leave the tmp dir removable


