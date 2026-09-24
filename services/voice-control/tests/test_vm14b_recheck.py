"""Independent re-check of PR #20 (ticket 14), round 2.

Written by the verifier, not the author. Every test here is meant to be
sabotage-bound: reverting the corresponding fix in app.py / SettingsView.tsx
must turn it RED.
"""
import os

import httpx
import pytest
import yaml

import app as app_module
from conftest import SentinelTransport
from voicecore import profiles

CANONICAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "..", "voice-config", "providers.yaml")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    config = tmp_path / "voice-config"
    (config / "agents").mkdir(parents=True)
    (config / "providers.yaml").write_text(open(CANONICAL).read())
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(config))

    class Cfg:
        config_dir = config
        agents = config / "agents"

        def write(self, filename, doc):
            (self.agents / filename).write_text(yaml.safe_dump(doc, sort_keys=False))

        def agent(self, agent_id, filename=None, **overrides):
            doc = {
                "id": agent_id,
                "description": "",
                "enabled": True,
                "hermes_profile": agent_id,
                "direction": "both",
                "pipeline": "realtime",
                "providers": {"realtime": "openai-gpt-realtime"},
                "knobs": {"voice": "ash"},
            }
            doc.update(overrides)
            self.write(filename or f"{agent_id}.yaml", doc)
            return doc

        def doc(self, filename):
            return yaml.safe_load((self.agents / filename).read_text())

        def raw(self, filename):
            return (self.agents / filename).read_bytes()

    return Cfg()


@pytest.fixture
def client(make_client):
    return make_client(SentinelTransport())


# ---------------------------------------------------------------------------
# B2 - the cascade pipeline must be saveable, and omission must not delete.
# ---------------------------------------------------------------------------

CASCADE_TRIO = {"stt": "deepgram", "llm": "gpt-4.1", "tts": "elevenlabs"}


async def test_b2_cascade_selection_round_trips(client, cfg):
    """Save cascade, re-read through the ROUTE, get cascade back with
    providers.realtime absent."""
    cfg.agent("arya")
    body_providers = dict(CASCADE_TRIO)
    body_providers["realtime"] = None
    async with client as c:
        res = await c.put("/api/agents/arya/voice",
                          json={"pipeline": "cascade", "providers": body_providers})
        assert res.status_code == 200, res.text
        read_back = await c.get("/api/agents/arya")
    assert read_back.status_code == 200, read_back.text
    doc = read_back.json()
    assert doc["pipeline"] == "cascade"
    assert doc["providers"] == CASCADE_TRIO
    assert "realtime" not in doc["providers"]
    # And on disk, which is what the bridges read.
    assert cfg.doc("arya.yaml")["providers"] == CASCADE_TRIO


async def test_b2_overbroad_omitting_a_key_must_not_delete_it(client, cfg):
    """OVER-BROAD TRAP. Null deletes; OMISSION must not. A payload that names
    only `realtime` must leave a stale `stt` alone, and a payload that omits
    `providers` entirely must leave the whole map alone."""
    stored = {"realtime": "openai-gpt-realtime", "stt": "deepgram"}
    cfg.agent("arya", providers=dict(stored), knobs={"voice": "cedar"})

    async with client as c:
        # (a) providers named, but only one key of it.
        res = await c.put("/api/agents/arya/voice",
                          json={"providers": {"realtime": "openai-gpt-realtime"}})
        assert res.status_code == 200, res.text
        assert cfg.doc("arya.yaml")["providers"] == stored, "omitted key was deleted"

        # (b) providers not named at all.
        res = await c.put("/api/agents/arya/voice", json={"knobs": {"voice": "marin"}})
        assert res.status_code == 200, res.text
        after = cfg.doc("arya.yaml")
        assert after["providers"] == stored, "whole providers map dropped on omission"
        assert after["knobs"]["voice"] == "marin"

        # (c) an empty providers map deletes nothing.
        res = await c.put("/api/agents/arya/voice", json={"providers": {}})
        assert res.status_code == 200, res.text
        assert cfg.doc("arya.yaml")["providers"] == stored


async def test_b2_realtime_selection_round_trips_after_cascade(client, cfg, monkeypatch):
    """Switch to cascade, then back to realtime, and resolve both through the
    same resolver the bridges use."""
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {"phone": frozenset({"outbound", "inbound"}), "talk": frozenset({"outbound", "inbound"})})
    cfg.agent("arya")
    pointer = {o: {d: None for d in profiles.ACTIVE_DIRECTIONS} for o in profiles.OUTLETS}
    pointer["phone"]["outbound"] = "arya"
    (cfg.config_dir / "active.yaml").write_text(
        yaml.safe_dump({"outlets": pointer}, sort_keys=False))

    to_cascade = dict(CASCADE_TRIO)
    to_cascade["realtime"] = None
    back = {"realtime": "openai-gpt-realtime", "stt": None, "llm": None, "tts": None}
    async with client as c:
        res = await c.put("/api/agents/arya/voice",
                          json={"pipeline": "cascade", "providers": to_cascade})
        assert res.status_code == 200, res.text
        profile = profiles.load_effective_profile("outbound", outlet="phone")
        assert profile.doc["pipeline"] == "cascade"

        res = await c.put("/api/agents/arya/voice",
                          json={"pipeline": "realtime", "providers": back})
        assert res.status_code == 200, res.text
    profile = profiles.load_effective_profile("outbound", outlet="phone")
    assert profile.doc["pipeline"] == "realtime"
    assert profile.doc["providers"] == {"realtime": "openai-gpt-realtime"}


# ---------------------------------------------------------------------------
# B3 - the voice route must refuse a file whose document carries another id.
# ---------------------------------------------------------------------------

async def test_b3_mismatched_document_id_404s_and_writes_nothing(client, cfg):
    """arya.yaml holds `id: brienne`. PUT /api/agents/arya/voice must 404 and
    leave BOTH documents byte-identical."""
    cfg.agent("brienne", filename="arya.yaml", knobs={"voice": "marin"})
    cfg.agent("brienne", filename="brienne.yaml", knobs={"voice": "marin"})
    before_arya = cfg.raw("arya.yaml")
    before_brienne = cfg.raw("brienne.yaml")

    async with client as c:
        res = await c.put("/api/agents/arya/voice", json={"knobs": {"voice": "shimmer"}})
    assert res.status_code == 404, res.text
    assert "arya" in res.text
    assert cfg.raw("arya.yaml") == before_arya
    assert cfg.raw("brienne.yaml") == before_brienne


async def test_b3_overbroad_the_matching_case_still_200s_and_writes(client, cfg):
    """OVER-BROAD TRAP. A guard that is too strict breaks every save. The
    ordinary filename==id pairing must still write."""
    cfg.agent("arya", knobs={"voice": "cedar"})
    async with client as c:
        res = await c.put("/api/agents/arya/voice", json={"knobs": {"voice": "shimmer"}})
    assert res.status_code == 200, res.text
    assert res.json()["agent"]["knobs"]["voice"] == "shimmer"
    assert cfg.doc("arya.yaml")["knobs"]["voice"] == "shimmer"


async def test_b3_overbroad_ids_with_legal_punctuation_still_save(client, cfg):
    """OVER-BROAD TRAP, second axis: ids the id rule allows (dots, dashes,
    underscores, digits) must still round-trip through the guard."""
    for aid in ("a-1", "a_1", "a.b", "AgentX", "x9"):
        if not app_module._safe_agent_id(aid):
            continue
        cfg.agent(aid, knobs={"voice": "cedar"})
        async with make_fresh(client) as c:
            res = await c.put(f"/api/agents/{aid}/voice",
                              json={"knobs": {"voice": "shimmer"}})
        assert res.status_code == 200, f"{aid}: {res.text}"
        assert cfg.doc(f"{aid}.yaml")["knobs"]["voice"] == "shimmer"


def make_fresh(client):
    """httpx.AsyncClient is single-use under `async with`; rebuild per loop pass."""
    return httpx.AsyncClient(transport=client._transport, base_url="http://testserver")


# ---------------------------------------------------------------------------
# N-candidates: what the PAGE actually sends, not what the API test hand-writes.
# ---------------------------------------------------------------------------

async def test_n_switch_back_to_realtime_with_the_payload_the_page_sends(client, cfg):
    """SettingsView.saveVoice sends `{realtime: <id>, stt: null, llm: null,
    tts: null}` for the realtime lane so the cascade trio is deleted."""
    trio = dict(CASCADE_TRIO)
    cfg.agent("arya", pipeline="cascade", providers=trio)
    async with client as c:
        res = await c.put("/api/agents/arya/voice",
                          json={"pipeline": "realtime",
                                "providers": {"realtime": "openai-gpt-realtime",
                                              "stt": None, "llm": None,
                                              "tts": None},
                                "knobs": {"voice": "ash"}})
    assert res.status_code == 200, res.text
    doc = cfg.doc("arya.yaml")
    assert doc["pipeline"] == "realtime"
    assert doc["providers"] == {"realtime": "openai-gpt-realtime"}


async def test_n_cascade_saved_without_choosing_providers(client, cfg):
    """The page seeds draftStt/Llm/Tts from the Agent's own document; a realtime
    Agent has none, so the three <select>s open blank and Save sends ''."""
    cfg.agent("arya")
    empty_trio = {"stt": "", "llm": "", "tts": ""}
    empty_trio["realtime"] = None
    async with client as c:
        res = await c.put("/api/agents/arya/voice",
                          json={"pipeline": "cascade", "providers": empty_trio})
    print("STATUS", res.status_code, res.text[:400])
    assert res.status_code in (200, 422)
    if res.status_code == 200:
        pytest.fail("empty provider ids were accepted and written: "
                    f"{cfg.doc('arya.yaml')['providers']}")


async def test_n_an_agent_the_roster_lists_can_be_uneditable(client, cfg):
    """An Agent stored as team-arya.yaml is listed AND editable. Identity is
    the document id, so Save writes that file and does not invent arya.yaml.
    The page must not list a row it then denies exists."""
    cfg.agent("arya", filename="team-arya.yaml")
    async with client as c:
        roster = await c.get("/api/agents")
        assert roster.status_code == 200, roster.text
        ids = [row["id"] for row in roster.json()]
        assert "arya" in ids, ids
        res = await c.put("/api/agents/arya/voice", json={"knobs": {"voice": "shimmer"}})
    assert res.status_code == 200, res.text
    assert res.json()["agent"]["id"] == "arya"
    assert res.json()["agent"]["knobs"]["voice"] == "shimmer"
    assert cfg.doc("team-arya.yaml")["knobs"]["voice"] == "shimmer"
    assert not (cfg.agents / "arya.yaml").exists()


async def test_n_a_yml_agent_is_listed_and_uneditable(client, cfg):
    """A .yml Agent is listed AND editable. The roster already accepted *.yml;
    the write routes must too."""
    cfg.agent("arya", filename="arya.yml")
    async with client as c:
        roster = await c.get("/api/agents")
        ids = [row["id"] for row in roster.json()]
        assert "arya" in ids, ids
        res = await c.put("/api/agents/arya/voice", json={"knobs": {"voice": "shimmer"}})
    assert res.status_code == 200, res.text
    assert cfg.doc("arya.yml")["knobs"]["voice"] == "shimmer"
    assert not (cfg.agents / "arya.yaml").exists()


async def test_n_delete_still_addresses_by_filename_without_an_id_guard(client, cfg):
    """arya.yaml holds `id: brienne`. DELETE /api/agents/arya must refuse
    (that URL names no Agent) and leave the file. DELETE /api/agents/brienne
    is the one that unlinks it — the document id, not the filename."""
    cfg.agent("brienne", filename="arya.yaml")
    before = cfg.raw("arya.yaml")
    async with client as c:
        res = await c.delete("/api/agents/arya")
    assert res.status_code == 404, res.text
    assert "arya" in res.text
    assert cfg.raw("arya.yaml") == before
    async with make_fresh(client) as c:
        res = await c.delete("/api/agents/brienne")
    assert res.status_code == 200, res.text
    assert not (cfg.agents / "arya.yaml").exists()


async def test_n_delete_refuses_when_url_id_does_not_match_document_id(client, cfg):
    """A DELETE whose URL id does not match the document's `id:` refuses and
    leaves both files byte-identical on disk."""
    cfg.agent("brienne", filename="arya.yaml")
    cfg.agent("sansa", filename="sansa.yaml")
    before_arya = cfg.raw("arya.yaml")
    before_sansa = cfg.raw("sansa.yaml")
    async with client as c:
        res = await c.delete("/api/agents/arya")
    assert res.status_code == 404, res.text
    assert cfg.raw("arya.yaml") == before_arya
    assert cfg.raw("sansa.yaml") == before_sansa


async def test_n_delete_of_a_renamed_in_use_agent_still_409s(client, cfg):
    """The in-use 409 is keyed on the document id. A file named for someone
    else cannot be deleted out from under an assigned Agent."""
    cfg.agent("brienne", filename="arya.yaml")
    pointer = {o: {d: None for d in profiles.ACTIVE_DIRECTIONS} for o in profiles.OUTLETS}
    pointer["phone"]["inbound"] = "brienne"
    (cfg.config_dir / "active.yaml").write_text(
        yaml.safe_dump({"outlets": pointer}, sort_keys=False))
    before = cfg.raw("arya.yaml")
    async with client as c:
        sneaky = await c.delete("/api/agents/arya")
        real = await c.delete("/api/agents/brienne")
    assert sneaky.status_code == 404, sneaky.text
    assert real.status_code == 409, real.text
    assert cfg.raw("arya.yaml") == before


async def test_n_a_listed_agent_can_be_assigned_even_when_the_file_is_yml(client, cfg):
    """The active-slot guard used to look for <id>.yaml, so a .yml Agent the
    roster offered could not be pointed at. Identity is the document id here
    too: the page must not offer a row the assign button then denies."""
    cfg.agent("arya", filename="arya.yml")
    pointer = {o: {d: None for d in profiles.ACTIVE_DIRECTIONS} for o in profiles.OUTLETS}
    pointer["phone"]["outbound"] = "arya"
    async with client as c:
        res = await c.put("/api/active", json={"outlets": pointer})
    assert res.status_code == 200, res.text
    assert res.json()["outlets"]["phone"]["outbound"] == "arya"


async def test_n_matching_cases_still_write_yaml_yml_and_renamed(client, cfg):
    """OVER-BROAD TRAP. A guard that only accepts filename==id.yaml breaks
    every save that is not that pairing. .yaml, .yml, and a renamed file
    must all round-trip through GET, PUT /voice, PUT whole-document, and
    DELETE — each against the file that actually holds the document."""
    cases = (
        ("plain", "plain.yaml"),
        ("dotted", "dotted.yml"),
        ("renamed", "team-renamed.yaml"),
    )
    for aid, filename in cases:
        cfg.agent(aid, filename=filename, knobs={"voice": "cedar"})
        async with make_fresh(client) as c:
            got = await c.get(f"/api/agents/{aid}")
            assert got.status_code == 200, f"{aid} GET: {got.text}"
            assert got.json()["id"] == aid
            voice = await c.put(f"/api/agents/{aid}/voice",
                                json={"knobs": {"voice": "shimmer"}})
            assert voice.status_code == 200, f"{aid} voice: {voice.text}"
            whole = dict(cfg.doc(filename))
            whole["description"] = f"edited {aid}"
            put = await c.put(f"/api/agents/{aid}", json=whole)
            assert put.status_code == 200, f"{aid} PUT: {put.text}"
        assert cfg.doc(filename)["knobs"]["voice"] == "shimmer"
        assert cfg.doc(filename)["description"] == f"edited {aid}"
        assert not (cfg.agents / f"{aid}.yaml").exists() or filename == f"{aid}.yaml"
        async with make_fresh(client) as c:
            deleted = await c.delete(f"/api/agents/{aid}")
        assert deleted.status_code == 200, f"{aid} DELETE: {deleted.text}"
        assert not (cfg.agents / filename).exists()
