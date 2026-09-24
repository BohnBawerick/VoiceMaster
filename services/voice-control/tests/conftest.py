"""Shared fixtures for the voice-control suite.

The whole suite is OFFLINE by construction:
  - every app instance gets an injected httpx transport (mock handler or a
    tripwire SentinelTransport) via ``app.state.transport``;
  - an autouse fixture scrubs every known secret env var plus VOICE_CONFIG_DIR
    and VOICE_AGENT, so a shell with the deployment secrets sourced changes nothing;
  - SentinelTransport raises on ANY request, so an accidental live call fails
    the test instead of silently dialing a vendor.
"""
import os
import sys
from pathlib import Path

import httpx
import pytest

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import app as app_module  # noqa: E402
from voicecore import probes  # noqa: E402
from voicecore import profiles  # noqa: E402

CANONICAL_DIR = APP_DIR.parent / "voice-config"

# Every env var any test could leak through: all registry families + all probers.
ALL_SECRET_ENVS = sorted(
    {e["secret_env"] for e in profiles.load_registry(CANONICAL_DIR).values()}
    | set(probes.PROBERS)
)

OPENAI_IDS = ["openai-gpt-realtime", "gpt-4.1", "openai-gpt-4o-transcribe"]

# Registry ids in file order. Count is incidental — assert membership+order, not a
# number baked into the name (a new provider must not force a rename).
CANONICAL_PROVIDERS = [
    "openai-gpt-realtime", "google-gemini-live",
    "nvidia-nemotron", "openrouter", "grok", "gpt-4.1", "gemini-2.5-flash",
    "claude-haiku-4.5", "glm", "opencodego", "hermes-agent",
    "elevenlabs-scribe", "deepgram", "soniox", "assemblyai", "openai-gpt-4o-transcribe",
    "nvidia-stt",
    "elevenlabs", "cartesia", "deepgram-aura", "inworld",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Deterministic env: no real keys, no ambient VOICE_* config."""
    for name in ALL_SECRET_ENVS:
        monkeypatch.delenv(name, raising=False)
    for name in ("VOICE_CONFIG_DIR", "VOICE_AGENT",
                 # s5 surface - a shell with the deployment secrets sourced must change nothing:
                 "VOICE_EVENTLOG_PATH", "VOICE_BENCH_DIR", "VOICE_PUBLIC_HOST",
                 "VOICE_OUTBOUND_ALLOWED_NUMBERS", "TWILIO_ACCOUNT_SID",
                 "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
                 # ticket 09 — owner number / mode-c URL must not leak in from a
                 # sourced shell; each test that needs them sets them itself:
                 "VOICE_OWNER_NUMBER", "VOICE_INBOUND_ALLOWED_CALLERS",
                 "VOICE_MODE_C_URL", "HERMES_GATEWAY_TOKEN",
                 "HERMES_GATEWAY_URL", "HERMES_PROFILE_GATEWAY_URLS",
                 "VOICE_MISSION_TIMEOUT_S",
                 # s6 surface — basic auth is opt-in via env; tests stay open
                 # unless a test sets these explicitly:
                 "VOICE_DASHBOARD_USER", "VOICE_DASHBOARD_PASSWORD",
                 # ticket 11 — the scheduler's knobs, and the two vars that
                 # decide what a naive local time MEANS. A developer shell with
                 # TZ set would otherwise resolve "3pm" somewhere else than CI
                 # does, which is the one class of bug these tests exist to
                 # catch; every test that needs a zone names it.
                 "TZ", "VOICE_TIMEZONE", "VOICE_SCHEDULER_ENABLED",
                 "VOICE_SCHEDULE_TICK_S", "VOICE_SCHEDULE_GRACE_S",
                 "VOICE_SCHEDULE_STALE_CLAIM_S",
                 # the call archive's store choice (voicecore.call_store). The
                 # archive tests name the store they read; the rest read an
                 # empty SQLite archive in their own tmp dir (below).
                 "VOICE_ARCHIVE", "HINDSIGHT_URL", "HINDSIGHT_BANK"):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_call_archive(monkeypatch, tmp_path):
    """A fresh SQLite call archive path per test; the file does not exist until written."""
    monkeypatch.setenv("VOICE_ARCHIVE_PATH", str(tmp_path / "calls.sqlite3"))


class SentinelTransport(httpx.AsyncBaseTransport):
    """Trips loudly on ANY request — proves a code path makes zero network calls."""

    def __init__(self):
        self.calls = []

    async def handle_async_request(self, request):
        self.calls.append(request)
        raise AssertionError(
            f"unexpected network attempt: {request.method} {request.url.host}")


class RecordingTransport(httpx.AsyncBaseTransport):
    """Routes requests through an async handler, recording every request."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def handle_async_request(self, request):
        self.calls.append(request)
        return await self.handler(request)


def entry(id="fake", role="llm", secret_env="FAKE_API_KEY", **overrides):
    doc = {
        "id": id, "role": role, "display_name": id, "secret_env": secret_env,
        "capabilities": [], "default_knobs": {},
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def make_app():
    """A fresh app wired to the given transport (never the real network)."""

    def _make(transport):
        application = app_module.create_app()
        application.state.transport = transport
        return application

    return _make


@pytest.fixture
def make_client(make_app):
    """ASGI-level async client for a fresh app + transport pair."""

    def _make(transport):
        application = make_app(transport)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver")

    return _make


# --------------------------------------------------------------------------
# Browser fixtures, shared by test_calls_browser and test_calls_matrix.
#
# Playwright is imported lazily inside the fixtures: it is a dev-only
# dependency, and importing it here would make the whole offline suite
# unrunnable without it.
# --------------------------------------------------------------------------


# Module-scoped, NOT session-scoped: Playwright's sync API runs its own event
# loop, and holding it open past the last browser test leaves `asyncio_mode =
# auto` handing that loop to the async tests in later modules, which then never
# await their coroutines. One browser per browser-test module is the cost of
# keeping the rest of the suite working.
@pytest.fixture(scope="module")
def browser():
    playwright_api = pytest.importorskip(
        "playwright.sync_api", reason="playwright is not installed (requirements-dev.txt)"
    )
    with playwright_api.sync_playwright() as driver:
        try:
            instance = driver.chromium.launch(
                executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None
            )
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright: {exc}")
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    new_page = context.new_page()
    # Everything here is served from localhost, so a wait that reaches ten
    # seconds is a defect, not slowness: fail then rather than at 30.
    new_page.set_default_timeout(10_000)
    yield new_page
    context.close()


@pytest.fixture
def stack(monkeypatch):
    """Bring up (Hindsight mock, dashboard) for one scenario; tear both down."""
    from browser_harness import Stack

    created = []

    def _start(banks, configured_bank="voice"):
        monkeypatch.setenv("HINDSIGHT_BANK", configured_bank)
        instance = Stack(banks, configured_bank)
        created.append(instance)
        return instance

    yield _start
    for instance in created:
        instance.close()


@pytest.fixture
def matrix_stack(stack):
    """Start the store described by one ``fixture_matrix`` cell."""

    def _start(cell):
        return stack(cell.banks, configured_bank=cell.configured_bank)

    return _start
