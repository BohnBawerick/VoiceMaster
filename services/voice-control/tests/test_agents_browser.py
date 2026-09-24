"""Browser-level tests for the Agents screen (ticket 02, on the ticket 16 Outlet axis).

Every acceptance item on ticket 02 is a RENDERING claim, and the first attempt
(PR #5) shipped with no frontend coverage at all. Its two defects were both
invisible to the backend suite and visible the moment a page was opened:

  F1  the two Outlet cards were built from `active.yaml`'s single `inbound` /
      `outbound` keys, which are a call-direction switch shared by BOTH Outlets,
      so the screen could not show two different Agents on the two Outlets while
      claiming exactly that;
  F2  an Agent pinned to an Outlet while disabled or invalid rendered in plain
      white, identical to a healthy one, while the roster below it correctly
      marked the same Agent broken in red -- and that Outlet's calls refuse
      entirely.

So these tests open the page. A real Chromium drives the real built bundle in
``static/assets/`` against a real uvicorn serving the real ``/api/agents`` and
``/api/active`` over a real ``VOICE_CONFIG_DIR`` on disk.

Two habits carried from the Calls browser tests, both learned from tests that
passed while a screen lied:

  - assert the SLOT, not the page. "The word broken appears somewhere" stays
    true while the phone card alone lies.
  - assert the rendered COLOUR, not only the class name. F2 was precisely a
    broken slot painted the same colour as a healthy one; a test that only reads
    a class attribute would have passed against a stylesheet that never used it.

Requires ``playwright`` (requirements-dev.txt) plus its Chromium -- see
``tests/test_calls_browser.py``.
"""
import copy
import json

import pytest
import yaml

pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed (requirements-dev.txt)"
)

# The rendered colours the screen's claims are made of. Both come from
# ``ui/src/index.css`` (--danger-text, --text); asserting them is what binds
# "shown as broken" to something an operator can actually see.
BROKEN_RED = "rgb(255, 143, 154)"
HEALTHY_WHITE = "rgb(241, 241, 243)"

HEALTHY = {
    "id": "placeholder",
    "description": "a worked agent",
    "enabled": True,
    "hermes_profile": "default",
    "pipeline": "realtime",
    "providers": {"realtime": "openai-gpt-realtime"},
    "knobs": {"voice": "marin", "model": "gpt-realtime-probe"},
}


def agent_doc(agent_id, **overrides):
    doc = copy.deepcopy(HEALTHY)
    doc["id"] = agent_id
    doc.update(overrides)
    return doc


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A scratch VOICE_CONFIG_DIR the dashboard process reads per request.

    ``profiles.config_dir()`` resolves the env var on every call and the uvicorn
    the ``stack`` fixture starts lives in this process, so setting it here is
    enough -- no restart, and no second copy of the configuration.
    """
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "agents").mkdir()

    class Config:
        root = tmp_path

        def agent(self, agent_id, **overrides):
            (tmp_path / "agents" / f"{agent_id}.yaml").write_text(
                yaml.safe_dump(agent_doc(agent_id, **overrides), sort_keys=False))
            return agent_id

        def raw_agent(self, agent_id, text):
            (tmp_path / "agents" / f"{agent_id}.yaml").write_text(text)
            return agent_id

        def active(self, **outlets):
            (tmp_path / "active.yaml").write_text(
                yaml.safe_dump({"outlets": outlets}, sort_keys=False))

        def stored(self):
            return yaml.safe_load((tmp_path / "active.yaml").read_text())

    return Config()


@pytest.fixture
def agents_page(stack, page, config):
    """The Agents screen, open, against a store with no call history."""

    def _open():
        running = stack({})
        page.goto(f"{running.base}/agents", wait_until="networkidle")
        page.wait_for_selector('[data-testid="outlet-card-phone"]')
        return page

    return _open


# --------------------------------------------------------------------------
# helpers -- every one of these addresses ONE slot, never the page
# --------------------------------------------------------------------------

def slot_agent_text(page, outlet, direction) -> str:
    return page.inner_text(f'[data-testid="slot-agent-{outlet}-{direction}"]').strip()


def slot_agent_colour(page, outlet, direction) -> str:
    return page.eval_on_selector(
        f'[data-testid="slot-agent-{outlet}-{direction}"]',
        "el => getComputedStyle(el).color")


def slot_warning(page, outlet, direction):
    node = page.query_selector(f'[data-testid="slot-warning-{outlet}-{direction}"]')
    return node.inner_text().strip() if node and node.is_visible() else None


def card_is_broken(page, outlet) -> bool:
    classes = page.get_attribute(f'[data-testid="outlet-card-{outlet}"]', "class") or ""
    return "outlet-card-broken" in classes


def slot_is_broken(page, outlet, direction) -> bool:
    classes = page.get_attribute(f'[data-testid="slot-{outlet}-{direction}"]', "class") or ""
    return "outlet-slot-broken" in classes


def select_options(page, outlet, direction) -> list:
    return page.eval_on_selector(
        f'[data-testid="slot-select-{outlet}-{direction}"]',
        "el => Array.from(el.options).map(o => o.value)")


def assert_slot_reads_broken(page, outlet, direction, *, naming):
    """The F2 bar, applied to one slot: the operator can SEE this Outlet is dead.

    Class, colour, the field path, and the flag -- all four, because each one
    alone has a way of passing while the screen still looks healthy.
    """
    assert slot_is_broken(page, outlet, direction), \
        f"{outlet}.{direction} carries no broken state"
    assert card_is_broken(page, outlet), f"the {outlet} card is not marked broken"
    assert page.is_visible(f'[data-testid="outlet-dead-{outlet}"]')
    assert page.is_visible(f'[data-testid="slot-flag-{outlet}-{direction}"]')

    warning = slot_warning(page, outlet, direction)
    assert warning is not None, f"{outlet}.{direction} shows no reason"
    assert f"outlets.{outlet}.{direction}" in warning, warning
    for fragment in naming:
        assert fragment in warning, f"{fragment!r} not in {warning!r}"

    assert slot_agent_colour(page, outlet, direction) == BROKEN_RED, \
        "the named Agent is not rendered in the broken colour -- this is F2 exactly"


def assert_slot_reads_healthy(page, outlet, direction, agent_id):
    assert not slot_is_broken(page, outlet, direction)
    assert slot_warning(page, outlet, direction) is None
    assert slot_agent_text(page, outlet, direction) == agent_id
    assert slot_agent_colour(page, outlet, direction) == HEALTHY_WHITE


# --------------------------------------------------------------------------
# F1: the Outlet axis is real on screen
# --------------------------------------------------------------------------

def test_two_different_agents_on_the_two_outlets_render_as_two_different_agents(
        config, agents_page):
    """The claim the screen exists to make, made true.

    Under the pre-16 model this configuration could not exist; under the flat
    `inbound`/`outbound` read PR #5 shipped, both cards would name the same
    Agent whatever the file said.
    """
    config.agent("phone-agent", description="Answers the Twilio line.")
    config.agent("talk-agent", description="Answers Nextcloud Talk.")
    config.active(phone={"inbound": "phone-agent", "outbound": "phone-agent"},
                  talk={"inbound": "talk-agent", "outbound": "talk-agent"})
    page = agents_page()

    assert_slot_reads_healthy(page, "phone", "inbound", "phone-agent")
    assert_slot_reads_healthy(page, "phone", "outbound", "phone-agent")
    assert_slot_reads_healthy(page, "talk", "inbound", "talk-agent")
    assert_slot_reads_healthy(page, "talk", "outbound", "talk-agent")


def test_the_two_outlets_can_diverge_by_direction_too(config, agents_page):
    """Outlet and direction are independent axes: four slots, four answers."""
    for name in ("a-inbound", "a-outbound", "b-inbound", "b-outbound"):
        config.agent(name)
    config.active(phone={"inbound": "a-inbound", "outbound": "a-outbound"},
                  talk={"inbound": "b-inbound", "outbound": "b-outbound"})
    page = agents_page()

    assert slot_agent_text(page, "phone", "inbound") == "a-inbound"
    assert slot_agent_text(page, "phone", "outbound") == "a-outbound"
    assert slot_agent_text(page, "talk", "inbound") == "b-inbound"
    assert slot_agent_text(page, "talk", "outbound") == "b-outbound"


def test_a_healthy_configuration_renders_clean(config, agents_page):
    """The negative control. Without it every assertion above could be satisfied
    by a screen that shouts about everything."""
    config.agent("house-agent", description="The one agent.")
    config.active(phone={"inbound": "house-agent", "outbound": "house-agent"},
                  talk={"inbound": "house-agent", "outbound": "house-agent"})
    page = agents_page()

    for outlet in ("phone", "talk"):
        assert not card_is_broken(page, outlet)
        for direction in ("inbound", "outbound"):
            assert_slot_reads_healthy(page, outlet, direction, "house-agent")
    assert page.query_selector_all('[data-testid="active-page-warning"]') == []
    assert page.query_selector_all('[data-testid="voice-agent-env-banner"]') == []
    assert "Available" in page.inner_text('[data-testid="agent-status-house-agent"]')


# --------------------------------------------------------------------------
# F2: broken and disabled Agents are shown as broken, on the card they break
# --------------------------------------------------------------------------

def test_a_disabled_agent_pinned_to_an_outlet_is_shown_as_broken(config, agents_page):
    config.agent("paused-agent", enabled=False)
    config.agent("talk-agent")
    config.active(phone={"inbound": "paused-agent", "outbound": None},
                  talk={"inbound": "talk-agent", "outbound": "talk-agent"})
    page = agents_page()

    assert slot_agent_text(page, "phone", "inbound") == "paused-agent"
    assert_slot_reads_broken(page, "phone", "inbound", naming=["enabled: false"])

    # ...and the healthy Outlet next to it is untouched, because ticket 16 fails
    # loud per Outlet and the screen must say so.
    assert not card_is_broken(page, "talk")
    assert_slot_reads_healthy(page, "talk", "inbound", "talk-agent")

    # The roster agreed with reality even in PR #5 for INVALID agents, but told
    # a disabled one it was "Available".
    status = page.inner_text('[data-testid="agent-status-paused-agent"]')
    assert "Available" not in status
    assert "Disabled" in status
    classes = page.get_attribute('[data-testid="agent-card-paused-agent"]', "class")
    assert "agent-card-broken" in classes


def test_an_invalid_agent_pinned_to_an_outlet_is_shown_as_broken(config, agents_page):
    config.agent("bad-agent", providers={"realtime": "no-such-provider"})
    config.agent("phone-agent")
    config.active(phone={"inbound": "phone-agent", "outbound": "phone-agent"},
                  talk={"inbound": None, "outbound": "bad-agent"})
    page = agents_page()

    assert slot_agent_text(page, "talk", "outbound") == "bad-agent"
    assert_slot_reads_broken(page, "talk", "outbound", naming=["invalid"])

    assert not card_is_broken(page, "phone")
    assert_slot_reads_healthy(page, "phone", "inbound", "phone-agent")

    status = page.inner_text('[data-testid="agent-status-bad-agent"]')
    assert "Available" not in status
    assert "broken" in status.lower()


def test_a_missing_agent_pinned_to_an_outlet_is_shown_as_broken(config, agents_page):
    """The slot names an Agent whose file is not there -- deleted out of band,
    or never created. There is no roster card to fall back on here: if the
    Outlet card does not say it, nothing on the screen does."""
    config.agent("talk-agent")
    config.active(phone={"inbound": "ghost-agent", "outbound": None},
                  talk={"inbound": "talk-agent", "outbound": "talk-agent"})
    page = agents_page()

    assert slot_agent_text(page, "phone", "inbound") == "ghost-agent"
    assert_slot_reads_broken(page, "phone", "inbound", naming=["does not exist"])
    assert page.query_selector('[data-testid="agent-card-ghost-agent"]') is None
    assert not card_is_broken(page, "talk")


def test_an_agent_edited_into_an_activation_the_direction_refuses_is_shown_as_broken(
        config, agents_page):
    """Cascade is outbound-only (s7). An agent that was healthy when assigned and
    was later switched to the cascade pipeline kills that inbound slot, and the
    dashboard health check is the only place that shows it."""
    config.agent("cascade-agent", pipeline="cascade",
                 providers={"stt": "deepgram", "llm": "gpt-4.1", "tts": "cartesia"},
                 knobs={})
    config.active(phone={"inbound": "cascade-agent", "outbound": None},
                  talk={"inbound": None, "outbound": None})
    page = agents_page()

    assert_slot_reads_broken(page, "phone", "inbound", naming=["cascade"])


def test_a_broken_slot_never_looks_like_a_healthy_one(config, agents_page):
    """The finding stated as a property: on ONE screen holding both, the broken
    slot and the healthy slot must not render identically. PR #5's screen failed
    exactly this -- same element, same colour, same weight, different truth."""
    config.agent("paused-agent", enabled=False)
    config.agent("good-agent")
    config.active(phone={"inbound": "paused-agent", "outbound": "good-agent"},
                  talk={"inbound": None, "outbound": None})
    page = agents_page()

    broken = slot_agent_colour(page, "phone", "inbound")
    healthy = slot_agent_colour(page, "phone", "outbound")
    assert broken != healthy, "a dead slot renders in the same colour as a live one"
    assert broken == BROKEN_RED and healthy == HEALTHY_WHITE


def test_a_broken_agent_is_not_offered_for_reassignment(config, agents_page):
    """It is stored, so it is shown; it cannot serve a call, so it is not on the
    menu. The current (broken) value stays selectable only as itself, labelled."""
    config.agent("paused-agent", enabled=False)
    config.agent("good-agent")
    config.active(phone={"inbound": "paused-agent", "outbound": None},
                  talk={"inbound": None, "outbound": None})
    page = agents_page()

    assert select_options(page, "phone", "inbound") == ["", "paused-agent", "good-agent"]
    # Nowhere it is NOT already stored is a broken agent offered.
    assert select_options(page, "talk", "inbound") == ["", "good-agent"]


# --------------------------------------------------------------------------
# "No Agent selected" is a named state, never a blank
# --------------------------------------------------------------------------

def test_an_empty_slot_is_unassigned_not_a_fake_agent(config, agents_page):
    config.agent("good-agent")
    config.active(phone={"inbound": None, "outbound": None},
                  talk={"inbound": None, "outbound": None})
    page = agents_page()

    for outlet in ("phone", "talk"):
        for direction in ("inbound", "outbound"):
            assert slot_agent_text(page, outlet, direction) == "No Agent assigned"
            assert not slot_is_broken(page, outlet, direction)
    assert page.locator('[data-testid="agent-card-default"]').count() == 0


def test_a_flat_active_file_is_reported_rather_than_routed(config, agents_page,
                                                           tmp_path):
    """Ticket 17, seen from the page. A file carrying the pre-16 flat keys used to
    render as the same Agent on all four slots. Those keys name no Outlet, so the
    loader refuses them - and the screen has to SAY so. Four calm empty slots over
    a file the bridges will not read is the F2 failure wearing a new hat."""
    config.agent("flat-agent")
    (tmp_path / "active.yaml").write_text(
        yaml.safe_dump({"inbound": "flat-agent", "outbound": "flat-agent"}))
    page = agents_page()

    banners = page.query_selector_all('[data-testid="active-page-warning"]')
    assert banners, "a refused active.yaml rendered no page-level warning"
    assert "outlets" in banners[0].inner_text()
    for outlet in ("phone", "talk"):
        for direction in ("inbound", "outbound"):
            assert slot_agent_text(page, outlet, direction) != "flat-agent"


# --------------------------------------------------------------------------
# The write path: one click changes ONE slot
# --------------------------------------------------------------------------

def test_assigning_one_outlet_leaves_the_other_outlet_alone(config, agents_page):
    """The hazard this screen exists to close.

    The deleted legacy screen's buttons sent the flat `{"inbound": id}` key,
    which means BOTH Outlets: one click there silently reassigned the Outlet the
    operator was not looking at and undid a per-Outlet split, with a clean 200
    and no warning anywhere. This screen must be structurally incapable of it.
    """
    config.agent("phone-agent")
    config.agent("talk-agent")
    config.agent("new-agent")
    config.active(phone={"inbound": "phone-agent", "outbound": "phone-agent"},
                  talk={"inbound": "talk-agent", "outbound": "talk-agent"})
    page = agents_page()

    sent = []
    page.on("request", lambda request: sent.append(request)
            if request.method == "PUT" and "/api/active" in request.url else None)

    page.select_option('[data-testid="slot-select-phone-inbound"]', "new-agent")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=\"slot-agent-phone-inbound\"]')"
        ".innerText.trim() === 'new-agent'")

    # 1. the request named one Outlet and one direction, and nothing else
    assert len(sent) == 1
    body = json.loads(sent[0].post_data)
    assert body == {"outlets": {"phone": {"inbound": "new-agent"}}}, body
    assert "inbound" not in body and "outbound" not in body, \
        "a legacy flat key would have written BOTH Outlets"

    # 2. what landed on disk: exactly one slot moved
    stored = config.stored()
    assert stored["outlets"]["phone"]["inbound"] == "new-agent"
    assert stored["outlets"]["phone"]["outbound"] == "phone-agent"
    assert stored["outlets"]["talk"] == {"inbound": "talk-agent", "outbound": "talk-agent"}

    # 3. and the screen still shows the Outlet it did not touch, unchanged
    assert slot_agent_text(page, "talk", "inbound") == "talk-agent"
    assert slot_agent_text(page, "talk", "outbound") == "talk-agent"
    assert slot_agent_text(page, "phone", "outbound") == "phone-agent"


def test_clearing_a_slot_writes_null_for_that_slot_only(config, agents_page):
    config.agent("phone-agent")
    config.agent("talk-agent")
    config.active(phone={"inbound": "phone-agent", "outbound": "phone-agent"},
                  talk={"inbound": "talk-agent", "outbound": "talk-agent"})
    page = agents_page()

    page.select_option('[data-testid="slot-select-phone-inbound"]', "")
    page.wait_for_function(
        "() => document.querySelector('[data-testid=\"slot-agent-phone-inbound\"]')"
        ".innerText.trim() === 'No Agent assigned'")

    stored = config.stored()
    assert stored["outlets"]["phone"] == {"inbound": None, "outbound": "phone-agent"}
    assert stored["outlets"]["talk"] == {"inbound": "talk-agent", "outbound": "talk-agent"}


def test_a_refused_assignment_says_why_on_the_slot_it_was_refused_for(
        config, agents_page):
    """The API refuses what the call path would refuse. When it does, the reason
    lands on the slot, not in a console nobody is reading."""
    config.agent("good-agent")
    config.active(phone={"inbound": None, "outbound": None},
                  talk={"inbound": None, "outbound": None})
    page = agents_page()

    # Disable the agent out of band, AFTER the page listed it as selectable --
    # the same race an operator hits with two tabs open.
    config.agent("good-agent", enabled=False)
    page.select_option('[data-testid="slot-select-phone-inbound"]', "good-agent")
    page.wait_for_selector('[data-testid="slot-error-phone-inbound"]')

    error = page.inner_text('[data-testid="slot-error-phone-inbound"]')
    assert "enabled: false" in error
    assert slot_agent_text(page, "phone", "inbound") == "No Agent assigned"
    assert config.stored()["outlets"]["phone"]["inbound"] is None


# --------------------------------------------------------------------------
# Honesty about what the screen cannot see, and what overrides it
# --------------------------------------------------------------------------

def test_an_unreadable_active_file_is_reported_on_the_page(config, agents_page):
    """No slot owns this fault, so nothing would paint it on a card. It must not
    vanish into the gap between the two: a screen that renders four calm Outlets
    over an `active.yaml` it could not read is the worst version of F2."""
    (config.root / "active.yaml").write_text("outlets: [this is not a map\n")
    page = agents_page()

    banners = page.query_selector_all('[data-testid="active-page-warning"]')
    assert banners and banners[0].is_visible()
    assert "active.yaml" in banners[0].inner_text()
    for outlet in ("phone", "talk"):
        assert not card_is_broken(page, outlet)


def test_voice_agent_env_is_declared_as_overriding_every_outlet(
        config, agents_page, monkeypatch):
    """`VOICE_AGENT` wins over the pointer on EVERY Outlet and both directions
    (profiles.load_effective_profile). While it is set, everything the cards
    below show is inert, and the screen has to say so rather than presenting
    stored intent as what answers."""
    config.agent("stored-agent")
    config.active(phone={"inbound": "stored-agent", "outbound": None},
                  talk={"inbound": None, "outbound": None})
    monkeypatch.setenv("VOICE_AGENT", "env-agent")
    page = agents_page()

    banner = page.query_selector('[data-testid="voice-agent-env-banner"]')
    assert banner is not None and banner.is_visible()
    text = banner.inner_text()
    assert "env-agent" in text
    assert "inert" in text
