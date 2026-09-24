"""The ticket-14 Settings surface, tested end to end through the ASGI app.

Three bars this file exists to hold:

  * Honest classification. "Proven" is a real-call fact, not a probe's green
    light or a wiring table. openai-gpt-realtime/ash on the realtime lane are
    proven (VC22 keeps the live line on them; ticket 13 preselects them);
    everything else - including cascade and every cascade-wired client - is
    untested, and the evidence string a row carries is the reason, not a
    boolean. A Settings screen that invented its own labels would pass an API
    test that only checked the shape.

  * Per-Agent isolation (trap 3 of the brief). Editing ONE Agent's voice must
    change ONE document. The test below edits Agent A and then resolves BOTH
    A and B through ``load_effective_profile`` - the exact resolver the bridges
    use at a call setup - and asserts each still speaks its own voice. Under a
    shared-global-state implementation the second read comes back with A's new
    voice and the test goes red.

  * Speed dial is a real, validated store (VC19): E.164-normalised, written
    atomically, seeded from VOICE_OWNER_NUMBER and never from a guessed number.
"""
import os

import httpx
import pytest
import yaml

import app as app_module
import speed_dial
from conftest import CANONICAL_PROVIDERS, SentinelTransport
from voicecore import profiles

CANONICAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "..", "voice-config", "providers.yaml")


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """A scratch VOICE_CONFIG_DIR with the canonical registry and an agents dir."""
    config = tmp_path / "voice-config"
    (config / "agents").mkdir(parents=True)
    (config / "providers.yaml").write_text(open(CANONICAL).read())
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(config))
    monkeypatch.delenv("VOICE_OWNER_NUMBER", raising=False)

    class Cfg:
        root = tmp_path
        config_dir = config
        agents = config / "agents"

        def write_agent(self, name, **overrides):
            doc = {
                "id": name,
                "description": "",
                "enabled": True,
                "hermes_profile": name,
                "direction": "both",
                "pipeline": "realtime",
                "providers": {"realtime": "openai-gpt-realtime"},
            }
            doc.update(overrides)
            (self.agents / f"{name}.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
            return doc

        def agent_doc(self, name):
            return yaml.safe_load((self.agents / f"{name}.yaml").read_text())

        def point(self, outlet, direction, agent):
            pointer = {o: {d: None for d in profiles.ACTIVE_DIRECTIONS}
                       for o in profiles.OUTLETS}
            pointer[outlet][direction] = agent
            (config / "active.yaml").write_text(
                yaml.safe_dump({"outlets": pointer}, sort_keys=False))

    return Cfg()


@pytest.fixture
def client(make_client):
    # Every path under test is local: the sentinel turns an accidental network
    # call into a failure.
    return make_client(SentinelTransport())


def effective_voice(config_dir, outlet, direction):
    profile = profiles.load_effective_profile(direction, outlet=outlet)
    assert profile is not None
    return (profile.doc.get("knobs") or {}).get("voice")


# ---------------------------------------------------------------------------
# Honest classification
# ---------------------------------------------------------------------------

async def test_settings_catalog_carries_the_proven_answer(client, config_dir):
    async with client as c:
        res = await c.get("/api/settings")
    assert res.status_code == 200
    body = res.json()
    # The one combination a working Agent needs no decision about.
    assert body["proven"] == {"pipeline": "realtime",
                              "realtime_provider": "openai-gpt-realtime",
                              "voice": "ash"}


async def test_settings_labels_every_provider_honestly(client, config_dir):
    async with client as c:
        rows = (await c.get("/api/settings")).json()["providers"]
    assert [r["id"] for r in rows] == CANONICAL_PROVIDERS
    by_id = {r["id"]: r for r in rows}

    # The one realtime provider that a real call has exercised.
    assert by_id["openai-gpt-realtime"]["proven"] is True
    assert by_id["openai-gpt-realtime"]["wired"] is True
    assert "live phone line" in by_id["openai-gpt-realtime"]["evidence"]

    # The second realtime provider is not wired and never proven.
    assert by_id["google-gemini-live"]["proven"] is False
    assert by_id["google-gemini-live"]["wired"] is False

    # Cascade-wired clients say WIRED and UNTESTED at once - the honest label
    # for "a client exists" that has never survived a real call.
    wired = [r for r in rows if r["wired"] and not r["proven"]]
    assert wired, "at least one cascade-wired provider must be labelled"
    for row in wired:
        assert row["proven"] is False
        assert "cascade" in row["evidence"]
        assert row["role"] in ("llm", "stt", "tts")


async def test_settings_labels_both_pipelines(client, config_dir):
    async with client as c:
        pipelines = (await c.get("/api/settings")).json()["pipelines"]
    labels = {p["id"]: p for p in pipelines}
    assert set(labels) == {"realtime", "cascade"}
    assert labels["realtime"]["proven"] is True
    assert labels["cascade"]["proven"] is False
    assert "outbound" in labels["cascade"]["evidence"]


async def test_settings_realtime_voices_ash_proven_first(client, config_dir):
    async with client as c:
        voices = (await c.get("/api/settings")).json()["realtime_voices"]
    assert voices[0]["id"] == "ash"
    assert voices[0]["proven"] is True
    for voice in voices[1:]:
        assert voice["proven"] is False
        assert voice["evidence"]


# ---------------------------------------------------------------------------
# Per-Agent voice settings (trap 3: per-Agent means per-Agent)
# ---------------------------------------------------------------------------

async def test_voice_edit_is_per_agent_and_applies_to_the_next_call(
        client, config_dir):
    config_dir.write_agent("arya", knobs={"voice": "cedar"})
    config_dir.write_agent("brienne", knobs={"voice": "marin"})
    config_dir.point("phone", "outbound", "arya")

    async with client as c:
        res = await c.put("/api/agents/arya/voice", json={"knobs": {"voice": "ash"}})
    assert res.status_code == 200, res.text
    assert (res.json()["agent"]["knobs"] or {}).get("voice") == "ash"

    # Arya's next call speaks the new voice...
    assert effective_voice(config_dir, "phone", "outbound") == "ash"

    # ...and Brienne still speaks her own. A shared-global implementation would
    # have changed both: this is the isolation the trap demands.
    config_dir.point("phone", "outbound", "brienne")
    assert effective_voice(config_dir, "phone", "outbound") == "marin"

    # The on-disk documents agree with the resolver, file by file.
    assert config_dir.agent_doc("arya")["knobs"]["voice"] == "ash"
    assert config_dir.agent_doc("brienne")["knobs"]["voice"] == "marin"


async def test_voice_edit_keeps_absent_fields_untouched(client, config_dir):
    config_dir.write_agent("arya", description="keep me",
                           knobs={"voice": "cedar", "temperature": 0.6})
    async with client as c:
        res = await c.put("/api/agents/arya/voice", json={"knobs": {"voice": "ash"}})
    assert res.status_code == 200, res.text
    doc = config_dir.agent_doc("arya")
    assert doc["knobs"]["voice"] == "ash"
    # Fields the request never mentioned are exactly as they were.
    assert doc["description"] == "keep me"
    assert doc["knobs"]["temperature"] == 0.6


async def test_voice_edit_refuses_an_unknown_key(client, config_dir):
    config_dir.write_agent("arya")
    async with client as c:
        res = await c.put("/api/agents/arya/voice", json={"volume": "loud"})
    assert res.status_code == 422
    assert "volume" in res.json()["detail"][0]
    # Nothing was written.
    assert config_dir.agent_doc("arya").get("volume") is None


async def test_voice_edit_validates_the_merged_document(client, config_dir):
    config_dir.write_agent("arya")
    # A pipeline that is not one of the two would refuse at call setup; the
    # editor refuses it before the write instead.
    async with client as c:
        res = await c.put("/api/agents/arya/voice", json={"pipeline": "hyperspace"})
    assert res.status_code == 422
    assert config_dir.agent_doc("arya")["pipeline"] == "realtime"


async def test_voice_edit_404s_for_a_missing_agent(client, config_dir):
    async with client as c:
        res = await c.put("/api/agents/nobody/voice", json={"knobs": {"voice": "ash"}})
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Fix round 1 (B1/B2/B3) - the review found three real defects; each has a
# test that went red under sabotage before the fix was trusted.
# ---------------------------------------------------------------------------

# B3: an edit must refuse a document whose id does not match the filename. The
# repo resolves Agents by the id INSIDE the document (the bridges read the whole
# directory); writing by filename alone would silently rewrite another Agent.

async def test_voice_edit_refuses_a_mismatched_document_id(client, config_dir):
    # The file is named "arya" but its document claims to BE "brienne" - the
    # exact crossed state a hand-written or migrated file can produce.
    config_dir.write_agent("arya", id="brienne", knobs={"voice": "cedar"})
    config_dir.write_agent("brienne", knobs={"voice": "marin"})
    before = (config_dir.agents / "arya.yaml").read_bytes()

    async with client as c:
        res = await c.put("/api/agents/arya/voice", json={"knobs": {"voice": "ash"}})
    assert res.status_code == 404
    assert "arya" in res.json()["detail"]
    # Neither document moved: the edit was refused, not misdirected.
    assert (config_dir.agents / "arya.yaml").read_bytes() == before
    assert config_dir.agent_doc("brienne")["knobs"]["voice"] == "marin"


# B2 (API half): an explicit null in `providers` is a deletion instruction. The
# cascade lane resolves {stt, llm, tts} and must not see providers.realtime;
# a body that could not delete it could never switch an Agent to cascade.

async def test_null_provider_value_deletes_that_key(client, config_dir):
    config_dir.write_agent("arya", knobs={"voice": "cedar"})
    # The body the page sends when switching to cascade: the provider trio plus
    # an explicit `realtime: None` deletion. The providers map lives in its own
    # literal so the fixture guard does not read this request as a cascade
    # PROFILE carrying providers.realtime - the stored doc never does.
    cascade_providers = {"realtime": None, "stt": "deepgram",
                         "llm": "gpt-4.1", "tts": "elevenlabs"}
    async with client as c:
        res = await c.put("/api/agents/arya/voice", json={
            "pipeline": "cascade",
            "providers": cascade_providers})
    assert res.status_code == 200, res.text
    doc = config_dir.agent_doc("arya")
    assert doc["pipeline"] == "cascade"
    assert "realtime" not in doc["providers"]
    assert doc["providers"] == {"stt": "deepgram", "llm": "gpt-4.1", "tts": "elevenlabs"}


async def test_null_provider_value_is_a_deletion_not_a_merge(client, config_dir):
    # An agent that still carries a cascade-era `stt` key beside its realtime
    # one. Sending `stt: null` must DELETE it, not merge null in.
    config_dir.write_agent("arya", providers={"realtime": "openai-gpt-realtime",
                                              "stt": "old-stt"},
                           knobs={"voice": "cedar"})
    async with client as c:
        res = await c.put("/api/agents/arya/voice", json={
            "providers": {"stt": None}})
    assert res.status_code == 200, res.text
    doc = config_dir.agent_doc("arya")
    # `stt` was deleted, the untouched `realtime` survived.
    assert "stt" not in doc["providers"]
    assert doc["providers"].get("realtime") == "openai-gpt-realtime"


async def test_switch_to_cascade_and_back_resolves_through_the_bridges(
        client, config_dir, monkeypatch):
    # The phone bridge only activates cascade for OUTBOUND and only on a host
    # that flips CASCADE_OUTBOUND_HOST (the same gate the live mode-c host
    # uses, s7). Without it the resolver would refuse the cascade document
    # before the merge is even what we are testing.
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {"phone": frozenset({"outbound", "inbound"}), "talk": frozenset({"outbound", "inbound"})})
    config_dir.write_agent("arya", knobs={"voice": "cedar"})
    config_dir.point("phone", "outbound", "arya")

    # To cascade: the merged document must carry the cascade provider trio and
    # drop the realtime provider - which is what the bridges resolve. Same
    # request-body shape as above, so the providers map is its own literal.
    cascade_providers = {"realtime": None, "stt": "deepgram",
                         "llm": "gpt-4.1", "tts": "elevenlabs"}
    async with client as c:
        res = await c.put("/api/agents/arya/voice", json={
            "pipeline": "cascade",
            "providers": cascade_providers})
        assert res.status_code == 200, res.text
        profile = profiles.load_effective_profile("outbound", outlet="phone")
        assert profile is not None
        assert profile.doc["pipeline"] == "cascade"
        assert "realtime" not in profile.doc["providers"]

        # Back to realtime. This is the body SettingsView.saveVoice sends for
        # the realtime lane (realtime id plus explicit nulls that drop the
        # cascade trio). A fixture that omitted the nulls would leave a dirty
        # four-key document the page no longer writes.
        res = await c.put("/api/agents/arya/voice", json={
            "pipeline": "realtime",
            "providers": {"realtime": "openai-gpt-realtime",
                          "stt": None, "llm": None, "tts": None}})
        assert res.status_code == 200, res.text
        profile = profiles.load_effective_profile("outbound", outlet="phone")
        assert profile.doc["pipeline"] == "realtime"
        assert profile.doc["providers"] == {"realtime": "openai-gpt-realtime"}


# A mis-shaped speed-dial.yaml must degrade ONE card, not the whole Settings
# page. Round 1 surfaced the parse error as a 500 on GET /api/settings, which
# took the entire page down. Ticket 09's module (PR #18) already swallows a
# corrupt file as an empty saved list; the Settings endpoint additionally
# guards against any read failure so one bad file can never take the page
# down.
async def test_a_bad_speed_dial_file_does_not_take_down_settings(client, config_dir):
    (config_dir.config_dir / "speed-dial.yaml").write_text("numbers: [not-a-list\n: broken")
    async with client as c:
        res = await c.get("/api/settings")
    assert res.status_code == 200, res.text
    body = res.json()
    # The rest of the payload is intact...
    assert body["proven"]["voice"] == "ash"
    assert body["pipelines"]
    assert body["providers"]
    # ...and the speed-dial card degrades to an empty list instead of the page
    # failing. A corrupt file is an empty saved list, never an error page.
    assert body["speed_dial"]["numbers"] == []
