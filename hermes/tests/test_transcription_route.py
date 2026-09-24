"""Tests for the gateway transcription-route overlay.

Run from the hermes/ directory:

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_transcription_route.py -q

No aiohttp, no hermes-agent, no live network, no credentials.
"""

import os
import sys
import types

import pytest

OVERLAY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "gateway_overlays",
)
sys.path.insert(0, OVERLAY)

import hermes_transcription_route as tr  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_install():
    tr.reset_install_for_tests()
    yield
    tr.reset_install_for_tests()


def test_error_body_matches_gateway_envelope():
    body = tr.error_body("boom", code="stt_failed", err_type="api_error")
    assert set(body) == {"error"}
    assert body["error"]["message"] == "boom"
    assert body["error"]["type"] == "api_error"
    assert body["error"]["code"] == "stt_failed"
    assert body["error"]["param"] is None


def test_safe_suffix_keeps_known_extension_and_strips_paths():
    assert tr.safe_audio_suffix("note.wav") == ".wav"
    assert tr.safe_audio_suffix("/tmp/../etc/passwd.webm") == ".webm"
    assert tr.safe_audio_suffix("no-extension") == tr.DEFAULT_SUFFIX
    assert tr.safe_audio_suffix("x.exe") == tr.DEFAULT_SUFFIX
    assert tr.safe_audio_suffix(None) == tr.DEFAULT_SUFFIX


def test_success_is_only_the_backend_text():
    kind, body, status = tr.classify_stt_result(
        {"success": True, "transcript": "hello from the mic"}
    )
    assert kind == "ok"
    assert status == 200
    assert body == {"text": "hello from the mic"}


def test_honest_silence_is_empty_text_not_an_error():
    kind, body, status = tr.classify_stt_result({"success": True, "transcript": ""})
    assert kind == "ok"
    assert status == 200
    assert body == {"text": ""}


def test_failure_is_an_error_and_never_carries_a_transcript():
    kind, body, status = tr.classify_stt_result(
        {
            "success": False,
            "transcript": "please ignore this plausible text",
            "error": "provider 503",
        }
    )
    assert kind == "error"
    assert status == 502
    assert "text" not in body
    assert "please ignore" not in str(body)
    assert body["error"]["message"] == "provider 503"
    assert body["error"]["code"] == "stt_failed"


def test_success_without_transcript_is_an_error_not_empty_success():
    kind, body, status = tr.classify_stt_result({"success": True})
    assert kind == "error"
    assert status == 502
    assert "text" not in body
    assert body["error"]["code"] == "stt_missing_transcript"


def test_success_with_null_transcript_is_an_error():
    kind, body, status = tr.classify_stt_result({"success": True, "transcript": None})
    assert kind == "error"
    assert "text" not in body


def test_non_dict_and_non_string_results_are_errors():
    kind, body, status = tr.classify_stt_result("hello there")
    assert kind == "error"
    assert status == 502
    assert "hello there" not in body.get("text", "")
    kind, body, status = tr.classify_stt_result(
        {"success": True, "transcript": ["not", "text"]}
    )
    assert kind == "error"
    assert body["error"]["code"] == "stt_invalid_transcript"


def test_unavailable_provider_is_503():
    kind, body, status = tr.classify_stt_result(
        {"success": False, "error": "No STT provider available"}
    )
    assert kind == "error"
    assert status == 503
    assert "text" not in body


def test_append_is_additive_and_preserves_handler_identity():
    health = object()
    chat = object()
    models = object()
    responses = object()
    original = [
        ("GET", "/health", health),
        ("POST", "/v1/chat/completions", chat),
        ("POST", "/v1/responses", responses),
        ("GET", "/v1/models", models),
    ]
    handler = object()
    out = tr.append_transcription_route(original, handler)
    assert out[:4] == original
    assert out[0][2] is health
    assert out[1][2] is chat
    assert out[2][2] is responses
    assert out[3][2] is models
    assert out[-1] == ("POST", tr.TRANSCRIPTION_PATH, handler)
    # original list is not mutated
    assert len(original) == 4


def test_append_does_not_duplicate_an_upstream_route():
    existing = object()
    routes = [
        ("GET", "/health", object()),
        ("POST", tr.TRANSCRIPTION_PATH, existing),
    ]
    out = tr.append_transcription_route(routes, object())
    matches = [row for row in out if row[1] == tr.TRANSCRIPTION_PATH]
    assert len(matches) == 1
    assert matches[0][2] is existing


def _install_fake_adapter(monkeypatch, table):
    adapter_cls = type(
        "APIServerAdapter",
        (),
        {"_http_route_table": lambda self: list(table)},
    )
    fake = types.ModuleType("gateway.platforms.api_server")
    fake.APIServerAdapter = adapter_cls
    pkg = types.ModuleType("gateway")
    platforms = types.ModuleType("gateway.platforms")
    monkeypatch.setitem(sys.modules, "gateway", pkg)
    monkeypatch.setitem(sys.modules, "gateway.platforms", platforms)
    monkeypatch.setitem(sys.modules, "gateway.platforms.api_server", fake)
    return adapter_cls


def test_install_patches_the_adapter_and_is_idempotent(monkeypatch):
    health = object()
    table = [("GET", "/health", health), ("POST", "/v1/chat/completions", object())]
    adapter_cls = _install_fake_adapter(monkeypatch, table)

    assert tr.install() is True
    assert tr.install() is True

    routes = adapter_cls()._http_route_table()
    assert routes[0] == ("GET", "/health", health)
    assert routes[0][2] is health
    assert ("POST", tr.TRANSCRIPTION_PATH, tr.handle_transcriptions) in routes
    assert sum(1 for row in routes if row[1] == tr.TRANSCRIPTION_PATH) == 1


def test_install_noops_when_the_adapter_cannot_be_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway", types.ModuleType("gateway"))
    monkeypatch.setitem(
        sys.modules, "gateway.platforms", types.ModuleType("gateway.platforms")
    )
    # Leave api_server missing so import fails.
    sys.modules.pop("gateway.platforms.api_server", None)
    assert tr.install() is False
