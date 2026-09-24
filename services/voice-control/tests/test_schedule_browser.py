"""Browser tests for the Schedule screen (ticket 11).

Real Chromium, the real built bundle, the real app process — which means the
REAL scheduler, because ``Stack`` runs uvicorn and uvicorn runs the lifespan.
So the last test here is the closest thing to the open acceptance line that
this machine can honestly do: a Schedule written through the screen's own API,
firing on the clock, dialling a phone bridge that answers on localhost, and the
screen showing it placed. It is still not a call that rings — only the deploy
can close that.

The phone bridge is a local mock. Nothing here places a real call.
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed (requirements-dev.txt)"
)

from browser_harness import free_port  # noqa: E402

OWNER = "+61491570156"
CANONICAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "..", "voice-config", "providers.yaml")

AGENT = {
    "id": "scout",
    "description": "the live one",
    "enabled": True,
    "pipeline": "realtime",
    "providers": {"realtime": "openai-gpt-realtime"},
}


class _ModeCHandler(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw.decode() or "{}")
        type(self).received.append({"path": self.path, "body": body,
                                    "auth": self.headers.get("Authorization")})
        payload = json.dumps({
            "placed": True, "call_sid": "CAschedule1",
            "call_id": "cid-scheduled", "agent": body.get("agent"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def mode_c(monkeypatch):
    _ModeCHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", free_port()), _ModeCHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("VOICE_MODE_C_URL", url)
    monkeypatch.setenv("HERMES_GATEWAY_TOKEN", "gw-browser")
    yield {"url": url, "received": _ModeCHandler.received}
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("VOICE_OWNER_NUMBER", OWNER)
    monkeypatch.setenv("VOICE_TIMEZONE", "Australia/Perth")
    (tmp_path / "agents").mkdir()
    (tmp_path / "providers.yaml").write_text(open(CANONICAL).read())
    (tmp_path / "agents" / "scout.yaml").write_text(yaml.safe_dump(AGENT))
    (tmp_path / "active.yaml").write_text(yaml.safe_dump({
        "outlets": {"phone": {"inbound": None, "outbound": None},
                    "talk": {"inbound": None, "outbound": None}}}))
    return tmp_path


def _write(page, mission, when_value):
    """Write a Schedule the way the owner does: Schedule, then "Schedule a call",
    which is the one New call form with Later selected (N3), then back to the list
    from the confirmation."""
    page.click("[data-testid='schedule-new']")
    page.wait_for_url("**/schedule/new")
    page.wait_for_selector("[data-testid='schedule-form']")
    page.select_option("[data-testid='place-agent']", "scout")
    page.fill("[data-testid='place-mission']", mission)
    page.fill("[data-testid='schedule-at']", when_value)
    page.click("[data-testid='schedule-submit']")
    page.wait_for_selector("[data-testid='schedule-success']")
    page.click("[data-testid='schedule-success'] a[href='/schedule']")
    page.wait_for_selector("[data-testid='schedule']")


def _local_input(minutes_ahead):
    when = datetime.now() + timedelta(minutes=minutes_ahead)
    return when.strftime("%Y-%m-%dT%H:%M")


def test_schedule_is_a_real_url_reachable_from_the_nav(stack, page, config, mode_c):
    running = stack({})
    page.goto(f"{running.base}/", wait_until="networkidle")
    page.click("[data-testid='nav-schedule']")
    page.wait_for_selector("[data-testid='schedule']")
    assert page.url.rstrip("/").endswith("/schedule")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("[data-testid='schedule']")
    assert "Soon" not in page.inner_text("[data-testid='nav-schedule']")


def test_writing_a_schedule_lists_it_as_upcoming_with_its_wall_clock(
        stack, page, config, mode_c):
    running = stack({})
    page.goto(f"{running.base}/schedule", wait_until="networkidle")
    _write(page, "Ask if Friday still works.", _local_input(90))
    page.wait_for_selector("[data-testid='schedule-upcoming'] .schedule-row")

    # It is the next call, named in the banner at the top...
    assert "scout" in page.inner_text("[data-testid='schedule-next']")
    row = page.query_selector("[data-testid='schedule-upcoming'] .schedule-row")
    assert row is not None, "the Schedule was written but not listed"
    text = row.inner_text()
    assert "scout" in text
    assert OWNER in text
    assert "Ask if Friday still works." in text
    # The phone has not rung: writing a Schedule dials nothing.
    assert mode_c["received"] == []


def test_cancelling_from_the_screen_takes_it_out_of_upcoming(
        stack, page, config, mode_c):
    running = stack({})
    page.goto(f"{running.base}/schedule", wait_until="networkidle")
    _write(page, "Cancel me.", _local_input(120))
    page.wait_for_selector("[data-testid='schedule-upcoming'] .schedule-row")

    row = page.query_selector("[data-testid='schedule-upcoming'] .schedule-row")
    schedule_id = row.get_attribute("data-testid").replace("schedule-row-", "")
    page.click(f"[data-testid='schedule-cancel-{schedule_id}']")
    page.wait_for_selector("[data-testid='schedule-upcoming-empty']")

    # ...and it is under Past, saying what became of it.
    page.click("[data-testid='schedule-tab-past']")
    page.wait_for_selector(f"[data-testid='schedule-status-{schedule_id}']")
    status = page.inner_text(f"[data-testid='schedule-status-{schedule_id}']")
    assert "Cancelled" in status
    assert mode_c["received"] == []


def test_a_schedule_written_here_fires_on_the_clock_and_shows_placed(
        stack, page, config, mode_c):
    """The whole ticket, end to end, as far as this machine can go.

    The Schedule is created through the API the screen uses (the form's
    datetime-local input has minute resolution, and waiting a minute in a test
    is not a better proof). Everything after that is real: the app's own
    scheduler notices, places the call down the manual path, and the screen —
    reloaded, not re-rendered from memory — shows it placed.
    """
    running = stack({})
    page.goto(f"{running.base}/schedule", wait_until="networkidle")
    created = page.evaluate(
        """async (at) => {
            const res = await fetch('/api/schedules', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({
                agent: 'scout', to: '""" + OWNER + """',
                mission: 'Ticket 11 browser check.', disclose: true, at,
                tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
              }),
            });
            return {status: res.status, body: await res.json()};
        }""",
        (datetime.now() + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%S"),
    )
    assert created["status"] == 201, created

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not mode_c["received"]:
        time.sleep(0.1)
    assert len(mode_c["received"]) == 1, "the scheduler never dialled the bridge"
    dialled = mode_c["received"][0]
    assert dialled["path"] == "/voice/outbound"
    assert dialled["auth"] == "Bearer gw-browser"
    assert dialled["body"]["agent"] == "scout"
    assert dialled["body"]["brief"] == "Ticket 11 browser check."
    assert dialled["body"]["disclose"] is True
    assert dialled["body"]["to"] == OWNER

    schedule_id = created["body"]["id"]
    page.reload(wait_until="networkidle")
    page.click("[data-testid='schedule-tab-past']")
    page.wait_for_selector(f"[data-testid='schedule-status-{schedule_id}']")
    assert "Placed" in page.inner_text(f"[data-testid='schedule-status-{schedule_id}']")
    assert "cid-scheduled" in page.inner_text(f"[data-testid='schedule-row-{schedule_id}']")
    assert page.query_selector(f"[data-testid='schedule-cancel-{schedule_id}']") is None
    # And the assignment it did not touch.
    assert yaml.safe_load((config / "active.yaml").read_text())["outlets"]["phone"] == \
        {"inbound": None, "outbound": None}
