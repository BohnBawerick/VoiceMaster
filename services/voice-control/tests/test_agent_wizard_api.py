"""The Agent creation wizard's API (ticket 13).

The ticket's first acceptance line is the one worth the most care: the wizard
creates a **real Hermes profile**, not a config file in this app. So these tests
assert against ticket 12's on-disk contract rather than against a shape this
service invented - the same predicates ``hermes_profile_registry._inspect``
applies every scan interval in the OTHER repo:

    a directory under profiles/ is startable ONLY when it has a config.yaml and
    carries no `.incomplete` marker; a reserved name, a bad name or a missing
    config is reported and never started.

``hermes_profiles.profile_is_startable`` is that predicate in one line, and every
abandonment test below is written against it. A test asserting instead that "the
directory is gone" would pass against a half-written profile that had merely been
tidied, and would say nothing about whether the phone line could reach it.

Two bars from the brief have their own sections at the bottom:

  * a half-completed or abandoned creation leaves nothing startable behind;
  * creating an Agent disturbs no Outlet and needs no redeploy.
"""
import os
import stat

import httpx
import pytest
import yaml

import app as app_module
import hermes_profiles
from voicecore import profiles

CANONICAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "..", "voice-config", "providers.yaml")


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """A scratch VOICE_CONFIG_DIR plus a scratch Hermes profiles directory.

    The profiles directory is a SEPARATE tree from the voice config on purpose:
    on the real deploy they are two different mounts, and a test that let them
    be one directory would not notice code that wrote a "profile" into the
    config dir instead - the exact VoiceMaster-local imitation the ticket
    forbids.
    """
    config = tmp_path / "voice-config"
    (config / "agents").mkdir(parents=True)
    (config / "providers.yaml").write_text(
        (open(CANONICAL).read()))
    home = tmp_path / "hermes-profiles"
    home.mkdir()
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(config))
    monkeypatch.setenv("HERMES_PROFILES_DIR", str(home))

    class Home:
        root = tmp_path
        config_dir = config
        profiles_dir = home

        def source(self, name="scout", **config_overrides):
            path = home / name
            path.mkdir()
            doc = {
                "identity": {"name": name.capitalize()},
                "model": {"default": "minimax/minimax-m2.7", "provider": "openrouter"},
                "tools": {"profile": "coding"},
                "mcp_servers": {"hindsight": {"url": "http://hindsight:8888/mcp/"}},
                "telegram": {"enabled": True, "dm_policy": "pairing"},
                "api_server": {"port": 18789},
            }
            doc.update(config_overrides)
            (path / "config.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
            (path / "SOUL.md").write_text(f"# {name}\nThe source being.\n")
            (path / "skills").mkdir()
            (path / "skills" / "a-skill.md").write_text("skill body\n")
            return name

        def agent_doc(self, name):
            return yaml.safe_load((config / "agents" / f"{name}.yaml").read_text())

        def profile_config(self, name):
            return yaml.safe_load((home / name / "config.yaml").read_text())

    return Home()


@pytest.fixture
def client(make_client):
    from conftest import SentinelTransport

    # Creating an Agent is entirely local: it writes two files. A network call
    # from this path would be a defect, and the sentinel makes it a failure.
    return make_client(SentinelTransport())


def payload(**overrides):
    body = {"name": "nora", "description": "A new being."}
    body.update(overrides)
    return body


# --------------------------------------------------------------------------
# It creates a real Hermes profile, in the real place
# --------------------------------------------------------------------------

async def test_creates_a_real_hermes_profile_directory(client, hermes_home):
    async with client as c:
        res = await c.post("/api/agents/create", json=payload())
    assert res.status_code == 201, res.text

    path = hermes_home.profiles_dir / "nora"
    # The directory the Hermes supervisor scans, with the files a profile is
    # made of (ADR 0001) - not a document in this app's config dir.
    assert path.is_dir()
    assert (path / "config.yaml").is_file()
    assert (path / "SOUL.md").is_file()
    for sub in ("sessions", "skills", "memory", "cron"):
        assert (path / sub).is_dir(), sub
    assert hermes_profiles.profile_is_startable(path)


async def test_the_profile_is_not_written_into_the_voice_config_dir(client, hermes_home):
    """The imitation this ticket forbids would look exactly like success here."""
    async with client as c:
        res = await c.post("/api/agents/create", json=payload())
    assert res.status_code == 201
    assert not (hermes_home.config_dir / "nora").exists()
    assert not (hermes_home.config_dir / "profiles").exists()


async def test_the_agent_names_the_profile_it_created(client, hermes_home):
    async with client as c:
        res = await c.post("/api/agents/create", json=payload())
    doc = hermes_home.agent_doc("nora")
    assert doc["hermes_profile"] == "nora"
    assert doc["id"] == "nora"
    assert res.json()["agent"] == doc


async def test_the_new_agent_is_immediately_on_the_roster(client, hermes_home):
    async with client as c:
        await c.post("/api/agents/create", json=payload())
        rows = (await c.get("/api/agents")).json()
    row = next(r for r in rows if r["id"] == "nora")
    # Valid and enabled means the Agents screen offers it in every Outlet
    # picker: created, then immediately assignable, with no restart in between.
    assert row["valid"] is True and row["enabled"] is True
    assert row["hermes_profile"] == "nora"


async def test_the_marker_name_is_ticket_12s(client, hermes_home):
    """A constant that has to match the other repo, asserted as a constant.

    ``hermes_profile_registry.INCOMPLETE_MARKER`` is what makes a half-written
    profile un-startable. If this drifts, the staging below still writes *a*
    file and every behavioural test still passes while the Hermes side sees a
    complete profile mid-write.
    """
    assert hermes_profiles.INCOMPLETE_MARKER == ".incomplete"
    assert hermes_profiles.CONFIG_NAME == "config.yaml"
    assert hermes_profiles.REGISTRY_BASENAME == os.path.join("gateways",
                                                             "gateways.json")


# --------------------------------------------------------------------------
# Where it cannot create one, it says so instead of pretending
# --------------------------------------------------------------------------

async def test_creation_refuses_when_the_profiles_dir_is_not_configured(
        client, hermes_home, monkeypatch):
    monkeypatch.delenv("HERMES_PROFILES_DIR")
    async with client as c:
        res = await c.post("/api/agents/create", json=payload())
        state = (await c.get("/api/hermes")).json()
    assert res.status_code == 503
    assert "HERMES_PROFILES_DIR" in res.json()["detail"][0]
    # And the refusal names the deploy change, in the response the screen reads.
    assert state["available"] is False
    assert "docker-compose.yml" in state["deploy_hint"]
    assert not (hermes_home.config_dir / "agents" / "nora.yaml").exists()


async def test_creation_refuses_when_the_mount_is_read_only(
        client, hermes_home, monkeypatch):
    """A read-only mount is the likeliest way this half-lands on the NAS."""
    if os.geteuid() == 0:
        pytest.skip("running as root: mode bits do not deny access")
    os.chmod(hermes_home.profiles_dir, stat.S_IRUSR | stat.S_IXUSR)
    try:
        async with client as c:
            res = await c.post("/api/agents/create", json=payload())
        assert res.status_code == 503
        assert "not writable" in res.json()["detail"][0]
    finally:
        os.chmod(hermes_home.profiles_dir, 0o755)


async def test_it_never_falls_back_to_a_directory_of_its_own(hermes_home, monkeypatch):
    """No default path, deliberately: a default would create a profile inside
    the dashboard's own container that no supervisor ever scans."""
    monkeypatch.delenv("HERMES_PROFILES_DIR")
    assert hermes_profiles.profiles_dir() is None
    with pytest.raises(hermes_profiles.ProfileCreateError):
        hermes_profiles.require_dir()


# --------------------------------------------------------------------------
# Names: legal in BOTH worlds, refused loudly, never sanitized
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name, why", [
    ("default", "the container's own profile"),
    ("Nora", "uppercase is illegal as a voicecore agent id"),
    ("no.dots", "a dot is illegal in a Hermes profile directory name"),
    ("../escape", "traversal"),
    ("", "empty"),
    ("-leading", "must start alphanumeric"),
])
async def test_illegal_names_are_refused_and_nothing_is_created(
        client, hermes_home, name, why):
    async with client as c:
        res = await c.post("/api/agents/create", json=payload(name=name))
    assert res.status_code in (409, 422), why
    assert sorted(p.name for p in hermes_home.profiles_dir.iterdir()) == [], why
    assert sorted(p.name for p in
                  (hermes_home.config_dir / "agents").iterdir()) == [], why


async def test_an_existing_profile_directory_is_never_written_into(client, hermes_home):
    hermes_home.source("nora")
    before = (hermes_home.profiles_dir / "nora" / "SOUL.md").read_text()
    async with client as c:
        res = await c.post("/api/agents/create", json=payload(name="nora"))
    assert res.status_code == 409
    assert (hermes_home.profiles_dir / "nora" / "SOUL.md").read_text() == before
    assert not (hermes_home.config_dir / "agents" / "nora.yaml").exists()


async def test_an_existing_agent_id_is_refused_before_the_profile_is_touched(
        client, hermes_home):
    (hermes_home.config_dir / "agents" / "nora.yaml").write_text("id: nora\n")
    async with client as c:
        res = await c.post("/api/agents/create", json=payload(name="nora"))
    assert res.status_code == 409
    assert not (hermes_home.profiles_dir / "nora").exists()


# --------------------------------------------------------------------------
# Inheritance: the quick path, and it takes something real
# --------------------------------------------------------------------------

async def test_inheritance_takes_the_sources_model_tools_and_soul(client, hermes_home):
    hermes_home.source("scout")
    async with client as c:
        res = await c.post("/api/agents/create",
                           json=payload(inherit_from="scout"))
    assert res.status_code == 201
    config = hermes_home.profile_config("nora")
    assert config["model"]["default"] == "minimax/minimax-m2.7"
    assert config["tools"]["profile"] == "coding"
    assert (hermes_home.profiles_dir / "nora" / "SOUL.md").read_text() == (
        "# scout\nThe source being.\n")


async def test_inheritance_does_not_take_the_sources_name_or_its_api_port(
        client, hermes_home):
    """Two beings introducing themselves as the same person, and two gateways
    racing for 18789. Neither is "everything you want to take"."""
    hermes_home.source("scout")
    async with client as c:
        await c.post("/api/agents/create", json=payload(inherit_from="scout"))
    config = hermes_home.profile_config("nora")
    assert config["identity"]["name"] == "nora"
    assert "api_server" not in config


async def test_inheritance_leaves_the_source_profile_untouched(client, hermes_home):
    hermes_home.source("scout")
    before = {p.name: p.read_bytes() for p in
              (hermes_home.profiles_dir / "scout").iterdir() if p.is_file()}
    async with client as c:
        await c.post("/api/agents/create", json=payload(inherit_from="scout"))
    after = {p.name: p.read_bytes() for p in
             (hermes_home.profiles_dir / "scout").iterdir() if p.is_file()}
    assert after == before


async def test_skills_are_copied_only_when_asked(client, hermes_home):
    hermes_home.source("scout")
    async with client as c:
        await c.post("/api/agents/create",
                     json=payload(inherit_from="scout"))
        await c.post("/api/agents/create",
                     json=payload(name="ada", inherit_from="scout",
                                  inherit_skills=True))
    assert list((hermes_home.profiles_dir / "nora" / "skills").iterdir()) == []
    assert [p.name for p in (hermes_home.profiles_dir / "ada" / "skills").iterdir()] \
        == ["a-skill.md"]


async def test_inheriting_from_a_profile_that_does_not_exist_is_refused(
        client, hermes_home):
    async with client as c:
        res = await c.post("/api/agents/create",
                           json=payload(inherit_from="ghost"))
    assert res.status_code == 422
    assert "ghost" in res.json()["detail"][0]
    assert not (hermes_home.profiles_dir / "nora").exists()


async def test_the_inheritance_preview_never_reads_a_dotenv(client, hermes_home):
    hermes_home.source("scout")
    (hermes_home.profiles_dir / "scout" / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=sourcetoken-must-never-appear\n")
    async with client as c:
        res = await c.get("/api/hermes/profiles/scout")
    assert res.status_code == 200
    assert "sourcetoken-must-never-appear" not in res.text
    assert res.json()["model"] == "minimax/minimax-m2.7"
    assert res.json()["soul"].startswith("# scout")


# --------------------------------------------------------------------------
# Declining inheritance walks the substantive choices
# --------------------------------------------------------------------------

async def test_the_full_path_writes_soul_model_tools_and_memory(client, hermes_home):
    async with client as c:
        res = await c.post("/api/agents/create", json=payload(
            identity_name="Nora",
            soul="# Nora\nShe is patient and exact.",
            model="anthropic/claude-haiku-4.5", model_provider="openrouter",
            tools_profile="general", memory="private"))
    assert res.status_code == 201
    path = hermes_home.profiles_dir / "nora"
    assert (path / "SOUL.md").read_text() == "# Nora\nShe is patient and exact.\n"
    config = hermes_home.profile_config("nora")
    assert config["identity"]["name"] == "Nora"
    assert config["model"] == {"default": "anthropic/claude-haiku-4.5",
                               "provider": "openrouter"}
    assert config["tools"]["profile"] == "general"


async def test_memory_shared_keeps_the_hindsight_server_private_drops_it(
        client, hermes_home):
    """"Where its memory lives" is a real difference in config.yaml, not a label.

    Shared keeps the inherited Hindsight MCP server, so the new being can consult
    the same banks; private drops it, so it remembers only in its own profile.
    """
    hermes_home.source("scout")
    async with client as c:
        await c.post("/api/agents/create", json=payload(
            name="shared-one", inherit_from="scout", memory="shared"))
        await c.post("/api/agents/create", json=payload(
            name="private-one", inherit_from="scout", memory="private"))
    assert "hindsight" in hermes_home.profile_config("shared-one")["mcp_servers"]
    assert "mcp_servers" not in hermes_home.profile_config("private-one")


async def test_an_unknown_memory_mode_is_refused(client, hermes_home):
    async with client as c:
        res = await c.post("/api/agents/create", json=payload(memory="elsewhere"))
    assert res.status_code == 422
    assert not (hermes_home.profiles_dir / "nora").exists()


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

async def test_telegram_token_is_written_0600_and_never_returned(client, hermes_home):
    # Well-formed on purpose: the leak check below is about a token that WAS
    # accepted and written, which is the only kind that could leak.
    token = "1234567:AAtelegramtokenthatmustnotleak00000"
    assert hermes_profiles.telegram_token_problem(token) is None
    async with client as c:
        res = await c.post("/api/agents/create", json=payload(
            telegram_connect=True, telegram_bot_token=token))
        state = await c.get("/api/hermes")
        agents = await c.get("/api/agents")
        agent = await c.get("/api/agents/nora")
    env_file = hermes_home.profiles_dir / "nora" / ".env"
    assert env_file.read_text() == f"TELEGRAM_BOT_TOKEN={token}\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    # It went in. It never comes back out - not in the creation response, not in
    # any listing, and not in the agent document (which validate_profile would
    # reject for carrying a credential anyway).
    for response in (res, state, agents, agent):
        assert token not in response.text
    assert res.json()["profile"]["telegram_connected"] is True


VALID_TOKEN = "8209633400:" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456_-"[:35]

MALFORMED_TOKENS = [
    ("not-a-real-telegram-token-at-all !! <script>", "no colon, and not the shape"),
    ("8209633400:tooshort", "the auth half is not 35 characters"),
    ("8209633400:" + "A" * 34, "one character short - the boundary"),
    ("8209633400:" + "A" * 36, "one character long - the other boundary"),
    ("notdigits:" + "A" * 35, "the bot id is not a run of digits"),
    ("8209633400" + "A" * 35, "no colon at all"),
    ("8209633400:" + "A" * 34 + "!", "an illegal character in the auth half"),
]


@pytest.mark.parametrize("token, why", MALFORMED_TOKENS)
async def test_a_malformed_telegram_token_is_refused_and_creates_nothing(
        client, hermes_home, token, why):
    """The accepted criterion was empty OR malformed, and only empty was checked.

    A malformed token was written verbatim and the profile created: not a hole
    (the file is 0600 and correctly owned) but a failure that surfaces far from
    its cause - the operator finds out when the bot never comes online, not when
    they typed it.
    """
    async with client as c:
        res = await c.post("/api/agents/create", json=payload(
            telegram_connect=True, telegram_bot_token=token))
    assert res.status_code == 422, why
    assert "not the shape of a Telegram bot token" in res.json()["detail"][0], why
    # Refused BEFORE anything is created, like every other refusal on this path:
    # no directory, and above all no .env holding the bad value.
    assert not (hermes_home.profiles_dir / "nora").exists(), why
    assert list(hermes_home.profiles_dir.iterdir()) == [], why
    assert not (hermes_home.config_dir / "agents" / "nora.yaml").exists(), why


@pytest.mark.parametrize("token, _why", MALFORMED_TOKENS[:3])
async def test_the_refusal_never_repeats_the_token_it_refused(
        client, hermes_home, token, _why):
    """The refusal reaches an HTTP response and a log. Echoing the value back to
    say what was wrong with it would put a credential in both."""
    async with client as c:
        res = await c.post("/api/agents/create", json=payload(
            telegram_connect=True, telegram_bot_token=token))
    assert res.status_code == 422
    assert token not in res.text
    # The shape IS named, so the refusal is actionable without the value.
    assert "<bot_id>:<auth_token>" in res.json()["detail"][0]


async def test_a_well_formed_token_is_accepted_and_whitespace_is_trimmed(
        client, hermes_home):
    """The other side of the check: a real-shaped token still works, and padding
    around it is trimmed rather than written into the .env verbatim."""
    async with client as c:
        res = await c.post("/api/agents/create", json=payload(
            telegram_connect=True, telegram_bot_token=f"  {VALID_TOKEN}\n"))
    assert res.status_code == 201, res.text
    env_file = hermes_home.profiles_dir / "nora" / ".env"
    assert env_file.read_text() == f"TELEGRAM_BOT_TOKEN={VALID_TOKEN}\n"


def test_the_token_shape_is_checked_without_touching_the_network():
    """Structural, never a call to Telegram: creating an Agent must not be able
    to fail because a third party is down, and the create path makes no network
    call at all (asserted separately by the sentinel transport)."""
    assert hermes_profiles.telegram_token_problem(VALID_TOKEN) is None
    assert hermes_profiles.TELEGRAM_TOKEN_RE.pattern == r"^\d+:[A-Za-z0-9_-]{35}$"


async def test_telegram_enabled_without_a_token_is_refused(client, hermes_home):
    """An inherited `telegram.enabled: true` with no bot of its own polls with
    nothing, or shares the source Agent's bot."""
    async with client as c:
        res = await c.post("/api/agents/create", json=payload(telegram_connect=True))
    assert res.status_code == 422
    assert "@BotFather" in res.json()["detail"][0]
    assert not (hermes_home.profiles_dir / "nora").exists()


async def test_declining_telegram_disables_an_inherited_one(client, hermes_home):
    hermes_home.source("scout")
    async with client as c:
        await c.post("/api/agents/create", json=payload(inherit_from="scout"))
    assert hermes_home.profile_config("nora")["telegram"]["enabled"] is False
    assert not (hermes_home.profiles_dir / "nora" / ".env").exists()


# --------------------------------------------------------------------------
# Voice setup, with the proven default preselected
# --------------------------------------------------------------------------

async def test_the_proven_default_is_what_an_unanswered_voice_step_writes(
        client, hermes_home):
    async with client as c:
        res = await c.post("/api/agents/create", json=payload())
        proven = (await c.get("/api/hermes")).json()["proven"]
    doc = hermes_home.agent_doc("nora")
    assert doc["pipeline"] == "realtime"
    assert doc["providers"] == {"realtime": "openai-gpt-realtime"}
    # The API states the default it preselects, so the screen cannot preselect a
    # different one and still look right.
    assert proven == {"pipeline": "realtime",
                      "realtime_provider": "openai-gpt-realtime"}
    assert res.json()["agent"]["pipeline"] == "realtime"


async def test_a_chosen_voice_reaches_the_agent_document(client, hermes_home):
    async with client as c:
        await c.post("/api/agents/create",
                     json=payload(knobs={"voice": "cedar", "model": "gpt-realtime-2"}))
    assert hermes_home.agent_doc("nora")["knobs"] == {"voice": "cedar",
                                                      "model": "gpt-realtime-2"}


async def test_cascade_is_selectable_and_must_be_cascade_shaped(client, hermes_home):
    """VC7: nothing is removed from the options. But a cascade-NAMED,
    realtime-SHAPED document is the s14b defect class, and the same validator the
    bridges resolve with refuses it here, before a profile exists."""
    async with client as c:
        good = await c.post("/api/agents/create", json=payload(
            name="casc", pipeline="cascade",
            providers={"stt": "deepgram", "llm": "gpt-4.1", "tts": "elevenlabs"}))
        bad = await c.post("/api/agents/create", json=payload(
            name="broken", pipeline="cascade",
            providers={"realtime": "openai-gpt-realtime"}))
    assert good.status_code == 201
    assert hermes_home.agent_doc("casc")["pipeline"] == "cascade"
    assert bad.status_code == 422
    assert not (hermes_home.profiles_dir / "broken").exists()


async def test_a_document_the_call_path_would_refuse_never_creates_a_profile(
        client, hermes_home):
    async with client as c:
        res = await c.post("/api/agents/create",
                           json=payload(providers={"realtime": "not-a-provider"}))
    assert res.status_code == 422
    assert not (hermes_home.profiles_dir / "nora").exists()
    assert not (hermes_home.config_dir / "agents" / "nora.yaml").exists()


# --------------------------------------------------------------------------
# BAR 1: a half-completed or abandoned creation leaves nothing startable
# --------------------------------------------------------------------------

async def test_a_failure_mid_write_leaves_nothing_startable(hermes_home, monkeypatch):
    """The crash case, driven at the point it actually happens.

    The assertion is ticket 12's predicate, not "the directory is gone": what
    matters is that no supervisor scan can find something to start, and that
    holds whether the tree was removed or merely left marked.
    """
    real_write = hermes_profiles._write

    def explode(path, text, mode=0o644):
        if path.name == hermes_profiles.CONFIG_NAME:
            raise OSError("disk full, halfway through")
        return real_write(path, text, mode)

    monkeypatch.setattr(hermes_profiles, "_write", explode)
    with pytest.raises(hermes_profiles.ProfileCreateError):
        hermes_profiles.create_profile({"name": "half"})
    assert not hermes_profiles.profile_is_startable(hermes_home.profiles_dir / "half")


async def test_when_the_tree_cannot_be_removed_the_marker_still_refuses_it(
        hermes_home, monkeypatch):
    """Cleanup is not the only guarantee, and must not be the only one.

    If removal fails too, the `.incomplete` marker is what keeps the profile out
    of the call path - degraded and reported, exactly as the Hermes side is
    built to handle, never silently startable.
    """
    real_write = hermes_profiles._write

    def explode(path, text, mode=0o644):
        if path.name == "SOUL.md":
            raise OSError("boom")
        return real_write(path, text, mode)

    monkeypatch.setattr(hermes_profiles, "_write", explode)
    monkeypatch.setattr(hermes_profiles.shutil, "rmtree",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    with pytest.raises(hermes_profiles.ProfileCreateError):
        hermes_profiles.create_profile({"name": "stuck"})
    path = hermes_home.profiles_dir / "stuck"
    assert path.is_dir(), "the tree survived, which is the case under test"
    assert (path / hermes_profiles.INCOMPLETE_MARKER).exists()
    assert not hermes_profiles.profile_is_startable(path)


async def test_a_cleanup_that_fails_halfway_still_leaves_nothing_startable(
        hermes_home, monkeypatch):
    """The case the marker alone does NOT cover.

    ``rmtree`` walks a directory in filesystem order, so a delete that fails
    part way through can take `.incomplete` with it and then stop - leaving a
    config.yaml with nothing marking it, which is a startable profile made by a
    cleanup. Removing config.yaml first, on its own, is what closes that.
    """
    def half_delete(target, *args, **kwargs):
        # The exact bad luck: the marker goes, then the delete gives up.
        marker = os.path.join(str(target), hermes_profiles.INCOMPLETE_MARKER)
        if os.path.exists(marker):
            os.unlink(marker)
        raise OSError("device busy, half way through")

    real_write = hermes_profiles._write

    def explode(path, text, mode=0o644):
        # Fail AFTER config.yaml exists, so there is something startable to
        # leave behind.
        if path.name == ".env":
            raise OSError("boom")
        return real_write(path, text, mode)

    monkeypatch.setattr(hermes_profiles, "_write", explode)
    monkeypatch.setattr(hermes_profiles.shutil, "rmtree", half_delete)
    with pytest.raises(hermes_profiles.ProfileCreateError):
        hermes_profiles.create_profile({"name": "unlucky", "telegram_connect": True,
                                        "telegram_bot_token": "t"})
    path = hermes_home.profiles_dir / "unlucky"
    assert path.is_dir(), "the tree survived, which is the case under test"
    assert not (path / hermes_profiles.INCOMPLETE_MARKER).exists(), \
        "the marker was taken by the half-finished delete, which is the setup"
    assert not hermes_profiles.profile_is_startable(path)


async def test_the_agent_document_is_written_while_the_marker_is_still_down(
        hermes_home):
    """Both halves of an Agent are covered by one guarantee.

    If the voice document were written after the marker came up, a failure
    there would leave a complete, startable profile with no Agent - and, worse,
    the reverse ordering leaves an Agent naming a profile that is still being
    written.
    """
    seen = {}

    def observe():
        path = hermes_home.profiles_dir / "nora"
        seen["marker_present"] = (path / hermes_profiles.INCOMPLETE_MARKER).exists()
        seen["startable_during"] = hermes_profiles.profile_is_startable(path)

    hermes_profiles.create_profile({"name": "nora"}, on_staged=observe)
    assert seen["marker_present"] is True
    assert seen["startable_during"] is False
    assert hermes_profiles.profile_is_startable(hermes_home.profiles_dir / "nora")


async def test_a_late_failure_removes_the_agent_document_that_was_already_written(
        client, hermes_home, monkeypatch):
    """The other end of the same guarantee.

    The agent document is written while the marker is still down, so a failure
    AFTER it lands - the marker unlink itself failing, say - would otherwise
    leave an Agent on the roster naming a profile that is still marked
    incomplete, i.e. a broken Agent the Outlet picker would offer.
    """
    real_unlink = os.unlink

    def explode(path, *args, **kwargs):
        if str(path).endswith(hermes_profiles.INCOMPLETE_MARKER) and not kwargs:
            raise OSError("could not clear the marker")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(hermes_profiles.os, "unlink", explode)
    async with client as c:
        res = await c.post("/api/agents/create", json=payload())
    assert res.status_code == 500
    assert not (hermes_home.config_dir / "agents" / "nora.yaml").exists()
    assert not hermes_profiles.profile_is_startable(
        hermes_home.profiles_dir / "nora")


async def test_a_failure_writing_the_agent_document_removes_the_profile_too(
        client, hermes_home, monkeypatch):
    def explode(path, doc):
        raise OSError("no space left for the agent document")

    monkeypatch.setattr(app_module, "_atomic_write_yaml", explode)
    async with client as c:
        res = await c.post("/api/agents/create", json=payload())
    assert res.status_code == 500
    assert not (hermes_home.profiles_dir / "nora").exists()
    assert not (hermes_home.config_dir / "agents" / "nora.yaml").exists()


async def test_the_profile_is_given_to_whoever_owns_the_profiles_directory(
        hermes_home, monkeypatch):
    """The two containers do not agree about who they are.

    The dashboard image sets no USER and runs as root; the agent image runs as
    `pn`, and its gateway writes sessions and a state db into the profile on
    every message. A root-owned profile is one the being cannot use - so every
    file created is chowned to the owner of the profiles directory, which is by
    construction that user. Simulated here (the test process cannot chown to
    another uid) by making the directory look like somebody else's.
    """
    chowned = []
    monkeypatch.setattr(hermes_profiles, "_owner_of",
                        lambda directory: (os.getuid() + 1, os.getgid() + 1))
    monkeypatch.setattr(hermes_profiles.os, "chown",
                        lambda target, uid, gid: chowned.append((str(target), uid, gid)))
    hermes_profiles.create_profile({"name": "nora"})
    given = {name for name, _, _ in chowned}
    path = hermes_home.profiles_dir / "nora"
    # The directory AND everything in it, not just the top level: the gateway
    # writes into sessions/ and reads config.yaml.
    assert str(path) in given
    assert str(path / "config.yaml") in given
    assert str(path / "sessions") in given
    assert {uid for _, uid, _ in chowned} == {os.getuid() + 1}


async def test_a_profile_that_cannot_be_given_away_is_not_left_behind(
        hermes_home, monkeypatch):
    """If the chown fails, the profile would be one the gateway cannot run.

    That is a broken being, so it is refused and removed rather than shipped -
    the same treatment as any other failure mid-write.
    """
    monkeypatch.setattr(hermes_profiles, "_owner_of",
                        lambda directory: (os.getuid() + 1, os.getgid() + 1))
    monkeypatch.setattr(hermes_profiles.os, "chown",
                        lambda *a: (_ for _ in ()).throw(PermissionError("not root")))
    with pytest.raises(hermes_profiles.ProfileCreateError):
        hermes_profiles.create_profile({"name": "nora"})
    assert not hermes_profiles.profile_is_startable(hermes_home.profiles_dir / "nora")


async def test_answering_questions_writes_nothing(client, hermes_home):
    """The abandonment guarantee's first line: nothing is persisted until the
    final submit, so every read the wizard makes while the operator is still
    deciding leaves the disk exactly as it was."""
    hermes_home.source("scout")
    before_profiles = sorted(p.name for p in hermes_home.profiles_dir.iterdir())
    before_agents = sorted(p.name for p in (hermes_home.config_dir / "agents").iterdir())
    async with client as c:
        await c.get("/api/hermes")
        await c.get("/api/hermes/profiles/scout")
        await c.get("/api/agents")
        await c.get("/api/providers/deepgram-aura/voices")
    assert sorted(p.name for p in hermes_home.profiles_dir.iterdir()) == before_profiles
    assert sorted(p.name for p in
                  (hermes_home.config_dir / "agents").iterdir()) == before_agents


# --------------------------------------------------------------------------
# BAR 2: creating an Agent disturbs no Outlet and needs no redeploy
# --------------------------------------------------------------------------

async def test_creating_an_agent_changes_no_outlet_assignment(client, hermes_home):
    (hermes_home.config_dir / "active.yaml").write_text(yaml.safe_dump(
        {"outlets": {"phone": {"inbound": None, "outbound": None},
                     "talk": {"inbound": None, "outbound": None}}}))
    async with client as c:
        before = (await c.get("/api/active")).json()
        stored_before = (hermes_home.config_dir / "active.yaml").read_bytes()
        await c.post("/api/agents/create", json=payload())
        after = (await c.get("/api/active")).json()
    assert after["outlets"] == before["outlets"]
    assert after["slot_warnings"] == before["slot_warnings"]
    # The pointer file is not merely equivalent, it is untouched: creation never
    # opens it, so there is no write to get wrong.
    assert (hermes_home.config_dir / "active.yaml").read_bytes() == stored_before


async def test_a_call_in_progress_resolves_the_same_profile_before_and_after(
        client, hermes_home):
    """The closest a test on this machine can get to "does not interrupt a call".

    A call resolves its Agent once, at setup, through
    ``profiles.load_effective_profile``. So: assign an Outlet, resolve it the way
    a bridge does, create a new Agent, resolve again - the answer must be the
    same object-for-object. This proves the configuration a live call reads is
    undisturbed. It does NOT prove an in-flight media session survives; only a
    real call can.
    """
    hermes_home.source("scout")
    async with client as c:
        await c.post("/api/agents/create", json=payload(name="onduty"))
        await c.put("/api/active",
                    json={"outlets": {"phone": {"inbound": "onduty"}}})
        before = profiles.load_effective_profile("inbound", outlet="phone")
        assert before is not None and before.doc["id"] == "onduty"
        await c.post("/api/agents/create", json=payload(name="newcomer"))
        after = profiles.load_effective_profile("inbound", outlet="phone")
    assert after is not None
    assert after.doc == before.doc


async def test_creation_touches_no_existing_agent_file(client, hermes_home):
    async with client as c:
        await c.post("/api/agents/create", json=payload(name="first"))
        before = (hermes_home.config_dir / "agents" / "first.yaml").read_bytes()
        await c.post("/api/agents/create", json=payload(name="second"))
    assert (hermes_home.config_dir / "agents" / "first.yaml").read_bytes() == before


async def test_creation_reaches_no_network(make_app, hermes_home):
    """No redeploy, and no call out either: two file writes and nothing else.

    The app's outbound transport is a SentinelTransport, which RAISES on any
    request and records it. A creation path that phoned a container API, a gateway or
    a provider fails here instead of passing quietly, and the recorded list
    keeps the claim true even if a future path swallows the exception.
    """
    from conftest import SentinelTransport

    sentinel = SentinelTransport()
    application = make_app(sentinel)
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver") as c:
        res = await c.post("/api/agents/create", json=payload())
    assert res.status_code == 201
    assert sentinel.calls == []


# --------------------------------------------------------------------------
# What the screen reads
# --------------------------------------------------------------------------

async def test_the_state_endpoint_lists_existing_profiles(client, hermes_home):
    hermes_home.source("scout")
    async with client as c:
        rows = (await c.get("/api/hermes")).json()["profiles"]
    assert [r["name"] for r in rows] == ["scout"]
    assert rows[0]["complete"] is True


async def test_a_half_created_profile_is_listed_as_incomplete(client, hermes_home):
    """Left by somebody else, or by a crash: reported, never offered as a
    healthy source to inherit from."""
    path = hermes_home.profiles_dir / "halfway"
    path.mkdir()
    (path / "config.yaml").write_text("model: {}\n")
    (path / hermes_profiles.INCOMPLETE_MARKER).write_text("")
    async with client as c:
        rows = (await c.get("/api/hermes")).json()["profiles"]
    row = next(r for r in rows if r["name"] == "halfway")
    assert row["complete"] is False and row["incomplete"] is True


async def test_the_gateway_registry_is_read_from_the_config_dir(client, hermes_home):
    """Ticket 12 F4: the reader finds gateways.json under VOICE_CONFIG_DIR,
    which is why wiring it costs no second redeploy."""
    gateways = hermes_home.config_dir / "gateways"
    gateways.mkdir()
    (gateways / "gateways.json").write_text(
        '{"profiles": {"scout": {"status": "ok", '
        '"gateway_url": "http://127.0.0.1:18790"}}}')
    hermes_home.source("scout")
    async with client as c:
        body = (await c.get("/api/hermes")).json()
    row = next(r for r in body["profiles"] if r["name"] == "scout")
    assert row["gateway_status"] == "ok"
    assert row["gateway_url"] == "http://127.0.0.1:18790"
    assert body["registry_path"].endswith(os.path.join("gateways", "gateways.json"))


async def test_a_missing_gateway_registry_is_not_an_error(client, hermes_home):
    """It is absent until that deploy lands, and its absence means "nothing has
    told us which gateways are running", never "no profiles exist"."""
    hermes_home.source("scout")
    async with client as c:
        rows = (await c.get("/api/hermes")).json()["profiles"]
    assert [r["name"] for r in rows] == ["scout"]
    assert rows[0]["gateway_status"] is None


async def test_profile_detail_404s_for_a_name_that_is_not_there(client, hermes_home):
    async with client as c:
        res = await c.get("/api/hermes/profiles/ghost")
    assert res.status_code == 404


async def test_creating_a_direct_agent_defaults_to_tools_on(client, hermes_home):
    async with client as c:
        res = await c.post("/api/agents/create", json=payload(
            pipeline="cascade", providers={
                "stt": "deepgram", "llm": "hermes-agent", "tts": "elevenlabs"}))
        assert res.status_code == 201, res.text
        loaded = (await c.get("/api/agents/nora")).json()
    assert hermes_home.agent_doc("nora") == loaded
    assert profiles.on_call_tools_of(loaded) is True
    assert hermes_profiles.profile_is_startable(hermes_home.profiles_dir / "nora")
