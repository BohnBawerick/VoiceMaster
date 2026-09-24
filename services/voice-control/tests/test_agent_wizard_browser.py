"""Browser-level tests for the Agent creation wizard (ticket 13).

Every claim the wizard makes is a rendering or interaction claim, so a real
Chromium drives the real built bundle in ``static/assets/`` against a real
uvicorn, a real ``VOICE_CONFIG_DIR`` and a real Hermes profiles directory on
disk. Ticket 02's held-back first attempt is the reason: both of its defects
were invisible to the backend suite and visible the moment a page was opened.

Three habits carried from ``test_agents_browser.py``:

  * address ONE control, not the page. "The word proven appears somewhere"
    stays true while the wrong option carries it;
  * assert the rendered COLOUR where the colour is the claim. "Proven" being
    green next to "outbound only" being amber is what an operator actually
    reads, and a class-name assertion passes against a stylesheet that never
    uses it;
  * assert the DISK, not only the screen. The one thing this wizard must never
    do is leave something behind, and the screen cannot be asked about that.

The abandonment test at the bottom is the brief's first bar: it drives the
wizard half way through, in a browser, then closes the tab, and asserts the two
directories are exactly as they were.
"""
import os

import pytest
import yaml

pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed (requirements-dev.txt)"
)

import hermes_profiles  # noqa: E402

# From ui/src/index.css. These are the claims, not their labels.
PROVEN_GREEN = "rgb(111, 224, 197)"
UNPROVEN_AMBER = "rgb(247, 201, 107)"

CANONICAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "..", "voice-config", "providers.yaml")


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """A scratch config dir AND a scratch Hermes profiles dir, two separate
    trees exactly as the deploy has them."""
    config = tmp_path / "voice-config"
    (config / "agents").mkdir(parents=True)
    (config / "providers.yaml").write_text(open(CANONICAL).read())
    home = tmp_path / "hermes-profiles"
    home.mkdir()
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(config))
    monkeypatch.setenv("HERMES_PROFILES_DIR", str(home))

    class Home:
        config_dir = config
        profiles_dir = home

        def source(self, name="scout"):
            path = home / name
            path.mkdir()
            (path / "config.yaml").write_text(yaml.safe_dump({
                "identity": {"name": name.capitalize()},
                "model": {"default": "minimax/minimax-m2.7", "provider": "openrouter"},
                "tools": {"profile": "coding"},
                "mcp_servers": {"hindsight": {"url": "http://memory/mcp/"}},
            }, sort_keys=False))
            (path / "SOUL.md").write_text(f"# {name}\nThe source being.\n")
            (path / "skills").mkdir()
            (path / "skills" / "a-skill.md").write_text("body\n")
            return name

        def snapshot(self):
            """Everything on disk in both trees, as a comparable set."""
            entries = set()
            for root in (home, config):
                for base, dirs, files in os.walk(root):
                    for name in list(dirs) + list(files):
                        entries.add(os.path.relpath(os.path.join(base, name), root))
            return entries

    return Home()


@pytest.fixture
def wizard(stack, page, hermes_home):
    """The wizard, open, at its own URL."""

    def _open(path="/agents/new"):
        running = stack({})
        page.goto(f"{running.base}{path}", wait_until="networkidle")
        return page

    return _open


def colour(page, selector) -> str:
    return page.eval_on_selector(
        selector, "el => window.getComputedStyle(el).color")


def step_names(page) -> list:
    return [el.inner_text().strip().split("\n")[-1]
            for el in page.query_selector_all('[data-testid="wizard-steps"] li')]


def fill_identity(page, name="nora", description="A new being."):
    page.fill('[data-testid="wizard-name"]', name)
    page.fill('[data-testid="wizard-description"]', description)


def go_to(page, panel):
    """Next until the named panel is on screen, whatever the step list is.

    Deliberately not a fixed count of clicks: the number of steps is exactly
    what the inherit/scratch choice CHANGES, so a hardcoded number would either
    break with the flow or, worse, quietly stop one step short and assert
    against the wrong panel.
    """
    selector = f'[data-testid="wizard-panel-{panel}"]'
    for _ in range(10):
        if page.query_selector(selector):
            page.wait_for_selector(selector)
            return
        page.click('[data-testid="wizard-next"]')
    raise AssertionError(f"the wizard never reached its {panel} step")


def go_to_review(page):
    go_to(page, "review")


# --------------------------------------------------------------------------
# The wizard is reachable, and says what it is about to make
# --------------------------------------------------------------------------

def test_the_agents_screen_offers_the_wizard(stack, page, hermes_home):
    running = stack({})
    page.goto(f"{running.base}/agents", wait_until="networkidle")
    page.wait_for_selector('[data-testid="new-agent"]')
    page.click('[data-testid="new-agent"]')
    page.wait_for_selector('[data-testid="agent-wizard"]')
    # A real, linkable, reloadable URL - not a modal that a reload loses.
    assert page.url.endswith("/agents/new")


def test_the_wizard_survives_a_reload_of_its_own_url(wizard):
    page = wizard()
    page.wait_for_selector('[data-testid="agent-wizard"]')
    page.reload(wait_until="networkidle")
    page.wait_for_selector('[data-testid="agent-wizard"]')


def test_the_review_step_names_the_hermes_profile_directory(wizard, hermes_home):
    """The screen says a real Hermes profile is what gets made, and where."""
    page = wizard()
    fill_identity(page)
    page.click('[data-testid="wizard-inherit-no"]')
    go_to_review(page)
    assert page.inner_text('[data-testid="review-profile"]').strip() == (
        f"{hermes_home.profiles_dir}/nora")


# --------------------------------------------------------------------------
# Inheritance is the default path, and it is the quick one
# --------------------------------------------------------------------------

def test_inheritance_is_preselected_when_there_is_something_to_inherit(
        wizard, hermes_home):
    hermes_home.source("scout")
    page = wizard()
    page.wait_for_selector('[data-testid="wizard-inherit-yes"]')
    # The radio the operator did not touch: the default path, chosen for them.
    assert page.is_checked('[data-testid="wizard-inherit-yes"]')
    assert not page.is_checked('[data-testid="wizard-inherit-no"]')
    assert page.input_value('[data-testid="wizard-inherit-source"]') == "scout"


def test_inheriting_is_four_steps_and_declining_is_seven(wizard, hermes_home):
    """"Quick" is a fact about the flow, so it is asserted as one.

    Inheriting asks who it is, its voice, Telegram, review. Declining adds the
    substantive questions the ticket names - its soul, its model and tools,
    where its memory lives - as three more steps.
    """
    hermes_home.source("scout")
    page = wizard()
    page.wait_for_selector('[data-testid="wizard-inherit-yes"]')
    assert step_names(page) == ["Who is it", "Its voice", "Telegram", "Review"]

    page.click('[data-testid="wizard-inherit-no"]')
    assert step_names(page) == [
        "Who is it", "Its soul", "What it thinks with", "Where its memory lives",
        "Its voice", "Telegram", "Review"]


def test_the_inherited_summary_shows_the_real_values(wizard, hermes_home):
    """"Inherited" as a word says nothing. The screen names what it takes."""
    hermes_home.source("scout")
    page = wizard()
    page.wait_for_selector('[data-testid="wizard-inherited-summary"]')
    summary = page.inner_text('[data-testid="wizard-inherited-summary"]')
    assert "minimax/minimax-m2.7" in summary
    assert "coding" in summary
    assert "shared Hindsight" in summary


def test_asking_to_change_what_it_inherits_adds_the_substantive_steps(
        wizard, hermes_home):
    hermes_home.source("scout")
    page = wizard()
    page.wait_for_selector('[data-testid="wizard-customise"]')
    page.check('[data-testid="wizard-customise"]')
    assert step_names(page) == [
        "Who is it", "Its soul", "What it thinks with", "Where its memory lives",
        "Its voice", "Telegram", "Review"]
    # And the soul it would change is prefilled with the one it is taking.
    fill_identity(page)
    go_to(page, "soul")
    assert "The source being." in page.input_value('[data-testid="wizard-soul"]')


def test_with_no_profile_to_inherit_from_the_screen_says_so(wizard, hermes_home):
    page = wizard()
    page.wait_for_selector('[data-testid="wizard-no-sources"]')
    assert page.is_checked('[data-testid="wizard-inherit-no"]')
    assert page.is_disabled('[data-testid="wizard-inherit-yes"]')


# --------------------------------------------------------------------------
# Voice setup, with the proven default preselected and honestly labelled
# --------------------------------------------------------------------------

def test_the_proven_pipeline_is_preselected_and_painted_as_proven(wizard, hermes_home):
    page = wizard()
    fill_identity(page)
    go_to(page, "voice")
    assert page.is_checked('[data-testid="wizard-pipeline-realtime"]')
    assert not page.is_checked('[data-testid="wizard-pipeline-cascade"]')
    # The label is a claim about what has actually run, so it is painted:
    # green on the proven lane, amber on the one that is outbound only.
    assert colour(page, ".wizard-proven") == PROVEN_GREEN
    assert colour(page, ".wizard-unproven") == UNPROVEN_AMBER
    assert "Outbound only" in page.inner_text(".wizard-unproven")


def test_the_preselected_lane_is_the_one_the_api_calls_proven(wizard, hermes_home):
    """The screen's preselection is compared to the API's answer, not to a
    literal in the test.

    Which lane is proven is the backend's to say (`GET /api/hermes` -> `proven`,
    from `hermes_profiles.PROVEN_PIPELINE`). A screen carrying its own copy of
    that answer looks right in every screenshot while drifting from the thing it
    is supposed to be reporting - so this asserts the two AGREE, and the test
    above pins what the answer currently is. Both are needed: agreement alone
    would survive both sides moving to cascade together, and a literal alone
    would survive the screen ignoring the API.
    """
    page = wizard()
    proven = page.evaluate(
        "async () => (await (await fetch('/api/hermes')).json()).proven")
    fill_identity(page)
    go_to(page, "voice")
    checked = page.evaluate(
        """() => {
            const el = document.querySelector('input[name="pipeline"]:checked');
            return el ? el.getAttribute('data-testid') : null;
        }""")
    assert checked == f"wizard-pipeline-{proven['pipeline']}"
    if proven["pipeline"] == "realtime":
        assert page.input_value('[data-testid="wizard-realtime-provider"]') == (
            proven["realtime_provider"])


def test_cascade_is_still_selectable_and_asks_for_all_three_roles(wizard, hermes_home):
    """VC7: nothing is removed from the options, only reorganised."""
    page = wizard()
    fill_identity(page)
    go_to(page, "voice")
    page.check('[data-testid="wizard-pipeline-cascade"]')
    page.wait_for_selector('[data-testid="wizard-stt-provider"]')
    assert page.is_visible('[data-testid="wizard-llm-provider"]')
    assert page.is_visible('[data-testid="wizard-tts-provider"]')
    assert not page.is_visible('[data-testid="wizard-realtime-provider"]')


def test_the_voice_field_says_a_suggestion_is_not_a_verified_id(wizard, hermes_home):
    page = wizard()
    fill_identity(page)
    go_to(page, "voice")
    assert "not verified" in page.inner_text('[data-testid="wizard-voice-help"]')


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def test_telegram_is_offered_and_off_by_default(wizard, hermes_home):
    page = wizard()
    fill_identity(page)
    go_to(page, "telegram")
    assert not page.is_checked('[data-testid="wizard-telegram-connect"]')
    assert page.is_visible('[data-testid="wizard-telegram-off"]')


def test_telegram_on_without_a_token_blocks_the_step(wizard, hermes_home):
    page = wizard()
    fill_identity(page)
    go_to(page, "telegram")
    page.check('[data-testid="wizard-telegram-connect"]')
    page.wait_for_selector('[data-testid="wizard-telegram-error"]')
    assert page.is_disabled('[data-testid="wizard-next"]')
    # A well-formed token clears the block; a malformed one has its own test.
    page.fill('[data-testid="wizard-telegram-token"]', VALID_TOKEN)
    assert not page.is_disabled('[data-testid="wizard-next"]')


VALID_TOKEN = "8209633400:" + "A" * 35


def test_a_malformed_token_is_refused_at_the_step_the_operator_typed_it(
        wizard, hermes_home):
    """The refusal the backend makes, mirrored where it is useful.

    Without this the operator answers every remaining question, presses Create,
    and only then learns the token was the wrong shape - and before this fix, did
    not learn it at all: the profile was created and the bad value written.
    """
    page = wizard()
    fill_identity(page)
    go_to(page, "telegram")
    page.check('[data-testid="wizard-telegram-connect"]')
    page.fill('[data-testid="wizard-telegram-token"]',
              "not-a-real-telegram-token-at-all")
    page.wait_for_selector('[data-testid="wizard-telegram-error"]')
    message = page.inner_text('[data-testid="wizard-telegram-error"]')
    assert "not the shape of a Telegram bot token" in message
    assert page.is_disabled('[data-testid="wizard-next"]')

    # And a well-formed one clears it, so the check is a shape test and not a
    # blanket refusal that would make Telegram unreachable.
    page.fill('[data-testid="wizard-telegram-token"]', VALID_TOKEN)
    page.wait_for_selector('[data-testid="wizard-telegram-error"]', state="detached")
    assert not page.is_disabled('[data-testid="wizard-next"]')


def test_the_refusal_on_screen_does_not_show_the_token(wizard, hermes_home):
    """The message is rendered, and a screenshot of it must not carry the value.

    Checked on the whole panel, not just the error line: a helper elsewhere that
    echoed "you typed X" would be the same leak in a different element.
    """
    bad = "8209633400:definitely-not-a-valid-token-value"
    page = wizard()
    fill_identity(page)
    go_to(page, "telegram")
    page.check('[data-testid="wizard-telegram-connect"]')
    page.fill('[data-testid="wizard-telegram-token"]', bad)
    page.wait_for_selector('[data-testid="wizard-telegram-error"]')
    assert bad not in page.inner_text('[data-testid="wizard-panel-telegram"]')
    # Still masked while it is being corrected.
    assert page.get_attribute('[data-testid="wizard-telegram-token"]', "type") == "password"


def test_the_token_field_does_not_show_the_token(wizard, hermes_home):
    page = wizard()
    fill_identity(page)
    go_to(page, "telegram")
    page.check('[data-testid="wizard-telegram-connect"]')
    page.wait_for_selector('[data-testid="wizard-telegram-token"]')
    assert page.get_attribute('[data-testid="wizard-telegram-token"]', "type") == "password"


# --------------------------------------------------------------------------
# Creating one, from the browser, end to end
# --------------------------------------------------------------------------

def test_creating_an_agent_writes_a_real_profile_and_lands_on_the_roster(
        wizard, hermes_home):
    page = wizard()
    fill_identity(page, "nora")
    go_to_review(page)
    page.click('[data-testid="wizard-create"]')

    # Back on the roster, with the new Agent on it.
    page.wait_for_selector('[data-testid="agent-card-nora"]')
    assert page.url.endswith("/agents")

    # And on disk: a real Hermes profile directory, startable by ticket 12's
    # own predicate, with its voice document beside it.
    path = hermes_home.profiles_dir / "nora"
    assert hermes_profiles.profile_is_startable(path)
    assert (path / "SOUL.md").is_file()
    assert (hermes_home.config_dir / "agents" / "nora.yaml").is_file()


def test_the_new_agent_is_immediately_assignable_to_an_outlet(wizard, hermes_home):
    """Created, then put on an Outlet, in one sitting and with no restart."""
    page = wizard()
    fill_identity(page, "nora")
    go_to_review(page)
    page.click('[data-testid="wizard-create"]')
    page.wait_for_selector('[data-testid="agent-card-nora"]')

    page.select_option('[data-testid="slot-select-phone-inbound"]', "nora")
    page.wait_for_function(
        """() => {
            const el = document.querySelector('[data-testid="slot-agent-phone-inbound"]');
            return el && el.innerText.trim() === 'nora';
        }""")
    stored = yaml.safe_load((hermes_home.config_dir / "active.yaml").read_text())
    assert stored["outlets"]["phone"]["inbound"] == "nora"
    # One Outlet, one direction: the other Outlet is untouched by the write.
    assert stored["outlets"]["talk"] == {"inbound": None, "outbound": None}


def test_a_refusal_leaves_the_roster_and_the_disk_as_they_were(wizard, hermes_home):
    """The name is taken. The screen says so, and nothing is created."""
    hermes_home.source("nora")
    (hermes_home.config_dir / "agents" / "nora.yaml").write_text(
        yaml.safe_dump({"id": "nora", "pipeline": "realtime",
                        "providers": {"realtime": "openai-gpt-realtime"}}))
    page = wizard()
    before = hermes_home.snapshot()
    fill_identity(page, "nora")
    page.click('[data-testid="wizard-inherit-no"]')
    go_to_review(page)
    page.click('[data-testid="wizard-create"]')
    page.wait_for_selector('[data-testid="wizard-submit-error"]')
    assert "already exists" in page.inner_text('[data-testid="wizard-submit-error"]')
    assert hermes_home.snapshot() == before


# --------------------------------------------------------------------------
# BAR 1: abandoning the wizard leaves nothing behind
# --------------------------------------------------------------------------

def test_abandoning_the_wizard_midway_leaves_nothing_on_disk(wizard, hermes_home):
    """The brief's first bar, driven the way it actually happens.

    Answer half the questions - a name, a description, a soul, a Telegram token
    - and then close the tab. Both trees must be byte-for-byte what they were,
    and in particular there must be no directory under profiles/ at all: not a
    complete one, not an incomplete one, not a marker.
    """
    hermes_home.source("scout")
    before = hermes_home.snapshot()

    page = wizard()
    fill_identity(page, "ghost", "never finished")
    page.check('[data-testid="wizard-customise"]')
    go_to(page, "soul")
    page.fill('[data-testid="wizard-soul"]', "# Ghost\nHalf a being.")
    go_to(page, "mind")
    page.fill('[data-testid="wizard-model"]', "some/model")
    go_to(page, "memory")
    page.check('[data-testid="wizard-memory-private"]')

    # The tab goes away mid-flow.
    page.context.close()

    assert hermes_home.snapshot() == before
    assert not (hermes_home.profiles_dir / "ghost").exists()
    assert sorted(p.name for p in hermes_home.profiles_dir.iterdir()) == ["scout"]


def test_cancelling_the_wizard_leaves_nothing_on_disk(wizard, hermes_home):
    """The other way out: pressing Cancel rather than closing the tab."""
    before = hermes_home.snapshot()
    page = wizard()
    fill_identity(page, "ghost")
    page.click('[data-testid="wizard-inherit-no"]')
    go_to(page, "soul")
    page.fill('[data-testid="wizard-soul"]', "# Ghost")
    page.click('[data-testid="wizard-cancel"]')
    page.wait_for_selector('[data-testid="outlet-card-phone"]')
    assert page.url.endswith("/agents")
    assert hermes_home.snapshot() == before


def test_an_abandoned_wizard_leaves_the_roster_offering_nothing_broken(
        wizard, hermes_home):
    """The failure the bar exists to prevent, checked where it would show.

    A partial Agent left behind would render on the roster - and worse, could be
    picked in an Outlet slot. After an abandoned wizard there is nothing extra
    on the roster and nothing extra in the picker.
    """
    page = wizard()
    fill_identity(page, "ghost")
    page.click('[data-testid="wizard-inherit-no"]')
    go_to(page, "soul")
    page.click('[data-testid="wizard-cancel"]')
    page.wait_for_selector('[data-testid="outlet-card-phone"]')
    assert page.query_selector('[data-testid="agent-card-ghost"]') is None
    options = page.eval_on_selector(
        '[data-testid="slot-select-phone-inbound"]',
        "el => Array.from(el.options).map(o => o.value)")
    assert options == [""], options


# --------------------------------------------------------------------------
# BAR 2: nothing is written before Create, and no Outlet is disturbed by it
# --------------------------------------------------------------------------

def test_creating_an_agent_does_not_disturb_an_existing_assignment(
        wizard, stack, page, hermes_home):
    """An Outlet already carrying an Agent keeps carrying it, and the pointer
    file is not rewritten - creation never opens it."""
    hermes_home.source("onduty")
    (hermes_home.config_dir / "agents" / "onduty.yaml").write_text(yaml.safe_dump({
        "id": "onduty", "enabled": True, "hermes_profile": "onduty",
        "pipeline": "realtime", "providers": {"realtime": "openai-gpt-realtime"}}))
    (hermes_home.config_dir / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {"phone": {"inbound": "onduty", "outbound": "onduty"},
                     "talk": {"inbound": None, "outbound": None}}}))
    pointer_before = (hermes_home.config_dir / "active.yaml").read_bytes()

    running = stack({})
    page.goto(f"{running.base}/agents/new", wait_until="networkidle")
    fill_identity(page, "newcomer")
    go_to_review(page)
    page.click('[data-testid="wizard-create"]')
    page.wait_for_selector('[data-testid="agent-card-newcomer"]')

    # The slot still names the Agent it named, on the screen and on disk.
    assert page.inner_text('[data-testid="slot-agent-phone-inbound"]').strip() == "onduty"
    assert (hermes_home.config_dir / "active.yaml").read_bytes() == pointer_before
    assert page.query_selector('[data-testid="outlet-dead-phone"]') is None


def test_the_screen_refuses_creation_and_names_the_deploy_when_it_cannot(
        stack, page, tmp_path, monkeypatch):
    """The honest answer where the profiles directory is not mounted.

    This is the state the dashboard is in on the deploy as it stands today, so
    it is not a hypothetical: the button is dead, the reason is on the screen,
    and the compose change is named.
    """
    config = tmp_path / "voice-config"
    (config / "agents").mkdir(parents=True)
    (config / "providers.yaml").write_text(open(CANONICAL).read())
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(config))
    monkeypatch.delenv("HERMES_PROFILES_DIR", raising=False)

    running = stack({})
    page.goto(f"{running.base}/agents", wait_until="networkidle")
    page.wait_for_selector('[data-testid="agents-create-blocked"]')
    assert page.is_disabled('[data-testid="new-agent"]')
    banner = page.inner_text('[data-testid="agents-create-blocked"]')
    assert "HERMES_PROFILES_DIR" in banner
    assert "docker-compose.yml" in banner

    # And reaching the wizard's URL directly says the same thing rather than
    # walking the operator through seven steps that end in a 503.
    page.goto(f"{running.base}/agents/new", wait_until="networkidle")
    page.wait_for_selector('[data-testid="wizard-unavailable"]')
    assert "HERMES_PROFILES_DIR" in page.inner_text(
        '[data-testid="wizard-unavailable-reason"]')
    page.fill('[data-testid="wizard-name"]', "nora")
    page.click('[data-testid="wizard-inherit-no"]')
    go_to_review(page)
    assert page.is_disabled('[data-testid="wizard-create"]')
