"""
OCS transport for the Nextcloud Talk <-> Hermes platform plugin.

Pure, gateway-independent layer. It speaks the Nextcloud OCS "spreed" (Talk)
chat API as the ``ai-agent`` user (HTTP Basic app-password auth), long-polls
each room the bot participates in, and posts replies back. It knows NOTHING
about Hermes: it hands each human message to an injected async callback and
exposes a ``send`` coroutine. Keeping the OCS logic here (no ``gateway`` import)
lets the pure helpers be unit-tested without the Hermes runtime installed.

Ported from an earlier stand-alone text bridge. That bridge's stateless
``call_hermes`` / history-rebuild logic is intentionally absent: the native
gateway owns conversation memory.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("nextcloud_talk.transport")

OCS_HEADERS = {"OCS-APIRequest": "true", "Accept": "application/json"}
SPREED = "/ocs/v2.php/apps/spreed/api"
CHUNK_LIMIT = 30000            # Talk hard cap is 32000 - leave headroom
ROOM_TYPE_ONE_TO_ONE = 1


# --- Pure message helpers (no client, no gateway - unit-testable) -----------

def render_message(m: dict) -> str:
    """Resolve Talk's {mention-userN}/rich-object placeholders into readable text."""
    text = m.get("message") or ""
    params = m.get("messageParameters") or {}
    for key, val in params.items():
        placeholder = "{" + key + "}"
        if placeholder not in text or not isinstance(val, dict):
            continue
        if val.get("type") == "user":
            label = "@" + (val.get("id") or val.get("name") or "")
        else:
            label = val.get("name") or val.get("id") or ""
        text = text.replace(placeholder, label)
    return text.strip()


def is_mentioned(m: dict, our_actor_id: str) -> bool:
    """True if the message @-mentions us (rich mention param or plain @text)."""
    params = m.get("messageParameters") or {}
    for val in params.values():
        if isinstance(val, dict) and val.get("type") == "user" and val.get("id") == our_actor_id:
            return True
    text = (m.get("message") or "").lower()
    actor = (our_actor_id or "").lower()
    return bool(actor) and (f"@{actor}" in text or f'@"{actor}"' in text)


def should_respond(room_type: int, m: dict, our_actor_id: str, trigger_mode: str) -> bool:
    """Trigger gate. smart (default): 1:1 answers all; group only on @mention."""
    if trigger_mode == "all":
        return True
    if trigger_mode == "oneToOneOnly":
        return room_type == ROOM_TYPE_ONE_TO_ONE
    if trigger_mode == "mention":
        return is_mentioned(m, our_actor_id)
    return True if room_type == ROOM_TYPE_ONE_TO_ONE else is_mentioned(m, our_actor_id)


def is_human_message(m: dict, our_actor_id: str, sent_refs: set) -> bool:
    """A real, non-system message from someone other than us (primary loop guard)."""
    if m.get("messageType") != "comment":
        return False
    if m.get("systemMessage"):
        return False
    if m.get("actorType") != "users":
        return False
    if m.get("actorId") == our_actor_id:            # never answer our own posts
        return False
    ref = m.get("referenceId")
    if ref and ref in sent_refs:                    # echo-suppression (secondary guard)
        return False
    return bool(render_message(m))


def speaker_tag(actor_id: str, display_name: str, owner_set: set) -> tuple:
    """Return (tag_line, is_owner). Identity is server-derived (actorId), not spoofable."""
    is_owner = bool(actor_id) and actor_id.lower() in owner_set
    who = display_name or actor_id or "unknown"
    if is_owner:
        tag = f"[sender: {who} (id={actor_id}) - OWNER, full trust]"
    else:
        tag = f"[sender: {who} (id={actor_id}) - GUEST, no actions without owner approval]"
    return tag, is_owner


def chunk_text(text: str, chunk_limit: int = CHUNK_LIMIT) -> list:
    """Split a long reply under Talk's char cap on natural boundaries."""
    text = text.strip()
    if len(text) <= chunk_limit:
        return [text] if text else []
    chunks, remaining = [], text
    while len(remaining) > chunk_limit:
        window = remaining[:chunk_limit]
        split = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "))
        if split <= 0:
            split = chunk_limit
        chunks.append(remaining[:split].strip())
        remaining = remaining[split:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


# --- OCS long-poll client ---------------------------------------------------

OnHumanMessage = Callable[[str, int, dict], Awaitable[None]]


class TalkClient:
    """Acts as the Talk user: discovers rooms, long-polls each, posts replies.

    ``on_human_message(token, room_type, message)`` is awaited for every inbound
    message that passes the loop guard and the trigger gate.
    """

    def __init__(
        self,
        base_url: str,
        user: str,
        app_password: str,
        *,
        on_human_message: OnHumanMessage,
        trigger_mode: str = "smart",
        poll_timeout: int = 30,
        discovery_interval: int = 30,
        set_read_marker: str = "1",
        allowlist: Optional[list] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.app_password = app_password
        self.on_human_message = on_human_message
        self.trigger_mode = trigger_mode
        self.poll_timeout = poll_timeout
        self.discovery_interval = discovery_interval
        self.set_read_marker = set_read_marker
        self.allowlist = [t for t in (allowlist or []) if t]

        self.our_actor_id: Optional[str] = None
        self.room_type: dict[str, int] = {}
        self._client: Optional[Any] = None            # httpx.AsyncClient (lazy import)
        self._discovery_task: Optional[asyncio.Task] = None
        self._room_tasks: dict[str, asyncio.Task] = {}
        self._room_last_id: dict[str, int] = {}
        self._sent_refs: set = set()
        self._sent_order: deque = deque(maxlen=500)

    async def start(self) -> str:
        """Open the OCS session, capture our actorId, launch room discovery."""
        import httpx
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            auth=httpx.BasicAuth(self.user, self.app_password),
            headers=OCS_HEADERS,
        )
        self.our_actor_id = await self._resolve_identity()
        self._discovery_task = asyncio.create_task(self._discovery_loop())
        return self.our_actor_id

    async def stop(self) -> None:
        tasks = [self._discovery_task, *self._room_tasks.values()]
        for task in tasks:
            if task:
                task.cancel()
        for task in tasks:
            if task:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._room_tasks.clear()
        if self._client:
            await self._client.aclose()

    def _remember_ref(self, ref: str) -> None:
        """Track a referenceId we posted, bounded, for echo-suppression."""
        if len(self._sent_order) == self._sent_order.maxlen:
            self._sent_refs.discard(self._sent_order[0])
        self._sent_order.append(ref)
        self._sent_refs.add(ref)

    async def _resolve_identity(self) -> str:
        """Capture the exact actorId casing Talk reports for us (loop-guard linchpin)."""
        try:
            r = await self._client.get("/ocs/v2.php/cloud/user", timeout=20.0)
            r.raise_for_status()
            uid = r.json().get("ocs", {}).get("data", {}).get("id")
            if uid:
                return uid
        except Exception as e:                          # noqa: BLE001
            logger.warning("identity resolve failed, using env value '%s': %s", self.user, e)
        return self.user

    async def _discovery_loop(self) -> None:
        while True:
            try:
                r = await self._client.get(f"{SPREED}/v4/room", timeout=20.0)
                r.raise_for_status()
                rooms = r.json().get("ocs", {}).get("data", [])
                current = set()
                for room in rooms:
                    tok = room.get("token")
                    if not tok or (self.allowlist and tok not in self.allowlist):
                        continue
                    current.add(tok)
                    self.room_type[tok] = room.get("type", 2)
                    if tok not in self._room_tasks or self._room_tasks[tok].done():
                        self._room_tasks[tok] = asyncio.create_task(self._room_poll_loop(tok))
                for tok in list(self._room_tasks):      # rooms we left / were removed from
                    if tok not in current:
                        self._room_tasks[tok].cancel()
                        self._room_tasks.pop(tok, None)
                        self._room_last_id.pop(tok, None)
                        self.room_type.pop(tok, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:                      # noqa: BLE001
                logger.warning("room discovery error: %s", e)
            await asyncio.sleep(self.discovery_interval)

    async def _bootstrap_room(self, token: str) -> int:
        """Pin to the room's current tail so a restart never replays backlog."""
        try:
            r = await self._client.get(
                f"{SPREED}/v1/chat/{token}", params={"lookIntoFuture": 0, "limit": 1}, timeout=20.0
            )
            if r.status_code == 200:
                given = r.headers.get("X-Chat-Last-Given")
                if given:
                    return int(given)
                data = r.json().get("ocs", {}).get("data", [])
                if data:
                    return max(int(m["id"]) for m in data)
        except Exception as e:                          # noqa: BLE001
            logger.warning("bootstrap failed for %s: %s", token, e)
        return 0

    async def _room_poll_loop(self, token: str) -> None:
        self._room_last_id[token] = await self._bootstrap_room(token)
        logger.info("watching room %s (type=%s) from id %s",
                    token, self.room_type.get(token), self._room_last_id[token])
        backoff = 1
        while True:
            try:
                r = await self._client.get(
                    f"{SPREED}/v1/chat/{token}",
                    params={
                        "lookIntoFuture": 1,
                        "lastKnownMessageId": self._room_last_id.get(token, 0),
                        "timeout": self.poll_timeout,
                        "setReadMarker": self.set_read_marker,
                        "includeLastKnown": 0,
                    },
                    timeout=self.poll_timeout + 15,
                )
                if r.status_code == 304:                # long-poll expired, nothing new
                    backoff = 1
                    continue
                r.raise_for_status()
                given = r.headers.get("X-Chat-Last-Given")
                messages = r.json().get("ocs", {}).get("data", [])
                if given:
                    self._room_last_id[token] = int(given)
                elif messages:
                    self._room_last_id[token] = max(int(m["id"]) for m in messages)
                backoff = 1
                await self._handle_messages(token, messages)
            except asyncio.CancelledError:
                raise
            except Exception as e:                      # noqa: BLE001
                logger.warning("poll error room %s: %s (retry in %ss)", token, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle_messages(self, token: str, messages: list) -> None:
        rtype = self.room_type.get(token, 2)
        for m in sorted(messages, key=lambda x: int(x["id"])):
            if not is_human_message(m, self.our_actor_id, self._sent_refs):
                continue
            if not should_respond(rtype, m, self.our_actor_id, self.trigger_mode):
                continue
            try:
                await self.on_human_message(token, rtype, m)
            except Exception:                           # noqa: BLE001
                logger.exception("on_human_message failed for room %s", token)

    async def send(self, token: str, text: str, reply_to: Optional[int] = None) -> Optional[str]:
        """Post a reply as the Talk user; returns the last message id. Raises on failure."""
        last_id = None
        for i, chunk in enumerate(chunk_text(text) or ["(no response)"]):
            ref = uuid.uuid4().hex
            self._remember_ref(ref)
            payload = {"message": chunk, "referenceId": ref}
            if reply_to and i == 0:
                payload["replyTo"] = reply_to
            r = await self._client.post(f"{SPREED}/v1/chat/{token}", data=payload, timeout=20.0)
            if r.status_code not in (200, 201):
                raise RuntimeError(f"send to {token} returned {r.status_code}: {r.text[:200]}")
            try:
                last_id = str(r.json().get("ocs", {}).get("data", {}).get("id") or last_id)
            except Exception:                           # noqa: BLE001
                pass
            await asyncio.sleep(0.3)
        return last_id
