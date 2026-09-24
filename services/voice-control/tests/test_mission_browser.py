"""Browser tests for Mission authoring on /place.

Real Chromium, real built bundle. The Hermes gateway and the phone bridge
are local mocks so this machine neither talks to a live Agent nor places
a real call.
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
    "hermes_profile": "scout",
    "pipeline": "realtime",
    "providers": {"realtime": "openai-gpt-realtime"},
}


class _HermesHandler(BaseHTTPRequestHandler):
    received = []
    reply = "Call the dentist and move Thursday to Friday."
    status = 200
    empty = False

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        type(self).received.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": raw,
        })
        if self.status != 200:
            payload = json.dumps({"error": "gateway down"}).encode()
            self.send_response(self.status)
        elif self.empty:
            payload = json.dumps({"choices": [{"message": {"content": ""}}]}).encode()
            self.send_response(200)
        elif self.path == "/v1/audio/transcriptions":
            payload = json.dumps({"text": "move thursday to friday"}).encode()
            self.send_response(200)
        else:
            payload = json.dumps({
                "choices": [{"message": {"content": self.reply}}]
            }).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _ModeCHandler(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw.decode() or "{}")
        type(self).received.append({"path": self.path, "body": body})
        payload = json.dumps({
            "placed": True, "call_sid": "CAbrowser10",
            "call_id": "cid-10", "agent": body.get("agent"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def hermes(monkeypatch):
    _HermesHandler.received = []
    _HermesHandler.reply = "Call the dentist and move Thursday to Friday."
    _HermesHandler.status = 200
    _HermesHandler.empty = False
    server = ThreadingHTTPServer(("127.0.0.1", free_port()), _HermesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("HERMES_GATEWAY_URL", url)
    monkeypatch.setenv("HERMES_PROFILE_GATEWAY_URLS", f"scout={url}")
    monkeypatch.setenv("HERMES_GATEWAY_TOKEN", "gw-browser")
    yield {"url": url, "handler": _HermesHandler}
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def mode_c(monkeypatch):
    _ModeCHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", free_port()), _ModeCHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("VOICE_MODE_C_URL", url)
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


def _open_place(stack, page):
    running = stack({})
    page.goto(f"{running.base}/place", wait_until="networkidle")
    page.wait_for_selector("[data-testid='place-mission']")
    page.select_option("[data-testid='place-agent']", "scout")
    return running


def test_elaborate_fills_the_field_and_does_not_dial(
        stack, page, config, hermes, mode_c):
    _open_place(stack, page)
    page.fill("[data-testid='place-mission']", "move thursday")
    page.click("[data-testid='place-expand']")
    page.wait_for_function(
        "document.querySelector('[data-testid=place-mission]').value.includes('dentist')"
    )
    assert "Call the dentist" in page.input_value("[data-testid='place-mission']")
    assert page.query_selector("[data-testid='place-success']") is None
    assert mode_c["received"] == []
    assert hermes["handler"].received
    assert all(item["path"] != "/voice/outbound" for item in hermes["handler"].received)
    assert all("/audio/speech" not in item["path"] for item in hermes["handler"].received)


def test_operator_must_review_before_place(stack, page, config, hermes, mode_c):
    """Never sent unseen: expand, edit, then place. The dial carries the edit."""
    _open_place(stack, page)
    page.fill("[data-testid='place-mission']", "short")
    page.click("[data-testid='place-expand']")
    page.wait_for_function(
        "document.querySelector('[data-testid=place-mission]').value.includes('dentist')"
    )
    page.fill("[data-testid='place-mission']", "the operator edited this")
    page.click("[data-testid='place-submit']")
    page.wait_for_selector("[data-testid='place-success']")
    assert len(mode_c["received"]) == 1
    assert mode_c["received"][0]["path"] == "/voice/outbound"
    assert mode_c["received"][0]["body"]["brief"] == "the operator edited this"


def test_expand_failure_leaves_the_typed_text(stack, page, config, hermes, mode_c):
    hermes["handler"].status = 503
    _open_place(stack, page)
    page.fill("[data-testid='place-mission']", "keep this typed line")
    page.click("[data-testid='place-expand']")
    page.wait_for_selector("[data-testid='place-assist-error']")
    assert page.input_value("[data-testid='place-mission']") == "keep this typed line"
    assert mode_c["received"] == []


def test_expand_empty_reply_leaves_the_typed_text(stack, page, config, hermes, mode_c):
    hermes["handler"].empty = True
    _open_place(stack, page)
    page.fill("[data-testid='place-mission']", "keep this typed line")
    page.click("[data-testid='place-expand']")
    page.wait_for_selector("[data-testid='place-assist-error']")
    assert page.input_value("[data-testid='place-mission']") == "keep this typed line"


def test_hand_typed_mission_still_places(stack, page, config, hermes, mode_c):
    _open_place(stack, page)
    page.fill("[data-testid='place-mission']", "typed in full by hand")
    page.click("[data-testid='place-submit']")
    page.wait_for_selector("[data-testid='place-success']")
    assert mode_c["received"][0]["body"]["brief"] == "typed in full by hand"


def test_record_unavailable_is_honest_and_keeps_the_form(
        stack, page, config, hermes, mode_c):
    running = stack({})
    page.add_init_script("""
        Object.defineProperty(navigator, 'mediaDevices', {
            configurable: true, get: () => undefined
        });
    """)
    page.goto(f"{running.base}/place", wait_until="networkidle")
    page.wait_for_selector("[data-testid='place-record']")
    page.select_option("[data-testid='place-agent']", "scout")
    page.fill("[data-testid='place-mission']", "keep this typed line")
    page.click("[data-testid='place-record']")
    page.wait_for_selector("[data-testid='place-assist-error']")
    assert "cannot record" in page.inner_text("[data-testid='place-assist-error']").lower()
    assert page.input_value("[data-testid='place-mission']") == "keep this typed line"
    assert mode_c["received"] == []
