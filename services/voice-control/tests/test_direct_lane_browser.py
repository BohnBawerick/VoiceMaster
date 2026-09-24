"""Browser-level tests for VC24 on an Agent's Voice and Tools tabs: choosing what you talk
to, which Hermes profile it is, its ElevenLabs voice and how Deepgram listens. (Ticket 18
put this editor on Settings behind an Agent dropdown; the redesign moved it, unchanged in
what it sends, to /agents/<id>/voice.)

The owner's ask was a SCREEN ("within VoiceMaster web, I can select..."), so these open
the real page in a real Chromium, like test_settings_browser. Two habits are kept from
the suites around it. **Assert the disk, not only the screen**: what matters about a
click is the Agent document the bridges will read. **Assert the rendered colour, not the
class name**: the lane is unproven, and an operator has to be able to SEE that.
"""
import json

import pytest
import yaml

pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed (requirements-dev.txt)"
)

from test_settings_browser import (  # noqa: E402,F401 - fixtures and helpers
    PROVEN_GREEN, config, settings_page, voice_page)

DIRECT = {"stt": "deepgram", "llm": "hermes-agent", "tts": "elevenlabs"}
VOICES = {"source": "account", "default": "v-rachel", "detail": None,
          "voices": [{"id": "v-rachel", "name": "Rachel"}, {"id": "v-adam", "name": "Adam"}]}


@pytest.fixture
def hermes(config, tmp_path, monkeypatch):  # noqa: F811
    """Two real profile directories and a registry in which only `vega` is running."""
    home = tmp_path / "hermes-profiles"
    for name in ("vega", "scout"):
        (home / name).mkdir(parents=True)
        (home / name / "config.yaml").write_text("model: x\n")
    (tmp_path / "gateways").mkdir()
    (tmp_path / "gateways" / "gateways.json").write_text(json.dumps({"profiles": {
        "vega": {"status": "ok", "gateway_url": "http://127.0.0.1:18791"},
        "scout": {"status": "crashed", "gateway_url": "http://127.0.0.1:18790"}}}))
    monkeypatch.setenv("HERMES_PROFILES_DIR", str(home))
    monkeypatch.setenv("HERMES_GATEWAY_URL", "http://hermes:18789")
    monkeypatch.delenv("HERMES_PROFILE_GATEWAY_URLS", raising=False)
    config.active(phone={"inbound": None, "outbound": None},
                  talk={"inbound": None, "outbound": None})
    return config


def _with_voices(page, body=VOICES):
    page.route("**/api/providers/elevenlabs/voices",
               lambda route: route.fulfill(status=200, content_type="application/json",
                                           body=json.dumps(body)))


def _save(page):
    page.click('[data-testid="settings-save-voice"]')
    page.wait_for_selector(
        '[data-testid="settings-voice-error"], [data-testid="settings-voice-saved"]',
        timeout=10_000)


def test_one_click_makes_an_agent_hermes_itself(hermes, voice_page, page):
    """The whole ask, through the page: pick Hermes directly, pick the profile, pick the
    ElevenLabs voice, tune Deepgram, Save - then read the document the bridges will."""
    hermes.agent("robot", hermes_profile="default")
    _with_voices(page)
    page = voice_page("robot")

    page.check('[data-testid="settings-type-hermes-direct"]')
    # Real phone calls proved the lane (O5), and the page says so in the proven colour,
    # not just a class.
    assert page.eval_on_selector(
        '[data-testid="settings-agent-type"] label:has([data-testid="settings-type-hermes-direct"]) .settings-tag',
        "el => getComputedStyle(el).color") == PROVEN_GREEN
    assert page.eval_on_selector(
        '[data-testid="settings-agent-type"] label:has([data-testid="settings-type-realtime"]) .settings-tag',
        "el => getComputedStyle(el).color") == PROVEN_GREEN

    page.select_option('[data-testid="settings-hermes-profile"]', "vega")
    page.select_option('[data-testid="settings-eleven-voice"]', "v-adam")
    # Ticket 21: ElevenLabs hears by default, and its own Listening card is shown.
    assert page.input_value('[data-testid="settings-hermes-stt"]') == "elevenlabs-scribe"
    assert not page.is_visible('[data-testid="settings-listening"]')
    page.fill('[data-testid="settings-scribe-language"]', "en-AU")
    page.fill('[data-testid="settings-scribe-keyterms"]', "Hermes, Alex ,  Perth")
    _save(page)
    assert page.is_visible('[data-testid="settings-voice-saved"]'), page.inner_text(
        '[data-testid="settings-voice-error"]')

    doc = hermes.agent_doc("robot")
    assert doc["pipeline"] == "cascade"
    assert doc["providers"] == dict(DIRECT, stt="elevenlabs-scribe")
    assert doc["hermes_profile"] == "vega"
    assert doc["guardrails"]["on_call_tools"] is True
    assert doc["knobs"] == {"voice": "v-adam", "language": "en-AU",
                            "keyterms": ["Hermes", "Alex", "Perth"]}


def test_a_deepgram_agent_can_move_to_elevenlabs_ears_and_back(hermes, voice_page, page):
    """The owner's Agent hears through Deepgram today. Switching it keeps the names to
    listen for and drops the Deepgram-only settings (a Deepgram model sent to ElevenLabs
    would be a model it does not have) and the language (review S3: a code one provider
    takes can end the other's session); Deepgram stays a choice."""
    hermes.agent("robot", pipeline="cascade", providers=dict(DIRECT), hermes_profile="vega",
                 knobs={"voice": "v-adam", "transcription_model": "nova-3",
                        "language": "en", "keyterms": ["Hermes"], "numerals": False})
    _with_voices(page)
    page = voice_page("robot")
    assert page.is_checked('[data-testid="settings-type-hermes-direct"]')
    assert page.input_value('[data-testid="settings-hermes-stt"]') == "deepgram"
    page.select_option('[data-testid="settings-hermes-stt"]', "elevenlabs-scribe")
    assert page.input_value('[data-testid="settings-scribe-keyterms"]') == "Hermes"
    _save(page)
    doc = hermes.agent_doc("robot")
    assert doc["providers"] == dict(DIRECT, stt="elevenlabs-scribe")
    assert doc["knobs"] == {"voice": "v-adam", "keyterms": ["Hermes"]}

    # A save re-reads the roster and re-seeds the form; start the second change from a
    # fresh page so it cannot race that re-seed.
    page = voice_page("robot")
    assert page.input_value('[data-testid="settings-hermes-stt"]') == "elevenlabs-scribe"
    page.select_option('[data-testid="settings-hermes-stt"]', "deepgram")
    assert page.is_visible('[data-testid="settings-listening"]')
    _save(page)
    assert hermes.agent_doc("robot")["providers"] == DIRECT


def test_the_old_voice_does_not_follow_the_agent_to_another_provider(
        hermes, voice_page, page):
    """'ash' is an OpenAI voice name. Carried into an ElevenLabs Agent it would save a
    voice that cannot be spoken, and the first anyone knew would be a silent call."""
    hermes.agent("robot", hermes_profile="vega", knobs={"voice": "ash"})
    _with_voices(page)
    page = voice_page("robot")
    page.check('[data-testid="settings-type-hermes-direct"]')
    assert page.input_value('[data-testid="settings-eleven-voice"]') == ""
    _save(page)
    assert "voice" not in (hermes.agent_doc("robot").get("knobs") or {})


def test_opening_and_saving_a_direct_agent_changes_nothing(hermes, voice_page, page):
    """Seeded from the Agent's own document, never from a default. A voice the account no
    longer lists stays selected, and 'inherit' booleans send nothing."""
    before = hermes.agent("robot", pipeline="cascade", providers=dict(DIRECT),
                          hermes_profile="vega", guardrails={"on_call_tools": False},
                          knobs={"voice": "v-gone", "keyterms": ["Hermes"], "numerals": False})
    original = hermes.agent_doc(before)
    _with_voices(page)
    page = voice_page("robot")
    assert page.is_checked('[data-testid="settings-type-hermes-direct"]')
    assert page.input_value('[data-testid="settings-hermes-profile"]') == "vega"
    assert page.input_value('[data-testid="settings-eleven-voice"]') == "v-gone"
    assert page.input_value('[data-testid="settings-stt-keyterms"]') == "Hermes"
    assert page.input_value('[data-testid="settings-stt-numerals"]') == "off"
    assert page.input_value('[data-testid="settings-stt-smart-format"]') == "inherit"
    _save(page)
    assert hermes.agent_doc("robot") == original


def test_a_deepgram_option_can_be_cleared_again(hermes, voice_page, page):
    hermes.agent("robot", pipeline="cascade", providers=dict(DIRECT), hermes_profile="vega",
                 knobs={"voice": "v-adam", "language": "en-AU", "keyterms": ["Hermes"],
                        "smart_format": True})
    _with_voices(page)
    page = voice_page("robot")
    page.fill('[data-testid="settings-stt-language"]', "")
    page.fill('[data-testid="settings-stt-keyterms"]', "")
    page.select_option('[data-testid="settings-stt-smart-format"]', "inherit")
    _save(page)
    assert hermes.agent_doc("robot")["knobs"] == {"voice": "v-adam"}


def test_no_voice_list_is_said_out_loud_and_a_voice_id_can_still_be_typed(
        hermes, voice_page, page):
    """The catalog endpoint answers `unavailable` rather than a fake list (no key here).
    The page must degrade the ONE control and say why, never the card."""
    hermes.agent("robot", pipeline="cascade", providers=dict(DIRECT), hermes_profile="vega")
    page = voice_page("robot")
    assert page.eval_on_selector('[data-testid="settings-eleven-voice"]',
                                 "el => el.tagName") == "INPUT"
    assert "No voice list" in page.inner_text('[data-testid="settings-eleven-voice-source"]')
    page.fill('[data-testid="settings-eleven-voice"]', "21m00Tcm4TlvDq8ikWAM")
    _save(page)
    assert hermes.agent_doc("robot")["knobs"]["voice"] == "21m00Tcm4TlvDq8ikWAM"


def test_a_profile_nobody_is_running_is_visible_before_and_refused_after(
        hermes, voice_page, page):
    """The picker says `scout` is not running. On an Agent that holds an Outlet the
    API then refuses it, the refusal is shown in the API's own words, and because the
    profile is written FIRST, nothing else about the Agent was changed either."""
    hermes.agent("robot", pipeline="cascade", providers=dict(DIRECT), hermes_profile="vega",
                 knobs={"voice": "v-adam"})
    hermes.active(phone={"inbound": "robot", "outbound": None},
                  talk={"inbound": None, "outbound": None})
    original = hermes.agent_doc("robot")
    _with_voices(page)
    page = voice_page("robot")
    option = page.inner_text('[data-testid="settings-hermes-profile"] option[value="scout"]')
    assert "not running" in option and "crashed" in option
    page.select_option('[data-testid="settings-hermes-profile"]', "scout")
    assert page.is_visible('[data-testid="settings-profile-unroutable"]')
    page.select_option('[data-testid="settings-eleven-voice"]', "v-rachel")
    _save(page)
    error = page.inner_text('[data-testid="settings-voice-error"]')
    assert "outlets.phone.inbound" in error and "no running gateway" in error
    assert hermes.agent_doc("robot") == original


def test_the_switch_cannot_take_the_phone_away_from_the_agent_answering_it(
        hermes, voice_page, page):
    hermes.agent("robot", hermes_profile="vega")
    hermes.active(phone={"inbound": "robot", "outbound": None},
                  talk={"inbound": None, "outbound": None})
    original = hermes.agent_doc("robot")
    page = voice_page("robot")
    page.check('[data-testid="settings-type-custom"]')
    page.check('[data-testid="settings-pipeline-cascade"]')
    page.select_option('[data-testid="settings-llm-provider"]', "gpt-4.1")
    _save(page)
    error = page.inner_text('[data-testid="settings-voice-error"]')
    assert "outlets.phone.inbound" in error and "Unassign" in error
    assert hermes.agent_doc("robot") == original
    assert yaml.safe_load((hermes.root / "active.yaml").read_text())[
        "outlets"]["phone"]["inbound"] == "robot"


@pytest.mark.parametrize("stored", [None, True, False], ids=["missing", "on", "off"])
def test_live_missing_tools_control_and_reload(hermes, voice_page, page, stored):
    fields = {} if stored is None else {"guardrails": {"on_call_tools": stored}}
    hermes.agent("robot", pipeline="cascade", providers=dict(DIRECT),
                 hermes_profile="vega", **fields)
    hermes.agent("unrelated", guardrails={"on_call_tools": False})
    before = hermes.agent_doc("unrelated")
    _with_voices(page)
    page = voice_page("robot", "tools")
    control = page.get_by_role("switch", name="Allow Hermes to use tools")
    voice_tab = '[data-testid="agent-tab-voice"]'
    tools_tab = '[data-testid="agent-tab-tools"]'
    assert control.is_visible()
    assert control.is_checked() is (stored is not False)
    # Voice and Tools are two tabs of ONE editor: a draft made on one is saved from
    # the other.
    page.click(voice_tab)
    page.select_option('[data-testid="settings-hermes-profile"]', "default")
    page.click(tools_tab)
    for enabled in (False, True):
        control.set_checked(enabled)
        _save(page)
        assert page.is_visible('[data-testid="settings-voice-saved"]')
        page.reload(wait_until="networkidle")
        assert control.is_checked() is enabled
        page.click(voice_tab)
        assert page.input_value('[data-testid="settings-hermes-profile"]') == "default"
        page.click(tools_tab)
        assert hermes.agent_doc("robot")["guardrails"]["on_call_tools"] is enabled
    assert hermes.agent_doc("unrelated") == before
    page.click('.master-item:has-text("unrelated")')
    page.wait_for_url("**/agents/unrelated/tools")
    assert not control.is_visible()
    page.click(voice_tab)
    page.check('[data-testid="settings-type-hermes-direct"]')
    page.click(tools_tab)
    assert control.is_visible() and not control.is_checked()
    page.click(voice_tab)
    page.check('[data-testid="settings-type-custom"]')
    page.select_option('[data-testid="settings-llm-provider"]', "gpt-4.1")
    page.click(tools_tab)
    assert not control.is_visible()
    _save(page)
    assert hermes.agent_doc("unrelated")["guardrails"]["on_call_tools"] is False


def test_profile_list_failure_keeps_default_and_current_profile(hermes, voice_page, page):
    hermes.agent("robot", pipeline="cascade", providers=dict(DIRECT), hermes_profile="vega")
    page.route("**/api/hermes", lambda route: route.fulfill(status=503, body="unavailable"))
    _with_voices(page)
    page = voice_page("robot")
    options = page.locator('[data-testid="settings-hermes-profile"] option').evaluate_all(
        "nodes => nodes.map(n => n.value)")
    assert options == ["default", "vega"]
    assert page.input_value('[data-testid="settings-hermes-profile"]') == "vega"


def test_only_real_agents_appear_in_roster_call_and_all_outlet_choices(
        hermes, voice_page, page):
    hermes.agent("robot", pipeline="cascade", providers=dict(DIRECT), hermes_profile="default")
    page = voice_page("robot")
    base = page.url.removesuffix("/agents/robot/voice")
    # Settings only states the assignment (T2); the Agents screen is where it is chosen.
    page.goto(f"{base}/settings", wait_until="networkidle")
    assert "Hermes (default profile)" not in page.inner_text("main")
    for route, prefix in (("agents", "slot-select"),):
        page.goto(f"{base}/{route}", wait_until="networkidle")
        assert "Hermes (default profile)" not in page.inner_text("main")
        assert page.locator('[data-testid="agent-card-default"]').count() == 0
        for outlet in ("phone", "talk"):
            for direction in ("inbound", "outbound"):
                select = page.locator(f'[data-testid="{prefix}-{outlet}-{direction}"]')
                assert select.locator("option").evaluate_all(
                    "nodes => nodes.map(n => n.value)") == ["", "robot"]
                select.select_option("robot")
                page.wait_for_function(
                    "async () => (await (await fetch('/api/active')).json())"
                    f".outlets.{outlet}.{direction} === 'robot'")
                page.reload(wait_until="networkidle")
                assert select.input_value() == "robot"
    page.goto(f"{base}/place", wait_until="networkidle")
    options = page.locator('[data-testid="place-agent"] option').evaluate_all(
        "nodes => nodes.map(n => n.value)")
    assert options == ["", "robot"]
    assert "Hermes (default profile)" not in page.inner_text("main")


def test_the_voice_provider_is_a_choice_too_and_aura_takes_its_own_voice(
        hermes, voice_page, page):
    """Review S5: the owner wants either stage switchable on "Hermes directly". The
    ElevenLabs voice id does not follow the Agent to Aura (Aura could not speak it)."""
    hermes.agent("robot", pipeline="cascade", providers=dict(DIRECT, stt="elevenlabs-scribe"),
                 hermes_profile="vega", knobs={"voice": "v-adam"})
    _with_voices(page)
    page = voice_page("robot")
    assert page.input_value('[data-testid="settings-hermes-tts"]') == "elevenlabs"
    page.select_option('[data-testid="settings-hermes-tts"]', "deepgram-aura")
    assert not page.is_visible('[data-testid="settings-eleven-voice"]')
    assert page.input_value('[data-testid="settings-aura-voice"]') == ""
    _save(page)
    doc = hermes.agent_doc("robot")
    assert doc["providers"] == dict(DIRECT, stt="elevenlabs-scribe", tts="deepgram-aura")
    assert "voice" not in (doc.get("knobs") or {})

    page = voice_page("robot")
    assert page.is_checked('[data-testid="settings-type-hermes-direct"]')
    page.select_option('[data-testid="settings-aura-voice"]', "aura-2-luna-en")
    _save(page)
    assert hermes.agent_doc("robot")["knobs"] == {"voice": "aura-2-luna-en"}


def test_changing_the_ears_clears_a_language_the_other_provider_would_refuse(
        hermes, voice_page, page):
    """Review S3: Deepgram's `multi` ends a Scribe session. The engine never sends it,
    and the page does not carry it across a Hearing change either."""
    hermes.agent("robot", pipeline="cascade", providers=dict(DIRECT), hermes_profile="vega",
                 knobs={"language": "multi"})
    _with_voices(page)
    page = voice_page("robot")
    assert page.input_value('[data-testid="settings-stt-language"]') == "multi"
    page.select_option('[data-testid="settings-hermes-stt"]', "elevenlabs-scribe")
    assert page.input_value('[data-testid="settings-scribe-language"]') == ""
    _save(page)
    assert "language" not in (hermes.agent_doc("robot").get("knobs") or {})
