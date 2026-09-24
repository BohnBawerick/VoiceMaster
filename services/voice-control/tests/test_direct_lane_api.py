"""VC24 on the dashboard API: choosing what you talk to, which Hermes profile it is, and
the two narrow writes the Settings screen makes.

The bar is the same one test_settings_api.py holds: a write is judged by what the
BRIDGES would then do, so every accepted change is re-read through the resolver the
bridges use (``load_effective_profile``), and every refused one is checked to have left
the file byte-identical.
"""
import json

import pytest
import yaml

from voicecore import profiles
from test_settings_api import client, config_dir  # noqa: F401 - fixtures

DIRECT = {"stt": "deepgram", "llm": "hermes-agent", "tts": "elevenlabs"}
VENDOR = {"stt": "deepgram", "llm": "openrouter", "tts": "elevenlabs"}
TO_DIRECT = {"pipeline": "cascade", "providers": dict(DIRECT, realtime=None),
             "knobs": {"voice": "el-voice-1"}}
TO_VENDOR = {"pipeline": "cascade", "providers": dict(VENDOR, realtime=None)}


@pytest.fixture
def hermes(config_dir, tmp_path, monkeypatch):  # noqa: F811
    """A profiles directory (vega complete, halfway still being created) and a registry
    in which only vega has a running gateway."""
    home = tmp_path / "hermes-profiles"
    for name, complete in (("vega", True), ("scout", True), ("halfway", False)):
        (home / name).mkdir(parents=True)
        if complete:
            (home / name / "config.yaml").write_text("model: x\n")
        else:
            (home / name / ".incomplete").write_text("")
    (config_dir.config_dir / "gateways").mkdir()
    (config_dir.config_dir / "gateways" / "gateways.json").write_text(json.dumps(
        {"profiles": {"vega": {"status": "ok", "gateway_url": "http://127.0.0.1:18791"},
                      "scout": {"status": "crashed",
                                       "gateway_url": "http://127.0.0.1:18790"}}}))
    monkeypatch.setenv("HERMES_PROFILES_DIR", str(home))
    monkeypatch.setenv("HERMES_GATEWAY_URL", "http://hermes:18789")
    monkeypatch.delenv("HERMES_PROFILE_GATEWAY_URLS", raising=False)
    monkeypatch.delenv(profiles.ENV_HERMES_DIRECT, raising=False)
    return config_dir


# ------------------------------------------------------------- what you talk to --

async def test_the_catalog_offers_hermes_directly_and_says_it_is_proven(client, hermes):
    async with client as c:
        body = (await c.get("/api/settings")).json()
    types = {t["id"]: t for t in body["agent_types"]}
    assert list(types) == ["hermes-direct", "realtime"]
    # Ticket 21: ElevenLabs hears by default; Deepgram stays a choice on the type.
    assert types["hermes-direct"]["providers"] == dict(DIRECT, stt="elevenlabs-scribe")
    assert types["hermes-direct"]["stt_options"] == ["elevenlabs-scribe", "deepgram"]
    # Review S5: the voice is a choice on the type too, ElevenLabs first.
    assert types["hermes-direct"]["tts_options"] == ["elevenlabs", "deepgram-aura"]
    assert types["hermes-direct"]["pipeline"] == "cascade"
    # VC7 + the honesty bar: selectable, and labelled for what it is. Real phone calls
    # have carried this lane (O5 closed), and the label names them and says Talk has not.
    assert types["hermes-direct"]["proven"] is True
    assert "2026-09-24" in types["hermes-direct"]["evidence"]
    assert "No Talk call" in types["hermes-direct"]["evidence"]
    assert types["realtime"]["proven"] is True
    by_id = {r["id"]: r for r in body["providers"]}
    row = by_id["hermes-agent"]
    assert row["wired"] is True and row["proven"] is True
    assert "2026-09-24" in row["evidence"]
    # The ears and voice that call used are proven; the other choices on the type are not.
    assert by_id["elevenlabs-scribe"]["proven"] is True
    assert by_id["elevenlabs"]["proven"] is True
    assert by_id["deepgram"]["proven"] is False
    assert by_id["deepgram-aura"]["proven"] is False


async def test_the_cascade_label_no_longer_says_talk_refuses_it(client, hermes):
    """The old label was wrong before this change (the Talk bridge ran outbound cascade)
    and named a flag that no longer exists."""
    async with client as c:
        labels = {p["id"]: p for p in (await c.get("/api/settings")).json()["pipelines"]}
    evidence = labels["cascade"]["evidence"]
    assert "Talk outlet refuses" not in evidence and "CASCADE_OUTBOUND_HOST" not in evidence
    assert "both bridges" in evidence and "inbound" in evidence
    assert labels["cascade"]["proven"] is False


async def test_the_roster_says_which_type_each_agent_is(client, hermes):
    hermes.write_agent("rt")
    hermes.write_agent("direct", pipeline="cascade", providers=dict(DIRECT),
                       hermes_profile="vega")
    hermes.write_agent("vendor", pipeline="cascade", providers=dict(VENDOR),
                       hermes_profile="scout")
    hermes.write_agent("scribe", pipeline="cascade",
                       providers=dict(DIRECT, stt="elevenlabs-scribe"), hermes_profile="vega")
    hermes.write_agent("odd-ears", pipeline="cascade", providers=dict(DIRECT, stt="soniox"),
                       hermes_profile="vega")
    hermes.write_agent("aura", pipeline="cascade",
                       providers=dict(DIRECT, stt="elevenlabs-scribe", tts="deepgram-aura"),
                       hermes_profile="vega")
    hermes.write_agent("odd-voice", pipeline="cascade", providers=dict(DIRECT, tts="cartesia"),
                       hermes_profile="vega")
    async with client as c:
        rows = {r["id"]: r for r in (await c.get("/api/agents")).json()}
    assert rows["rt"]["agent_type"] == "realtime"
    assert rows["direct"]["agent_type"] == "hermes-direct"
    # Ticket 21: the type is the same whichever of its offered ears it uses.
    assert rows["scribe"]["agent_type"] == "hermes-direct"
    assert rows["odd-ears"]["agent_type"] == "custom"
    # ...and whichever of its offered voices (review S5); anything else is Advanced.
    assert rows["aura"]["agent_type"] == "hermes-direct"
    assert rows["odd-voice"]["agent_type"] == "custom"
    assert rows["vendor"]["agent_type"] == "custom"       # still there, under Advanced
    assert rows["direct"]["hermes_routable"] is True
    assert rows["vendor"]["hermes_routable"] is False     # registry says crashed
    assert rows["rt"]["hermes_routable"] is False         # a profile nobody started


# ---------------------------------------------------------- the profile picker --

async def test_the_picker_lists_default_first_and_what_is_reachable(client, hermes):
    async with client as c:
        rows = (await c.get("/api/hermes")).json()["selectable"]
    assert [r["name"] for r in rows] == ["default", "halfway", "scout", "vega"]
    by = {r["name"]: r for r in rows}
    # `default` is the container's own home, never a directory under profiles/.
    assert by["default"] == {"name": "default", "complete": True, "gateway_status": None,
                             "gateway_url": "http://hermes:18789", "routable": True}
    assert by["vega"]["routable"] is True
    assert by["vega"]["gateway_url"] == "http://127.0.0.1:18791"
    assert by["scout"]["routable"] is False
    assert by["scout"]["gateway_status"] == "crashed"
    assert by["halfway"]["complete"] is False and by["halfway"]["routable"] is False


async def test_the_picker_works_with_no_profiles_directory(client, config_dir,  # noqa: F811
                                                           monkeypatch):
    """Binding an Agent to `default` needs nothing mounted, so the list is served even
    where creating a profile is unavailable."""
    monkeypatch.delenv("HERMES_PROFILES_DIR", raising=False)
    async with client as c:
        body = (await c.get("/api/hermes")).json()
    assert body["available"] is False
    assert [r["name"] for r in body["selectable"]] == ["default"]


async def test_changing_the_profile_changes_who_the_bridges_reach(client, hermes):
    hermes.write_agent("robot", pipeline="cascade", providers=dict(DIRECT),
                       hermes_profile="default", knobs={"voice": "el-1"})
    async with client as c:
        res = await c.put("/api/agents/robot/hermes-profile",
                          json={"hermes_profile": "vega"})
    assert res.status_code == 200, res.text
    assert res.json()["routable"] is True
    assert res.json()["gateway_url"] == "http://127.0.0.1:18791"
    # ONE field changed; the rest of the document is untouched.
    doc = hermes.agent_doc("robot")
    assert doc["hermes_profile"] == "vega"
    assert doc["knobs"] == {"voice": "el-1"} and doc["providers"] == DIRECT


@pytest.mark.parametrize("body,status", [
    ({"hermes_profile": "ghost"}, 422),            # no such profile
    ({"hermes_profile": "halfway"}, 422),          # still being created
    ({"hermes_profile": ""}, 422),
    ({"hermes_profile": 7}, 422),
    ({"hermes_profile": "vega", "pipeline": "cascade"}, 422),   # exactly one field
    ({}, 422),
])
async def test_a_refused_profile_change_writes_nothing(client, hermes, body, status):
    hermes.write_agent("robot", hermes_profile="default")
    before = (hermes.agents / "robot.yaml").read_bytes()
    async with client as c:
        res = await c.put("/api/agents/robot/hermes-profile", json=body)
    assert res.status_code == status
    assert (hermes.agents / "robot.yaml").read_bytes() == before


async def test_an_assigned_agent_cannot_be_bound_to_a_profile_nobody_is_running(
        client, hermes):
    """The quiet outage: on the direct lane every call would fall back to Realtime, on
    the Realtime lane every tool call would fail, and the assignment would still look
    healthy. Unassigned, the same change is allowed and says it is not routable."""
    hermes.write_agent("robot", pipeline="cascade", providers=dict(DIRECT),
                       hermes_profile="vega")
    hermes.point("phone", "inbound", "robot")
    before = (hermes.agents / "robot.yaml").read_bytes()
    async with client as c:
        held = await c.put("/api/agents/robot/hermes-profile",
                           json={"hermes_profile": "scout"})
        assert held.status_code == 409
        assert "outlets.phone.inbound" in held.text and "crashed" in held.text
        assert (hermes.agents / "robot.yaml").read_bytes() == before

        await c.put("/api/active", json={"outlets": {"phone": {"inbound": None}}})
        free = await c.put("/api/agents/robot/hermes-profile",
                           json={"hermes_profile": "scout"})
    assert free.status_code == 200 and free.json()["routable"] is False


# ------------------------------------------- the agent-type switch and held slots --

async def test_switching_the_agent_on_the_phone_to_hermes_directly_is_allowed(
        client, hermes):
    """The change the owner asked for, on a live slot, in one click. The bridge then
    resolves THIS document for an inbound call, which it refused before VC24."""
    hermes.write_agent("robot", hermes_profile="vega")
    hermes.point("phone", "inbound", "robot")
    async with client as c:
        res = await c.put("/api/agents/robot/voice", json=TO_DIRECT)
    assert res.status_code == 200, res.text
    profile = profiles.load_effective_profile("inbound", outlet="phone")
    assert profiles.is_hermes_direct(profile.doc)
    assert "realtime" not in profile.doc["providers"]


async def test_the_switch_cannot_break_a_slot_the_agent_holds(client, hermes):
    """An outside-vendor cascade has no inbound lane. Setting it on the Agent answering
    the phone used to save with a 200 and leave every inbound call refusing to start."""
    hermes.write_agent("robot", hermes_profile="vega")
    hermes.point("phone", "inbound", "robot")
    before = (hermes.agents / "robot.yaml").read_bytes()
    async with client as c:
        res = await c.put("/api/agents/robot/voice", json=TO_VENDOR)
    assert res.status_code == 409
    assert "outlets.phone.inbound" in res.text and "outbound-only" in res.text
    assert (hermes.agents / "robot.yaml").read_bytes() == before
    assert profiles.load_effective_profile("inbound", outlet="phone").pipeline == "realtime"


async def test_the_same_switch_on_an_unassigned_agent_is_still_allowed(client, hermes):
    """VC7: nothing is removed. The guard is about held slots, not about the option."""
    hermes.write_agent("robot", hermes_profile="vega")
    async with client as c:
        res = await c.put("/api/agents/robot/voice", json=TO_VENDOR)
    assert res.status_code == 200
    assert hermes.agent_doc("robot")["providers"] == VENDOR


async def test_an_outbound_slot_takes_a_tools_off_direct_agent(client, hermes):
    """The tools setting is the Agent's own and Hermes enforces it, so a tools-off direct
    Agent may hold an outbound slot. It used to be refused as 'cannot be sandboxed'."""
    hermes.write_agent("robot", hermes_profile="vega", guardrails={"on_call_tools": False})
    hermes.point("phone", "outbound", "robot")
    async with client as c:
        res = await c.put("/api/agents/robot/voice", json=TO_DIRECT)
    assert res.status_code == 200, res.text
    assert hermes.agent_doc("robot")["providers"] == DIRECT
    assert profiles.load_effective_profile("outbound", outlet="phone").agent_id == "robot"


@pytest.mark.parametrize("tools", [True, False], ids=["tools-on", "tools-off"])
@pytest.mark.parametrize("outlet", ["phone", "talk"])
async def test_a_direct_agent_can_be_assigned_outbound_whatever_its_tools_setting(
        client, hermes, outlet, tools):
    """The tools setting is per Agent and enforced by Hermes, so it is no reason to keep
    an Agent out of a slot. PUT /api/active runs the same activation the bridges run."""
    hermes.write_agent("robot", pipeline="cascade", providers=dict(DIRECT),
                       hermes_profile="vega", guardrails={"on_call_tools": tools})
    async with client as c:
        res = await c.put("/api/active", json={"outlets": {outlet: {"outbound": "robot"}}})
        assert res.status_code == 200, res.text
        body = (await c.get("/api/active")).json()
    assert profiles.load_effective_profile("outbound", outlet=outlet).agent_id == "robot"
    assert not body.get("warnings"), body["warnings"]


async def test_the_whole_document_put_is_deliberately_left_alone(client, hermes):
    """services/voice-control/CLAUDE.md: the owner chose not to guard this route, and no
    screen calls it. Pinned so the guard above is not 'helpfully' copied onto it without
    that decision being revisited."""
    doc = hermes.write_agent("robot", hermes_profile="vega")
    hermes.point("phone", "inbound", "robot")
    doc.update(pipeline="cascade", providers=dict(VENDOR))
    async with client as c:
        res = await c.put("/api/agents/robot", json=doc)
    assert res.status_code == 200


# ------------------------------------------------------ the Listening card's knobs --

async def test_deepgram_options_are_set_and_cleared_one_at_a_time(client, hermes):
    hermes.write_agent("robot", pipeline="cascade", providers=dict(DIRECT),
                       hermes_profile="vega", knobs={"voice": "el-1"})
    async with client as c:
        res = await c.put("/api/agents/robot/voice", json={"knobs": {
            "transcription_model": "nova-3", "language": "en-AU",
            "keyterms": ["Hermes", "Alex"], "smart_format": True, "numerals": False}})
        assert res.status_code == 200, res.text
        assert hermes.agent_doc("robot")["knobs"] == {
            "voice": "el-1", "transcription_model": "nova-3", "language": "en-AU",
            "keyterms": ["Hermes", "Alex"], "smart_format": True, "numerals": False}
        # A null DELETES the knob. Without it a language could be set and never cleared.
        res = await c.put("/api/agents/robot/voice",
                          json={"knobs": {"language": None, "keyterms": None}})
    assert res.status_code == 200
    assert hermes.agent_doc("robot")["knobs"] == {
        "voice": "el-1", "transcription_model": "nova-3",
        "smart_format": True, "numerals": False}


@pytest.mark.parametrize("knobs", [{"smart_format": "yes"}, {"numerals": 1},
                                   {"endpointing": 300}, {"utterance_end_ms": 1000}])
async def test_a_bad_or_unsupported_deepgram_option_is_refused(client, hermes, knobs):
    """Deepgram's own endpointing is not offered: turn-taking is VoiceMaster's, so it
    would be a setting with no effect."""
    hermes.write_agent("robot", pipeline="cascade", providers=dict(DIRECT),
                       hermes_profile="vega")
    before = (hermes.agents / "robot.yaml").read_bytes()
    async with client as c:
        res = await c.put("/api/agents/robot/voice", json={"knobs": knobs})
    assert res.status_code == 422
    assert (hermes.agents / "robot.yaml").read_bytes() == before

