"""Add POST /v1/audio/transcriptions to a Hermes gateway process.

Upstream hermes-agent v0.21.3 at tag v2026.9.14 has no such route. A gateway
without this overlay returns 404 for the path; GET of a
real POST-only route returns 405. This overlay is loaded from the
gateway_overlays directory (HERMES_GATEWAY_OVERLAYS, put on PYTHONPATH by
hermes-supervisor.sh) via sitecustomize before `hermes gateway run`
imports the adapter, and patches APIServerAdapter._http_route_table so
connect() registers the path. Patching at import time rather than editing
api_server.py keeps the hermes-agent checkout untouched, so an upgrade or a
volume mounted over the source does not lose the route.

STT is the Agent's own: tools.transcription_tools.transcribe_audio reads
this process's stt config (the profile that owns this gateway). No new
provider key is introduced. A client `model` field is ignored.

Failures return an OpenAI-style error envelope. A 200 body is only ever
the transcript the backend actually produced.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("hermes_transcription_route")

TRANSCRIPTION_PATH = "/v1/audio/transcriptions"
MAX_AUDIO_BYTES = 25 * 1024 * 1024
SUPPORTED_SUFFIXES = {
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpga",
    ".m4a",
    ".wav",
    ".webm",
    ".ogg",
    ".oga",
    ".opus",
    ".aac",
    ".flac",
    ".caf",
}
DEFAULT_SUFFIX = ".webm"

_installed = False


def error_body(
    message: str,
    err_type: str = "invalid_request_error",
    param: Optional[str] = None,
    code: Optional[str] = None,
) -> Dict[str, Any]:
    """Match gateway.platforms.api_server._openai_error, including redaction."""
    try:
        from gateway.platforms.api_server import _openai_error

        return _openai_error(message, err_type=err_type, param=param, code=code)
    except Exception:
        return {
            "error": {
                "message": str(message),
                "type": err_type,
                "param": param,
                "code": code,
            }
        }


def safe_audio_suffix(filename: Optional[str]) -> str:
    """Keep only a known audio suffix from an upload name. Paths are discarded."""
    if not filename:
        return DEFAULT_SUFFIX
    suffix = Path(Path(filename).name).suffix.lower()
    if suffix in SUPPORTED_SUFFIXES:
        return suffix
    return DEFAULT_SUFFIX


def _status_for_stt_error(error: str) -> int:
    lowered = error.lower()
    if (
        "no stt" in lowered
        or "not available" in lowered
        or "disabled" in lowered
        or "not configured" in lowered
    ):
        return 503
    return 502


def classify_stt_result(result: Any) -> Tuple[str, Dict[str, Any], int]:
    """Turn a transcribe_audio envelope into (kind, body, http_status).

    kind is "ok" only when the backend reported success AND supplied a
    string transcript (empty string is allowed: honest silence). Anything
    else is an error body. A failure never carries a transcript.
    """
    if not isinstance(result, dict):
        return (
            "error",
            error_body(
                "Transcription backend returned an unusable result.",
                err_type="api_error",
                code="stt_invalid_result",
            ),
            502,
        )
    if result.get("success") is not True:
        err = result.get("error") or "Transcription failed."
        return (
            "error",
            error_body(str(err), err_type="api_error", code="stt_failed"),
            _status_for_stt_error(str(err)),
        )
    if "transcript" not in result or result["transcript"] is None:
        return (
            "error",
            error_body(
                "Transcription backend reported success without a transcript.",
                err_type="api_error",
                code="stt_missing_transcript",
            ),
            502,
        )
    text = result["transcript"]
    if not isinstance(text, str):
        return (
            "error",
            error_body(
                "Transcription backend returned a non-text transcript.",
                err_type="api_error",
                code="stt_invalid_transcript",
            ),
            502,
        )
    return "ok", {"text": text}, 200


def routes_already_have_transcription(routes: List[tuple]) -> bool:
    return any(len(row) >= 2 and row[1] == TRANSCRIPTION_PATH for row in routes)


def append_transcription_route(routes: List[tuple], handler: Callable) -> List[tuple]:
    """Return a new table with the transcription route added if missing.

    Existing rows are kept in order and by identity. This is the only
    mutation install() makes to the gateway's route table.
    """
    out = list(routes)
    if not routes_already_have_transcription(out):
        out.append(("POST", TRANSCRIPTION_PATH, handler))
    return out


def install() -> bool:
    """Patch APIServerAdapter._http_route_table. Idempotent."""
    global _installed
    if _installed:
        return True
    try:
        from gateway.platforms.api_server import APIServerAdapter
    except ImportError as exc:
        logger.warning("transcription route not installed: %s", exc)
        return False

    original = APIServerAdapter._http_route_table

    def patched(self):
        return append_transcription_route(original(self), handle_transcriptions)

    APIServerAdapter._http_route_table = patched
    _installed = True
    logger.info("installed POST %s on the gateway route table", TRANSCRIPTION_PATH)
    return True


def reset_install_for_tests() -> None:
    """Test helper. Not used in production."""
    global _installed
    _installed = False


async def handle_transcriptions(request, *, transcribe: Optional[Callable] = None):
    """POST /v1/audio/transcriptions - OpenAI multipart contract, profile STT."""
    from aiohttp import web

    adapter = None
    app = getattr(request, "app", None)
    if app is not None:
        adapter = app.get("api_server_adapter")
    if adapter is None:
        return web.json_response(
            error_body(
                "API server adapter is not available.",
                err_type="api_error",
                code="adapter_unavailable",
            ),
            status=503,
        )

    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err

    content_type = (getattr(request, "content_type", "") or "").lower()
    if not content_type.startswith("multipart/"):
        return web.json_response(
            error_body(
                "Expected multipart/form-data with a 'file' field.",
                param="file",
                code="missing_file",
            ),
            status=400,
        )

    try:
        reader = await request.multipart()
    except Exception as exc:
        logger.warning("transcription multipart parse failed: %s", exc)
        return web.json_response(
            error_body(
                "Could not parse multipart body.",
                param="file",
                code="invalid_multipart",
            ),
            status=400,
        )

    audio_bytes = None
    suffix = DEFAULT_SUFFIX
    while True:
        part = await reader.next()
        if part is None:
            break
        name = part.name or ""
        if name == "file":
            suffix = safe_audio_suffix(getattr(part, "filename", None))
            audio_bytes = await part.read(decode=False)

    if not audio_bytes:
        return web.json_response(
            error_body("Missing audio file.", param="file", code="missing_file"),
            status=400,
        )
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        return web.json_response(
            error_body("Audio file too large.", param="file", code="body_too_large"),
            status=413,
        )

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="hermes-stt-", suffix=suffix)
        try:
            os.write(fd, audio_bytes)
        finally:
            os.close(fd)
        os.chmod(tmp_path, 0o600)

        if transcribe is None:
            from tools.transcription_tools import transcribe_audio

            transcribe = transcribe_audio

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None, lambda: transcribe(tmp_path, source="api_server")
            )
        except Exception as exc:
            logger.warning("transcription backend raised: %s", exc, exc_info=True)
            return web.json_response(
                error_body(
                    "Transcription backend raised an error.",
                    err_type="api_error",
                    code="stt_raised",
                ),
                status=502,
            )

        kind, body, status = classify_stt_result(result)
        if kind != "ok":
            logger.warning(
                "transcription failed: %s",
                (body.get("error") or {}).get("message"),
            )
        else:
            logger.info(
                "transcription ok (%d chars)",
                len(body.get("text") or ""),
            )
        return web.json_response(body, status=status)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
