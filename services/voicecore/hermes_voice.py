"""The Agent's own Hermes gateway, ON a call (VC24: the direct lane).

``hermes_gateway.ask_chat`` is listen-only by construction: two allowed paths, and a
guard that raises unless the body cuts tools. That guard exists so summarising a
finished call cannot make an Agent act, and it is why this is a SEPARATE module rather
than a flag on that client. Here the Agent is the one holding the conversation, and it
may act: the owner chose full tools for a listed caller (report q5-tools).

One conversation per Call
-------------------------
Every turn carries ``X-Hermes-Session-Id: voice-<call id>``. Hermes loads the history
for that id from its own session store, so this client sends ONE new user message per
turn and never resends the transcript. The id is per Call and shared with no other
channel; long-term memory stays whatever the profile's own memory is (no
``X-Hermes-Session-Key`` is sent, so the call is not fenced off from it).

Why ``/v1/chat/completions`` and not ``/v1/responses``
-----------------------------------------------------
The plan suggested ``/v1/responses`` with a named ``conversation``. Read against the
pinned upstream source (``api_server_openai_routes.py`` at 345cd2b) that endpoint never
reports a failed turn: its terminal event carries no ``hermes`` block, and a turn whose
model chain died completes as ordinary text. ``/v1/chat/completions`` ends its stream
with ``finish_reason: "error"`` and ``hermes.failed: true``. A phone line that reads a
billing error aloud is the failure this client exists to prevent, so it uses the
endpoint that can tell it.

A dead model is never read aloud
--------------------------------
Text is released sentence by sentence so speech starts while Hermes is still writing,
with one rule: the LAST sentence received is held until the stream ends cleanly. A
failed turn's error text arrives in one burst with the failure flag straight behind
it, so it is still being held when the flag lands, and it is dropped. Residual, stated
plainly: a failure that streams several sentences before flagging itself would have
its earlier sentences spoken.

The Agent's tools switch is ``tool_choice``
--------------------------------------------
The merged Hermes server enforces ``tool_choice`` on a chat request. An Agent with
``guardrails.on_call_tools`` off sends ``none`` and Hermes has no tools on the call;
an Agent with it on sends ``auto`` and keeps the profile's full tool set. The field is
built into every body (first turn, later turns, and a resend after a retry) from a
value fixed when the conversation is constructed, and a value that is not one of the
two is refused there. ``tools: []`` is never used: it enforces nothing. The same
vocabulary guards summaries and Missions (``hermes_gateway.assert_listen_only``).

This module never dials, and never speaks: it yields text. ``cascade_live`` owns speech.
"""
import asyncio
import json
import logging
import re

import httpx

from . import cascade_config
from . import hermes_gateway

logger = logging.getLogger("voice.hermes_voice")

CHAT_PATH = hermes_gateway.CHAT_PATH
HEALTH_PATH = "/health"

# How long the pickup check may take before the call is answered on the Realtime lane
# instead (report q6-failure). The caller is already hearing ring-through at this point.
PROBE_BUDGET_S = 1.0
CONNECT_TIMEOUT_S = 5.0
# Upstream writes an SSE keepalive comment every 30 s while an agent turn is busy, so a
# read that stays silent for longer than this means the gateway is gone, not thinking.
STREAM_STALL_S = 45.0

SESSION_PREFIX = "voice-"
_SESSION_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_SESSION_LEN = 200            # upstream caps the header at 256

# A sentence ends at terminal punctuation (plus any closing quote or bracket) followed
# by whitespace, or at a line break.
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+|\n+")
# Shorter pieces ("Sure.", "OK.") ride along with the next sentence: one ElevenLabs
# request per two-word fragment costs more first-byte latency than it saves.
MIN_SENTENCE_CHARS = 24
_MARKUP = re.compile(r"[*_`#]+")


class HermesTurnFailed(RuntimeError):
    """A turn produced nothing that may be spoken.

    ``retryable`` is True only when no part of a response was received, so resending the
    same user message cannot make Hermes act twice.
    """

    def __init__(self, reason: str, *, retryable: bool = False):
        super().__init__(reason)
        self.retryable = retryable


def session_id_for_call(call_id: str) -> str:
    """The Hermes session id for one Call. Upstream interpolates it into file names and
    rejects path-unsafe ids, so anything outside ``[A-Za-z0-9_-]`` is folded to ``-``."""
    cleaned = _SESSION_UNSAFE.sub("-", (call_id or "").strip()).strip("-")
    return (SESSION_PREFIX + (cleaned or "call"))[:_MAX_SESSION_LEN]


def speakable(text: str) -> str:
    """Drop the markup a text-first agent slips into a reply. ElevenLabs reads an
    asterisk as the word, so this runs on every sentence on its way to speech."""
    return re.sub(r"\s+", " ", _MARKUP.sub("", text or "")).strip()


def _visible(raw: str) -> str:
    """``raw`` without reasoning blocks. Prefix-stable as ``raw`` grows: a closed block
    disappears, and an unclosed one hides everything from its opening tag on."""
    text = cascade_config._THINK_BLOCK.sub("", raw)
    idx = text.lower().rfind("<think>")
    return text[:idx] if idx != -1 else text


class SentenceBuffer:
    """Turns a stream of text deltas into speakable sentences, holding the tail."""

    def __init__(self):
        self._raw = ""
        self._released = 0          # chars of the VISIBLE text already handed out

    def feed(self, delta: str) -> list:
        """Sentences that are complete AND followed by more text."""
        self._raw += delta or ""
        visible = _visible(self._raw)
        out, start = [], self._released
        for match in _SENTENCE_END.finditer(visible, start):
            piece = visible[start:match.end()]
            if len(piece.strip()) < MIN_SENTENCE_CHARS:
                continue            # too short to stand alone: extend to the next break
            spoken = speakable(piece)
            if spoken:
                out.append(spoken)
            start = match.end()
        self._released = start
        return out

    def flush(self) -> "str | None":
        """The held tail. Call ONLY once the stream has ended cleanly."""
        tail = speakable(_visible(self._raw)[self._released:])
        self._released = len(_visible(self._raw))
        return tail or None


def _headers(token: str, session_id: str) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream",
               "X-Hermes-Session-Id": session_id}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class HermesConversation:
    """One Call's conversation with one Hermes profile."""

    def __init__(self, *, gateway_url: str, token: str, call_id: str, tool_choice: str,
                 instructions: str = "", model: "str | None" = None, transport=None):
        # Required, and checked here: a conversation that could send no switch would
        # leave the Agent's tools at the upstream default (on) without saying so.
        self._tool_choice = hermes_gateway.check_tool_choice(tool_choice)
        self._url = (gateway_url or "").rstrip("/") + CHAT_PATH
        self._token = token
        self.session_id = session_id_for_call(call_id)
        self._instructions = instructions
        self._model = model
        self._transport = transport
        self.usage = None           # the last turn's usage block, when Hermes sent one
        self.tool_events = 0        # hermes.tool.progress frames seen on the last turn
        # Set on the turn's first hermes.tool.progress frame: the engine's filler waits
        # on it, because a turn that runs a tool is a real wait and one that does not
        # usually answers in 2.5-3.7 s. The engine clears it when it starts a turn.
        self.tool_progress = asyncio.Event()

    def _body(self, text: str) -> dict:
        messages = []
        if self._instructions:
            # Upstream treats the system message as an EPHEMERAL prompt for this turn, so
            # it is sent every time: a profile's SOUL.md carries no spoken-style rules.
            messages.append({"role": "system", "content": self._instructions})
        messages.append({"role": "user", "content": text})
        body = {"messages": messages, "stream": True, "tool_choice": self._tool_choice}
        if self._model:
            body["model"] = self._model       # a Hermes model_routes alias
        return body

    async def stream_turn(self, text: str):
        """Yield speakable sentences for one turn. Raises HermesTurnFailed."""
        self.usage = None
        self.tool_events = 0
        buffer = SentenceBuffer()
        received = finished = False
        event_name = ""
        timeout = httpx.Timeout(STREAM_STALL_S, connect=CONNECT_TIMEOUT_S)
        try:
            async with httpx.AsyncClient(transport=self._transport,
                                         timeout=timeout) as client:
                async with client.stream(
                        "POST", self._url, json=self._body(text),
                        headers=_headers(self._token, self.session_id)) as resp:
                    if resp.status_code == 403:
                        # Upstream refuses X-Hermes-Session-Id unless the gateway has an
                        # API key configured. /health still answers, so the pickup check
                        # passes and every turn then fails: say why, where it will be read.
                        logger.error(
                            "Hermes gateway %s answered 403 to a session turn. Upstream "
                            "requires API_SERVER_KEY on the gateway before it accepts "
                            "X-Hermes-Session-Id; check that profile's gateway has it set",
                            self._url)
                    if resp.status_code != 200:
                        raise HermesTurnFailed(
                            f"HTTP {resp.status_code} from the gateway",
                            retryable=resp.status_code in (429, 502, 503, 504))
                    async for line in resp.aiter_lines():
                        received = True
                        if not line:
                            event_name = ""
                            continue
                        if line.startswith(":"):
                            continue                      # keepalive comment
                        if line.startswith("event:"):
                            event_name = line[6:].strip()
                            continue
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        if event_name == "hermes.tool.progress":
                            self.tool_events += 1
                            self.tool_progress.set()
                            continue
                        done = self._read_chunk(data, buffer)
                        for sentence in done[0]:
                            yield sentence
                        finished = finished or done[1]
        except HermesTurnFailed:
            raise
        except httpx.HTTPError as exc:
            raise HermesTurnFailed(
                f"{type(exc).__name__} talking to the gateway",
                retryable=not received) from exc
        if not finished:
            # The socket closed with no terminal chunk. The held tail is NOT spoken: an
            # unflagged ending is exactly where an error body would otherwise slip out.
            raise HermesTurnFailed("the stream ended without a finish chunk")
        tail = buffer.flush()
        if tail:
            yield tail

    def _read_chunk(self, data: str, buffer: SentenceBuffer) -> "tuple[list, bool]":
        """One SSE data frame -> (sentences ready to speak, stream finished cleanly)."""
        try:
            chunk = json.loads(data)
        except ValueError:
            return [], False
        if not isinstance(chunk, dict):
            return [], False
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        extras = chunk.get("hermes") if isinstance(chunk.get("hermes"), dict) else {}
        choices = chunk.get("choices") or [{}]
        choice = choices[0] if isinstance(choices[0], dict) else {}
        finish = choice.get("finish_reason")
        if extras.get("failed") or finish == "error" or (
                "error" in chunk and not chunk.get("choices")):
            error = chunk.get("error")
            detail = (error.get("message") if isinstance(error, dict) else None) \
                or extras.get("error") or "the agent turn failed"
            # Logged, never spoken: this is where a provider's billing text lands.
            logger.warning("Hermes turn failed on session %s: %s",
                           self.session_id, str(detail)[:300])
            raise HermesTurnFailed("the agent turn failed")
        delta = (choice.get("delta") or {}).get("content")
        sentences = buffer.feed(delta) if isinstance(delta, str) and delta else []
        return sentences, finish is not None


async def probe(gateway_url: "str | None", *, budget_s: float = PROBE_BUDGET_S,
                transport=None) -> bool:
    """Is this profile's gateway answering right now? Never raises.

    ``GET /health`` is unauthenticated upstream and does no agent work, so it costs the
    caller nothing. It proves the process is up, not that its model chain is alive: a
    dead chain is caught per turn by the failure flag instead.
    """
    if not gateway_url:
        return False
    async def ask():
        async with httpx.AsyncClient(transport=transport, timeout=budget_s) as client:
            return await client.get(gateway_url.rstrip("/") + HEALTH_PATH)

    try:
        # httpx's timeout is per operation (connect, then each read), so on its own it
        # bounds nothing in total. The budget is a promise to a ringing phone, so it is
        # held here as one deadline.
        resp = await asyncio.wait_for(ask(), timeout=budget_s)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Hermes gateway %s did not answer the pickup check (%s)",
                       gateway_url, type(exc).__name__)
        return False
    if resp.status_code != 200:
        logger.warning("Hermes gateway %s answered the pickup check with HTTP %s",
                       gateway_url, resp.status_code)
        return False
    return True
