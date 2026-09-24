"""Browser-level tests for the Settings screen (ticket 14).

The ticket's acceptance lines are RENDERING claims about a page that did not
exist until now:

  * the proven path is preselected, no decision needed;
  * every provider, voice and pipeline stays selectable;
  * the advanced providers and voices sit behind a deliberate disclosure;
  * every option carries an honest proven/untested label;
  * voice settings are per-Agent and apply to that Agent's next call;
  * Settings is one consolidated page (providers, voices, Outlet assignment,
    speed dial).

A backend test cannot bind any of those - "proven is true" in a JSON payload
says nothing about whether an operator can SEE it. So these tests open the real
page in a real Chromium, exactly like test_agents_browser, and they assert the
rendered colours, not only class names: a label that never got a stylesheet
rule would pass a class-name test and be invisible to a human.

The classification itself is the backend's (settings_catalog.py) - this file
tests that the screen shows it, not that it is right.
"""
import pytest
import yaml

pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed (requirements-dev.txt)"
)

# The rendered colours the labels are painted with (ui/src/index.css):
# --accent-text for "Proven", --warning-text for "Untested".
PROVEN_GREEN = "rgb(111, 224, 197)"
UNPROVEN_AMBER = "rgb(247, 201, 107)"


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
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "agents").mkdir()

    class Config:
        root = tmp_path

        def agent(self, agent_id, **overrides):
            (tmp_path / "agents" / f"{agent_id}.yaml").write_text(
                yaml.safe_dump(agent_doc(agent_id, **overrides), sort_keys=False))
            return agent_id

        def agent_doc(self, agent_id):
            return yaml.safe_load((tmp_path / "agents" / f"{agent_id}.yaml").read_text())

        def active(self, **outlets):
            (tmp_path / "active.yaml").write_text(
                yaml.safe_dump({"outlets": outlets}, sort_keys=False))

        def stored(self):
            return yaml.safe_load((tmp_path / "active.yaml").read_text())

        def corrupt_speed_dial(self):
            (tmp_path / "speed-dial.yaml").write_text(
                "numbers: [not-a-list\n: broken")

    return Config()


@pytest.fixture
def settings_page(stack, page, config):
    """Settings, on one of its sections: "" (Defaults), "speed-dial" or "advanced"."""
    def _open(section=""):
        running = stack({})
        path = f"/settings/{section}" if section else "/settings"
        page.goto(f"{running.base}{path}", wait_until="networkidle")
        page.wait_for_selector('[data-testid="settings-page"]')
        return page

    return _open


@pytest.fixture
def voice_page(stack, page, config):
    """One Agent's Voice tab. The per-Agent editor that ticket 14 put on Settings
    lives on the Agent's own page since the redesign (A2): the same form, the same
    two narrow endpoints, one Agent per URL instead of a dropdown."""
    def _open(agent_id, tab="voice"):
        running = stack({})
        page.goto(f"{running.base}/agents/{agent_id}/{tab}", wait_until="networkidle")
        page.wait_for_selector('[data-testid="settings-agent-voice"]')
        return page

    return _open


def tag_colour(page, testid):
    return page.eval_on_selector(
        f'[data-testid="{testid}"]',
        "el => getComputedStyle(el).color")


def settings_tag_colour(page, testid):
    return page.eval_on_selector(
        f'[data-testid="{testid}"] .settings-tag',
        "el => getComputedStyle(el).color")


# --------------------------------------------------------------------------
# The page renders its sections, and every one of them is reachable
# --------------------------------------------------------------------------

def test_settings_sections_and_each_agents_voice_are_all_reachable(
        config, settings_page, voice_page):
    config.agent("arya")
    config.active(phone={"inbound": None, "outbound": None},
                  talk={"inbound": None, "outbound": None})
    page = settings_page()

    # Defaults: the proven path, where each Agent's voice now lives, and the
    # Outlet assignment stated read-only.
    assert page.is_visible('[data-testid="settings-proven"]')
    assert page.is_visible('[data-testid="settings-agent-voice-moved"]')
    assert page.is_visible('[data-testid="settings-outlets"]')
    link = page.get_attribute(
        '[data-testid="settings-agent-voice-moved"] a[href="/agents/arya/voice"]', "href")
    assert link == "/agents/arya/voice"

    # The other two sections, one click each.
    page.click('[data-testid="settings-nav-speed-dial"]')
    page.wait_for_selector('[data-testid="settings-speed-dial"]')
    page.click('[data-testid="settings-nav-advanced"]')
    page.wait_for_selector('[data-testid="settings-advanced"]')

    # ...and the per-Agent voice editor, on the Agent's page.
    page = voice_page("arya")
    assert page.is_visible('[data-testid="settings-agent-type"]')


# --------------------------------------------------------------------------
# The proven path is stated first, in the proven colour
# --------------------------------------------------------------------------

def test_the_proven_path_is_stated_and_painted_green(config, settings_page):
    page = settings_page()
    proven = page.inner_text('[data-testid="settings-proven"]')
    assert "realtime" in proven
    assert "openai-gpt-realtime" in proven
    assert "ash" in proven
    # The label is painted: an operator sees Proven before being asked to decide.
    green = page.eval_on_selector(
        '[data-testid="settings-proven"] .settings-tag',
        "el => getComputedStyle(el).color")
    assert green == PROVEN_GREEN


# --------------------------------------------------------------------------
# Per-Agent voice settings, per Agent
# --------------------------------------------------------------------------

def test_changing_one_agents_voice_changes_only_that_agent(
        config, voice_page, page):
    config.agent("arya", knobs={"voice": "cedar"})
    config.agent("brienne", knobs={"voice": "marin"})
    config.active(phone={"inbound": None, "outbound": None},
                  talk={"inbound": None, "outbound": None})
    voice_page("arya")

    page.fill('[data-testid="settings-voice"]', "ash")
    page.click('[data-testid="settings-save-voice"]')
    page.wait_for_selector('[data-testid="settings-voice-saved"]', timeout=10_000)

    # Only Arya's document changed.
    assert config.agent_doc("arya")["knobs"]["voice"] == "ash"
    assert config.agent_doc("brienne")["knobs"]["voice"] == "marin"
    # The saved message says it applies to the NEXT call.
    assert "next call" in page.inner_text('[data-testid="settings-voice-saved"]')


def test_the_proven_voice_is_preselected_in_the_per_agent_editor(
        config, voice_page, page):
    config.agent("arya")
    config.active(phone={"inbound": None, "outbound": None},
                  talk={"inbound": None, "outbound": None})
    page = voice_page("arya")
    # The editor opens with the proven voice, so an operator needs no decision.
    assert page.input_value('[data-testid="settings-voice"]') == "ash"


# B1: opening an Agent must show THAT Agent's voice, and saving without
# touching the voice field must not rewrite it to the proven default. The
# round-1 defect seeded the editor from the catalog's `proven`, so opening an
# Agent whose voice is NOT ash and pressing Save silently changed its voice.
def test_opening_an_agent_shows_that_agents_voice(config, voice_page, page):
    config.agent("arya", knobs={"voice": "cedar"})
    config.active(phone={"inbound": None, "outbound": None},
                  talk={"inbound": None, "outbound": None})
    page = voice_page("arya")
    # The field shows what THIS Agent speaks, not the proven default.
    assert page.input_value('[data-testid="settings-voice"]') == "cedar"


def test_saving_without_touching_the_voice_field_keeps_it(config, voice_page, page):
    config.agent("arya", knobs={"voice": "cedar"})
    config.active(phone={"inbound": None, "outbound": None},
                  talk={"inbound": None, "outbound": None})
    page = voice_page("arya")
    # Change nothing about the voice; press Save. The Agent's voice must stay.
    page.click('[data-testid="settings-save-voice"]')
    page.wait_for_selector('[data-testid="settings-voice-saved"]', timeout=10_000)
    assert config.agent_doc("arya")["knobs"]["voice"] == "cedar"


# B2: cascade must be reachable from the page. The round-1 defect left the
# three cascade dropdowns unread (never sent) and the endpoint unable to
# delete providers.realtime, so switching an Agent to cascade was refused.
def test_switch_to_cascade_through_the_page(config, voice_page, page, monkeypatch):
    import voicecore.profiles as profiles

    config.agent("arya", knobs={"voice": "cedar"})
    config.active(phone={"inbound": None, "outbound": None},
                  talk={"inbound": None, "outbound": None})
    # The phone bridge only activates cascade on a host that flips
    # CASCADE_OUTBOUND_HOST (the same gate the live mode-c host uses, s7).
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {"phone": frozenset({"outbound", "inbound"}), "talk": frozenset({"outbound", "inbound"})})
    page = voice_page("arya")

    # VC24: an outside-vendor cascade is an Advanced choice now (VC7: reorganised, not
    # removed). The pipeline and provider controls are the same ones, one click deeper.
    page.check('[data-testid="settings-type-custom"]')
    page.check('[data-testid="settings-pipeline-cascade"]')
    page.select_option('[data-testid="settings-stt-provider"]', "deepgram")
    page.select_option('[data-testid="settings-llm-provider"]', "gpt-4.1")
    page.select_option('[data-testid="settings-tts-provider"]', "elevenlabs")
    page.click('[data-testid="settings-save-voice"]')
    page.wait_for_selector('[data-testid="settings-voice-saved"]', timeout=10_000)

    doc = config.agent_doc("arya")
    assert doc["pipeline"] == "cascade"
    assert "realtime" not in doc["providers"]
    assert doc["providers"] == {"stt": "deepgram", "llm": "gpt-4.1",
                                "tts": "elevenlabs"}


# --------------------------------------------------------------------------
# Honest labels, painted
# --------------------------------------------------------------------------

def test_every_provider_and_voice_carries_an_honest_painted_label(
        config, settings_page, page):
    page = settings_page("advanced")

    # The Advanced section lists every provider with its label.
    page.wait_for_selector('[data-testid="settings-provider-openai-gpt-realtime"]')

    # The proven provider is painted green; the untested ones amber.
    assert settings_tag_colour(
        page, "settings-provider-openai-gpt-realtime") == PROVEN_GREEN
    assert settings_tag_colour(
        page, "settings-provider-google-gemini-live") == UNPROVEN_AMBER

    # Every provider row carries a label saying which it is.
    labels = page.eval_on_selector_all(
        '[data-testid^="settings-provider-"] .settings-tag',
        "els => els.map(e => e.textContent)")
    assert any("Proven" in label for label in labels)
    assert any("Untested" in label for label in labels)
    assert len(labels) > 3

    # The voice list names the proven voice first, painted green, and the
    # alternatives amber.
    first_voice = page.inner_text('[data-testid="settings-voice-ash"]')
    assert "ash" in first_voice
    assert settings_tag_colour(page, "settings-voice-ash") == PROVEN_GREEN
    assert settings_tag_colour(page, "settings-voice-cedar") == UNPROVEN_AMBER


def test_every_option_stays_selectable(config, voice_page, page):
    config.agent("arya")
    config.active(phone={"inbound": None, "outbound": None},
                  talk={"inbound": None, "outbound": None})
    page = voice_page("arya")

    # VC24: the page opens on WHAT YOU ARE TALKING TO. A realtime Agent reads as the
    # proven type, the direct Hermes lane is offered beside it, and Advanced is the way
    # to everything else. VC7 is the bar: reorganised, and nothing below may go missing.
    assert page.is_checked('[data-testid="settings-type-realtime"]')
    assert page.is_enabled('[data-testid="settings-type-hermes-direct"]')
    assert not page.is_visible('[data-testid="settings-pipeline-realtime"]')
    page.check('[data-testid="settings-type-custom"]')

    # Both pipelines are offered, and the unproven one is selectable.
    assert page.is_checked('[data-testid="settings-pipeline-realtime"]')

    # The provider dropdown lists every provider, proven or not.
    options = page.eval_on_selector(
        '[data-testid="settings-realtime-provider"]',
        "el => Array.from(el.options).map(o => o.value)")
    assert "openai-gpt-realtime" in options
    assert "google-gemini-live" in options

    # And the cascade lane (the advanced pipeline) stays selectable too.
    page.check('[data-testid="settings-pipeline-cascade"]')
    assert page.is_checked('[data-testid="settings-pipeline-cascade"]')


# --------------------------------------------------------------------------
# Outlet assignment is stated here and written only on the Agents screen (T2)
# --------------------------------------------------------------------------

def test_outlet_assignment_is_stated_here_and_changed_on_agents(config, settings_page, page):
    config.agent("arya")
    config.active(phone={"inbound": "arya", "outbound": None},
                  talk={"inbound": None, "outbound": None})
    page = settings_page()

    phone = page.inner_text('[data-testid="settings-outlet-phone"]')
    assert "arya" in phone
    assert page.is_visible('[data-testid="settings-outlet-talk"]')

    # Read-only: the Agents screen is the ONLY surface that writes an assignment
    # (ticket 02), so Settings offers no control that could disagree with it...
    assert page.query_selector('[data-testid="settings-outlets"] select') is None
    assert page.query_selector('[data-testid^="settings-slot-select-"]') is None
    # ...and says where the control is.
    page.click('[data-testid="settings-outlets"] a[href="/agents"]')
    page.wait_for_selector('[data-testid="slot-select-phone-inbound"]')
    assert page.input_value('[data-testid="slot-select-phone-inbound"]') == "arya"


# A mis-shaped speed-dial.yaml must degrade ONE card, not the whole Settings
# page. The round-1 defect surfaced the file's parse error as a failed page.
# Ticket 09's module (PR #18) treats a corrupt file as an empty saved list, so
# the card renders as empty - the point is the rest of the page stays alive.
def test_a_bad_speed_dial_file_degrades_only_that_card(
        config, settings_page, page):
    config.agent("arya")
    config.active(phone={"inbound": None, "outbound": None},
                  talk={"inbound": None, "outbound": None})
    config.corrupt_speed_dial()
    page = settings_page()

    # The rest of Settings is alive: proven path, Agent voice links, outlets.
    assert page.is_visible('[data-testid="settings-proven"]')
    assert page.is_visible('[data-testid="settings-agent-voice-moved"]')
    assert page.is_visible('[data-testid="settings-outlet-phone"]')
    # The speed-dial section degrades to an empty list instead of the page failing.
    page.click('[data-testid="settings-nav-speed-dial"]')
    page.wait_for_selector('[data-testid="settings-speed-dial"]')
    assert page.is_visible('[data-testid="settings-speed-dial"]')