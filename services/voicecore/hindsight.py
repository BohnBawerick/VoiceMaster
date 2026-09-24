"""Ship a voice-call transcript into the call archive - Hindsight, or the built-in SQLite store.

Thin inline client (httpx only; no dependency on the heavy hindsight-retainer package). Retains one
document into a Hindsight bank; Hindsight's LLM extraction distils it into durable facts. Best-effort
and fire-and-forget: retain never raises into the call path, and retain_detached runs it as a
background task so a slow/unreachable store can't wedge call teardown.

`retain_detached` is the one seam where a call's archive write is dispatched, whichever store is
active. It asks `call_store.backend` where the document goes: Hindsight when one is configured, the
SQLite file beside the event log otherwise (see `call_store` for the rule).

⚠️ Verified live contract - a `POST /v1/retain` with an in-body `bank_id` is STALE. The bank is a
PATH segment: POST {url}/v1/default/banks/{bank}/memories, with no auth, so keep the store on a
private network. There is no default URL: an unset ``HINDSIGHT_URL`` means "no Hindsight".

s5 (ticket 05): calls live in their OWN bank. ``DEFAULT_BANK`` is `voice`, not the shared `hermes`
gateway-session bank, so the call archive is a clean set holding calls and nothing else. The
dashboard still READS both banks, so nothing retained before the switch is lost.
"""
import asyncio
import logging

import httpx

from . import call_store

logger = logging.getLogger("voice.hindsight")

DEFAULT_URL = ""   # no Hindsight unless HINDSIGHT_URL names one
DEFAULT_BANK = "voice"

# Keep strong refs to in-flight detached retains so they aren't garbage-collected mid-flight.
_PENDING: set = set()


async def retain_result(url: str, bank: str, *, content: str, document_id: str,
                        metadata: dict = None, tags: list = None,
                        timeout: float = 30.0) -> "tuple[bool, str | None]":
    """POST one document to a Hindsight bank. Returns ``(ok, reason)``.

    ``reason`` is None on success and a short human-readable failure description otherwise -
    the retain is fire-and-forget, so the reason is the only thing a caller can put somewhere
    a human will look. Never raises.

    Sends `async: true` so Hindsight extracts server-side and the HTTP call returns promptly
    (extraction is 30-300s synchronously); the timeout still bounds a slow/unreachable server.
    """
    if not content or not content.strip():
        return False, "empty transcript - nothing to retain"
    item = {"content": content, "document_id": document_id}
    if metadata:
        item["metadata"] = {k: str(v) for k, v in metadata.items()}
    if tags:
        item["tags"] = list(tags)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{url.rstrip('/')}/v1/default/banks/{bank}/memories",
                json={"items": [item], "async": True})
            resp.raise_for_status()
        logger.info("Hindsight retain ok (bank=%s, doc=%s, %d chars)", bank, document_id, len(content))
        return True, None
    except Exception as e:  # noqa: BLE001
        reason = str(e).strip() or e.__class__.__name__
        logger.warning("Hindsight retain failed (bank=%s, doc=%s): %s", bank, document_id, reason)
        return False, reason


async def retain(url: str, bank: str, **kwargs) -> bool:
    """``retain_result`` reduced to its boolean. Kept as the plain client contract."""
    ok, _reason = await retain_result(url, bank, **kwargs)
    return ok


def retain_detached(url: str, bank: str, *, on_result=None, prepare=None, **kwargs) -> bool:
    """Fire-and-forget retain that outlives call teardown (kept referenced so it isn't GC'd).

    ``on_result(ok, reason)`` - optional - is invoked when the retain finally settles, which is
    AFTER the call has already been torn down and its call record written. It is how a failed
    write becomes visible instead of being swallowed; a callback that raises is logged and
    never propagated (a broken observer must not be worse than no observer).

    ``prepare`` - optional, ticket 06 - is an async callable awaited on this task BEFORE the
    POST, so that work which must not touch the call path can still land in the document that
    is about to be written (it mutates the ``metadata`` dict it was given). It is the ONE
    extension point for that, deliberately: this stays the single seam where a call's archive
    write is dispatched, so a test that asserts "the retain fired" keeps binding. A ``prepare``
    that raises costs only what it was going to add - the retain happens regardless.

    The write itself goes where `call_store.backend` says: `retain_result` (Hindsight, at
    ``url``/``bank``) or `call_store.write_result` (the SQLite file). Both return the same
    ``(ok, reason)``, and a backend that cannot even be chosen - a bad ``VOICE_ARCHIVE`` -
    settles as a failed write with that sentence, never as an exception.

    Returns True if a task was scheduled (there was a running loop), False otherwise.
    """
    async def _write():
        try:
            chosen = call_store.backend(hindsight_url=url)
        except ValueError as exc:
            logger.warning("call archive not written: %s", exc)
            return False, str(exc)
        if chosen == call_store.SQLITE:
            return await call_store.write_result(
                content=kwargs.get("content"), document_id=kwargs.get("document_id"),
                metadata=kwargs.get("metadata"), tags=kwargs.get("tags"))
        if not (url or "").strip():
            return False, "VOICE_ARCHIVE is 'hindsight' but HINDSIGHT_URL is not set"
        return await retain_result(url, bank, **kwargs)

    async def _run():
        if prepare is not None:
            try:
                await prepare()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("retain prepare failed - retaining without it", exc_info=True)
        ok, reason = await _write()
        if on_result is not None:
            try:
                on_result(ok, reason)
            except Exception:  # noqa: BLE001
                logger.warning("retain on_result callback failed", exc_info=True)
        return ok

    try:
        task = asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        logger.warning("retain_detached called with no running loop - skipping")
        if on_result is not None:
            try:
                on_result(False, "no running event loop - retain was never dispatched")
            except Exception:  # noqa: BLE001
                logger.warning("retain on_result callback failed", exc_info=True)
        return False
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    return True
