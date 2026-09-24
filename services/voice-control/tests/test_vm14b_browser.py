"""Independent re-check of B1 through a real Chromium against the COMMITTED bundle.

B1 was a front-end defect (the editor seeded draftVoice from the catalog's
`proven` rather than from the Agent's own document), so only the built bundle
can bind it. Reverting SettingsView.tsx and rebuilding must turn
`test_b1_*_preserves` RED while `test_b1_overbroad_*` stays GREEN.
"""
import pytest
import yaml

pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed (requirements-dev.txt)"
)


def agent_doc(agent_id, **overrides):
    doc = {
        "id": agent_id,
        "description": "a worked agent",
        "enabled": True,
        "hermes_profile": agent_id,
        "pipeline": "realtime",
        "providers": {"realtime": "openai-gpt-realtime"},
        "knobs": {"voice": "ash"},
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "agents").mkdir()

    class Cfg:
        root = tmp_path

        def agent(self, agent_id, **overrides):
            (tmp_path / "agents" / f"{agent_id}.yaml").write_text(
                yaml.safe_dump(agent_doc(agent_id, **overrides), sort_keys=False))
            return agent_id

        def doc(self, agent_id):
            return yaml.safe_load((tmp_path / "agents" / f"{agent_id}.yaml").read_text())

        def no_slots(self):
            (tmp_path / "active.yaml").write_text(yaml.safe_dump(
                {"outlets": {"phone": {"inbound": None, "outbound": None},
                             "talk": {"inbound": None, "outbound": None}}},
                sort_keys=False))

    return Cfg()


@pytest.fixture
def open_settings(stack, page, cfg):
    """The per-Agent voice editor. It lived on Settings behind an Agent dropdown when
    these tests were written; it is on each Agent's own Voice tab now (A2)."""
    def _open(agent_id="arya"):
        running = stack({})
        page.goto(f"{running.base}/agents/{agent_id}/voice", wait_until="networkidle")
        page.wait_for_selector('[data-testid="settings-agent-voice"]')
        return page

    return _open


# ---------------------------------------------------------------------------
# B1: the editor must show, and must preserve, THIS Agent's voice.
# ---------------------------------------------------------------------------

def test_b1_lone_agent_save_preserves_its_own_voice(cfg, open_settings, page):
    """The plainest operator path: one Agent whose voice is not the proven
    default, open Settings, press Save. Nothing was touched, so nothing may
    change. No <select> interaction - this exercises the AUTO seed."""
    cfg.agent("arya", knobs={"voice": "marin"})
    cfg.no_slots()
    page = open_settings()
    assert page.input_value('[data-testid="settings-voice"]') == "marin"
    page.click('[data-testid="settings-save-voice"]')
    page.wait_for_selector('[data-testid="settings-voice-saved"]', timeout=10_000)
    assert cfg.doc("arya")["knobs"]["voice"] == "marin"


def test_b1_selecting_a_second_agent_seeds_from_that_agent(cfg, open_settings, page):
    """Two Agents, neither on the proven voice. Selecting the second must show
    the second's voice, and saving must keep it - and must not touch the first."""
    cfg.agent("arya", knobs={"voice": "cedar"})
    cfg.agent("brienne", knobs={"voice": "shimmer"})
    cfg.no_slots()
    page = open_settings("arya")
    # Moving from one Agent to the next re-seeds the same editor from the next one.
    page.click('.master-item:has-text("brienne")')
    page.wait_for_url("**/agents/brienne/voice")
    assert page.input_value('[data-testid="settings-voice"]') == "shimmer"
    page.click('[data-testid="settings-save-voice"]')
    page.wait_for_selector('[data-testid="settings-voice-saved"]', timeout=10_000)
    assert cfg.doc("brienne")["knobs"]["voice"] == "shimmer"
    assert cfg.doc("arya")["knobs"]["voice"] == "cedar"


def test_b1_overbroad_a_deliberate_voice_change_still_writes(cfg, open_settings, page):
    """OVER-BROAD TRAP. A fix that simply stopped sending the voice would pass
    the two tests above and break the feature. Typing a new voice must write it."""
    cfg.agent("arya", knobs={"voice": "marin"})
    cfg.no_slots()
    page = open_settings()
    page.fill('[data-testid="settings-voice"]', "shimmer")
    page.click('[data-testid="settings-save-voice"]')
    page.wait_for_selector('[data-testid="settings-voice-saved"]', timeout=10_000)
    assert cfg.doc("arya")["knobs"]["voice"] == "shimmer"


def test_b1_the_editor_also_seeds_pipeline_and_provider_from_the_agent(
        cfg, open_settings, page, monkeypatch):
    """The same seeding bug class on the other two fields: a cascade Agent must
    open showing cascade, not the proven realtime lane."""
    import voicecore.profiles as profiles
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {"phone": frozenset({"outbound", "inbound"}), "talk": frozenset({"outbound", "inbound"})})
    trio = {"stt": "deepgram", "llm": "gpt-4.1", "tts": "elevenlabs"}
    cfg.agent("arya", pipeline="cascade", providers=trio, knobs={"voice": "marin"})
    cfg.no_slots()
    page = open_settings()
    assert page.is_checked('[data-testid="settings-pipeline-cascade"]')
    assert page.input_value('[data-testid="settings-stt-provider"]') == "deepgram"
    assert page.input_value('[data-testid="settings-llm-provider"]') == "gpt-4.1"
    assert page.input_value('[data-testid="settings-tts-provider"]') == "elevenlabs"


def test_b1_saving_a_cascade_agent_untouched_keeps_it_cascade(
        cfg, open_settings, page, monkeypatch):
    """The B1 defect, applied to the pipeline field: opening a cascade Agent and
    pressing Save must not demote it to the proven realtime lane."""
    import voicecore.profiles as profiles
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {"phone": frozenset({"outbound", "inbound"}), "talk": frozenset({"outbound", "inbound"})})
    trio = {"stt": "deepgram", "llm": "gpt-4.1", "tts": "elevenlabs"}
    cfg.agent("arya", pipeline="cascade", providers=trio, knobs={"voice": "marin"})
    cfg.no_slots()
    page = open_settings()
    page.click('[data-testid="settings-save-voice"]')
    page.wait_for_selector('[data-testid="settings-voice-saved"]', timeout=10_000)
    doc = cfg.doc("arya")
    assert doc["pipeline"] == "cascade"
    assert doc["providers"] == trio


# ---------------------------------------------------------------------------
# N-candidate: what the page does when an operator switches BACK to realtime.
# ---------------------------------------------------------------------------

def test_n_switching_back_to_realtime_through_the_page(
        cfg, open_settings, page, monkeypatch):
    import voicecore.profiles as profiles
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {"phone": frozenset({"outbound", "inbound"}), "talk": frozenset({"outbound", "inbound"})})
    trio = {"stt": "deepgram", "llm": "gpt-4.1", "tts": "elevenlabs"}
    cfg.agent("arya", pipeline="cascade", providers=trio, knobs={"voice": "marin"})
    cfg.no_slots()
    page = open_settings()
    page.check('[data-testid="settings-pipeline-realtime"]')
    page.click('[data-testid="settings-save-voice"]')
    page.wait_for_selector('[data-testid="settings-voice-saved"]', timeout=10_000)
    doc = cfg.doc("arya")
    assert doc["pipeline"] == "realtime"
    assert set(doc["providers"]) == {"realtime"}, doc["providers"]


def test_n_switching_to_cascade_without_choosing_providers(
        cfg, open_settings, page, monkeypatch):
    """A realtime Agent has no stt/llm/tts, so the three <select>s open blank.
    What does an operator who checks cascade and presses Save actually get?"""
    import voicecore.profiles as profiles
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {"phone": frozenset({"outbound", "inbound"}), "talk": frozenset({"outbound", "inbound"})})
    cfg.agent("arya", knobs={"voice": "marin"})
    cfg.no_slots()
    page = open_settings()
    page.check('[data-testid="settings-type-custom"]')     # VC24: pipelines live under Advanced
    page.check('[data-testid="settings-pipeline-cascade"]')
    print("STT select value:", repr(page.input_value('[data-testid="settings-stt-provider"]')))
    page.click('[data-testid="settings-save-voice"]')
    page.wait_for_selector(
        '[data-testid="settings-voice-error"], [data-testid="settings-voice-saved"]',
        timeout=10_000)
    shown = page.input_value('[data-testid="settings-stt-provider"]')
    if page.is_visible('[data-testid="settings-voice-error"]'):
        error = page.inner_text('[data-testid="settings-voice-error"]')
        print("ERROR SHOWN:", error[:400])
    else:
        error = ""
        print("SAVED:", cfg.doc("arya"))
    # The screen shows a provider selected; the payload must carry THAT provider.
    assert f"unknown provider id ''" not in error, (
        f"the STT <select> displays {shown!r} but the save sent an empty id: {error[:200]}")


# ---------------------------------------------------------------------------
# N-candidates found by inspection, checked through the page.
# ---------------------------------------------------------------------------

def test_n_an_agent_with_no_voice_gets_one_pinned_by_pressing_save(
        cfg, open_settings, page):
    """The wizard omits `knobs.voice` when the field is left blank, and blank
    means "whatever the bridge already uses" (profile.resolve -> OPENAI_VOICE).
    Opening Settings on such an Agent prefills the proven voice and Save writes
    it - converting a deferring Agent into a pinned one, untouched."""
    cfg.agent("arya")
    doc = yaml.safe_load((cfg.root / "agents" / "arya.yaml").read_text())
    doc.pop("knobs", None)
    (cfg.root / "agents" / "arya.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    cfg.no_slots()
    page = open_settings()
    print("PREFILLED VOICE:", repr(page.input_value('[data-testid="settings-voice"]')))
    page.click('[data-testid="settings-save-voice"]')
    page.wait_for_selector('[data-testid="settings-voice-saved"]', timeout=10_000)
    after = cfg.doc("arya")
    print("AFTER SAVE:", after.get("knobs"))
    assert "knobs" not in after or "voice" not in (after.get("knobs") or {}), (
        "pressing Save without touching anything pinned a voice onto an Agent "
        f"that deliberately had none: {after.get('knobs')}")


def test_n_clearing_the_voice_field_writes_an_empty_voice(cfg, open_settings, page):
    """An operator who wants "use the bridge default" clears the field. What
    lands on disk?"""
    cfg.agent("arya", knobs={"voice": "marin"})
    cfg.no_slots()
    page = open_settings()
    page.fill('[data-testid="settings-voice"]', "")
    page.click('[data-testid="settings-save-voice"]')
    page.wait_for_selector(
        '[data-testid="settings-voice-error"], [data-testid="settings-voice-saved"]',
        timeout=10_000)
    doc = cfg.doc("arya")
    print("AFTER CLEAR:", doc.get("knobs"),
          "| error:", page.is_visible('[data-testid="settings-voice-error"]'))
    assert (doc.get("knobs") or {}).get("voice") != "", (
        f"an empty voice id was written to disk: {doc.get('knobs')}")
