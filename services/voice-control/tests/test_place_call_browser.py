"""Browser tests for Place a call. Real Chromium, real built bundle, real
VOICE_CONFIG_DIR. The phone bridge is a local mock so this machine does not
place a real call.
"""
import json
import os
import threading
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
            "placed": True, "call_sid": "CAbrowser1",
            "call_id": "cid-browser", "agent": body.get("agent"),
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
    (tmp_path / "agents").mkdir()
    (tmp_path / "providers.yaml").write_text(open(CANONICAL).read())
    (tmp_path / "agents" / "scout.yaml").write_text(yaml.safe_dump(AGENT))
    (tmp_path / "active.yaml").write_text(yaml.safe_dump({
        "outlets": {
            "phone": {"inbound": None, "outbound": None},
            "talk": {"inbound": None, "outbound": None},
        }
    }))
    return tmp_path


def test_place_screen_is_a_real_url(stack, page, config, mode_c):
    running = stack({})
    page.goto(f"{running.base}/", wait_until="networkidle")
    page.click("[data-testid='place-from-calls']")
    page.wait_for_selector("[data-testid='place-call']")
    assert page.url.rstrip("/").endswith("/place")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("[data-testid='place-call']")


def test_owner_is_on_speed_dial_and_there_is_no_dry_run(
        stack, page, config, mode_c):
    running = stack({})
    page.goto(f"{running.base}/place", wait_until="networkidle")
    page.wait_for_selector("[data-testid='speed-dial-owner']")
    assert OWNER in page.inner_text("[data-testid='speed-dial-owner']")
    assert page.query_selector("text=dry run") is None
    assert page.query_selector("text=Dry-run") is None
    assert page.is_visible("[data-testid='place-disclose']")


def test_placing_a_call_from_the_browser_hits_the_bridge(
        stack, page, config, mode_c):
    pointer = (config / "active.yaml").read_text()
    running = stack({})
    page.goto(f"{running.base}/place", wait_until="networkidle")
    page.wait_for_selector("[data-testid='place-agent']")
    page.select_option("[data-testid='place-agent']", "scout")
    page.fill("[data-testid='place-mission']", "Ask if Friday still works.")
    page.check("[data-testid='place-disclose']")
    page.click("[data-testid='place-submit']")
    page.wait_for_selector("[data-testid='place-success']")
    assert "Ask if Friday still works." in page.inner_text("[data-testid='place-success']")
    assert (config / "active.yaml").read_text() == pointer
    assert len(mode_c["received"]) == 1
    sent = mode_c["received"][0]
    assert sent["path"] == "/voice/outbound"
    assert sent["auth"] == "Bearer gw-browser"
    assert sent["body"]["agent"] == "scout"
    assert sent["body"]["brief"] == "Ask if Friday still works."
    assert sent["body"]["disclose"] is True
    assert sent["body"]["to"] == OWNER
