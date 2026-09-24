"""s02 dashboard contract: the fields the rebuilt Agents screen reads.

Ticket 16 landed the outlet axis and the flat `warnings` list. The screen needs
two more things from the same one read, and this module pins them:

  - `slot_warnings[outlet][direction]` -- the same warnings, addressed to the
    slot they belong to, so a fault is painted on the card that is dead instead
    of stacking an unattributed banner over a row of calm-looking Outlets;
  - `outlet_order` -- `profiles.OUTLETS`, so the screen renders the Outlets that
    EXIST rather than a hardcoded pair.

The bar these tests hold: `slot_warnings` and `warnings` come from one pass over
one read, and must never disagree about which slot is dead.
"""
import copy

import pytest
import yaml
from fastapi.testclient import TestClient

from voicecore import profiles
from conftest import SentinelTransport

VALID_DOC = {
    "id": "probe-agent",
    "description": "s02 probe",
    "enabled": True,
    "hermes_profile": "default",
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


def _all_slot_messages(body):
    return [m for by_direction in body["slot_warnings"].values()
            for m in by_direction.values() if m]


# -- the axis the screen renders ---------------------------------------------

def test_outlet_order_is_the_model_not_a_hardcoded_pair(client):
    body = client.get("/api/active").json()
    assert body["outlet_order"] == list(profiles.OUTLETS)
    assert set(body["outlets"]) == set(profiles.OUTLETS)
    assert set(body["slot_warnings"]) == set(profiles.OUTLETS)


def test_a_healthy_configuration_has_no_slot_warnings(client, cfg_dir):
    _agent_on_disk(cfg_dir, doc())
    client.put("/api/active", json={"outlets": {
        "phone": {"inbound": "probe-agent", "outbound": "probe-agent"},
        "talk": {"inbound": "probe-agent", "outbound": "probe-agent"}}})
    body = client.get("/api/active").json()
    assert body["warnings"] == []
    assert body["slot_warnings"] == {
        o: {d: None for d in profiles.ACTIVE_DIRECTIONS} for o in profiles.OUTLETS}


# -- slot attribution: the card that is dead is the card that says so ---------

@pytest.mark.parametrize("outlet", list(profiles.OUTLETS))
@pytest.mark.parametrize("direction", list(profiles.ACTIVE_DIRECTIONS))
def test_a_disabled_agent_warns_on_its_own_slot_and_no_other(
        client, cfg_dir, outlet, direction):
    _agent_on_disk(cfg_dir, doc(enabled=False))
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {outlet: {direction: "probe-agent"}}}))
    body = client.get("/api/active").json()

    message = body["slot_warnings"][outlet][direction]
    assert message is not None and "enabled: false" in message
    assert f"outlets.{outlet}.{direction}" in message
    assert _all_slot_messages(body) == [message]


def test_a_missing_agent_warns_on_its_slot(client, cfg_dir):
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {"phone": {"inbound": "ghost-agent"}}}))
    body = client.get("/api/active").json()
    message = body["slot_warnings"]["phone"]["inbound"]
    assert "does not exist" in message
    assert body["slot_warnings"]["talk"]["inbound"] is None


def test_an_invalid_agent_warns_on_its_slot(client, cfg_dir):
    _agent_on_disk(cfg_dir, doc(providers={"realtime": "no-such-provider"}))
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {"talk": {"outbound": "probe-agent"}}}))
    body = client.get("/api/active").json()
    assert "invalid" in body["slot_warnings"]["talk"]["outbound"]
    assert body["slot_warnings"]["phone"]["outbound"] is None


def test_a_structural_fault_warns_on_its_slot(client, cfg_dir):
    """The parser's own problems carry a field path too, and belong to a slot
    just as much as the health check's do."""
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {"phone": {"inbound": "[bad]"}}}))
    body = client.get("/api/active").json()
    assert "outlets.phone.inbound" in body["slot_warnings"]["phone"]["inbound"]
    assert body["slot_warnings"]["talk"]["inbound"] is None


def test_a_malformed_outlets_map_warns_on_every_slot_it_breaks(client, cfg_dir):
    """A fault above the slots - an `outlets` key that is not a map - kills every
    slot at once. The flat list dedupes it to one message for a human; the slot
    map still marks every slot it kills, because every card is dead."""
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump({"outlets": "not a map"}))
    body = client.get("/api/active").json()
    assert len(body["warnings"]) == 1
    for outlet in profiles.OUTLETS:
        for direction in profiles.ACTIVE_DIRECTIONS:
            assert body["slot_warnings"][outlet][direction] == body["warnings"][0]


def test_every_slot_warning_is_also_in_the_flat_list(client, cfg_dir):
    """The flat list stays the whole truth for any client that ignores the map.
    Divergence here is how a banner and a card end up disagreeing."""
    _agent_on_disk(cfg_dir, doc(enabled=False))
    _agent_on_disk(cfg_dir, doc(id="bad-agent",
                                providers={"realtime": "no-such-provider"}))
    (cfg_dir / "active.yaml").write_text(yaml.safe_dump({"outlets": {
        "phone": {"inbound": "probe-agent", "outbound": "ghost-agent"},
        "talk": {"inbound": "bad-agent", "outbound": "[bad]"}}}))
    body = client.get("/api/active").json()

    slot_messages = _all_slot_messages(body)
    assert len(slot_messages) == 4
    assert set(slot_messages) == set(body["warnings"])


def test_an_unreadable_active_file_stays_page_level(client, cfg_dir):
    """Nothing is attributable when the whole file is gone: the message must not
    be silently dropped by a screen that only reads the slot map."""
    (cfg_dir / "active.yaml").write_text("outlets: [this is not a map\n")
    body = client.get("/api/active").json()
    assert body["warnings"], "a broken file must still be reported"
    assert _all_slot_messages(body) == []


def test_put_active_answers_with_the_slot_map_too(client, cfg_dir):
    """The screen renders the PUT response directly; a response missing the map
    would blank every warning until the next reload."""
    _agent_on_disk(cfg_dir, doc())
    body = client.put("/api/active", json={"outlets": {
        "phone": {"inbound": "probe-agent"}}}).json()
    assert body["outlet_order"] == list(profiles.OUTLETS)
    assert set(body["slot_warnings"]) == set(profiles.OUTLETS)


# -- the roster row ------------------------------------------------------------

def test_agent_rows_carry_the_hermes_profile(client, cfg_dir):
    """ADR 0001: an Agent IS a Hermes profile. Which one is part of recognising
    it on the roster."""
    _agent_on_disk(cfg_dir, doc(hermes_profile="research"))
    _agent_on_disk(cfg_dir, doc(id="no-profile-agent", hermes_profile=None))
    rows = {row["id"]: row for row in client.get("/api/agents").json()}
    assert rows["probe-agent"]["hermes_profile"] == "research"
    assert rows["no-profile-agent"]["hermes_profile"] is None


def test_agent_rows_carry_per_outlet_flags_for_a_split_configuration(client, cfg_dir):
    """What the roster's Outlet chips are drawn from. Two agents, one Outlet
    each: neither may claim the other's."""
    _agent_on_disk(cfg_dir, doc(id="phone-agent"))
    _agent_on_disk(cfg_dir, doc(id="talk-agent"))
    client.put("/api/active", json={"outlets": {
        "phone": {"inbound": "phone-agent", "outbound": "phone-agent"},
        "talk": {"inbound": "talk-agent", "outbound": "talk-agent"}}})
    rows = {row["id"]: row for row in client.get("/api/agents").json()}

    assert rows["phone-agent"]["outlets"] == {
        "phone": {"inbound": True, "outbound": True},
        "talk": {"inbound": False, "outbound": False}}
    assert rows["talk-agent"]["outlets"] == {
        "phone": {"inbound": False, "outbound": False},
        "talk": {"inbound": True, "outbound": True}}
