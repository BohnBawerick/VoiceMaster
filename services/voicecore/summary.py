"""The per-call summary, written by the Agent that was on the call (ticket 06).

One short paragraph describing what happened, produced at teardown by the SAME Hermes
gateway the call's Agent used for its in-call tool round-trips, and stored on the Call's
own archive document (`call_record.build_metadata`). No second model, no separate
provider path, no global summariser: the Agent's `hermes_profile` chooses the gateway
exactly the way the live call path chooses it, so the summary comes from the Agent that
was actually there.

THREE RULES, ALL LOAD-BEARING
=============================

**1. The call is the product; the summary is a by-product.** Nothing here ever runs on
the call path. The summariser is invoked from inside the DETACHED retain task that
already outlives teardown (`call_record.retain_call`), it is bounded by
``DEFAULT_TIMEOUT_S``, and every failure mode - no gateway, HTTP error, timeout,
garbage body, an exception from anywhere - resolves to "no summary" and lets the
transcript and the recording be retained COMPLETE. `summarise_call` never raises.

**2. Never fabricate.** A summariser handed an empty or near-empty transcript will
cheerfully invent a plausible call. So the model is never asked unless
`is_summarisable` says there is a real two-sided conversation on the wire:

    both parties spoke at least once, the OTHER PARTY said at least
    ``MIN_CALLER_CHARS``, and together they said at least ``MIN_SPOKEN_CHARS``.

An unanswered call, a call hung up on the greeting, a call where nobody said anything -
none of them reach the gateway at all, and none of them get a summary. The caller's own
floor is the one that makes "hung up on the greeting" work in production rather than
only in a fixture; the constants below carry the whole argument, including the version
of this guard that shipped without it and let a model invent "the caller did not speak". Nor is that
absence dressed up: no "the caller did not speak" (which is an inference from silence),
no empty string, no placeholder. The document records ``summary_state`` and no
``summary`` key.

This is also why this module does NOT reuse the services' `call_hermes_agent`. That
helper answers a phone caller, so it returns an apology STRING on failure ("Sorry, that
took too long."). Stored as a summary, that sentence is a fabricated summary of a call
that was never summarised. Here a failure returns None, never prose.

The turn itself goes through ``hermes_gateway.ask_chat``: one listen-only chat
(``tool_choice: none``). Summarising a finished call must not let the Agent fire
skills, including anything that places a call.

**3. Absent must be legible as absent.** ``summary_state`` is a recorded fact about what
this system did, not a guess about the call:

* ``written``               - the Agent produced a summary; ``summary`` holds it.
* ``nothing_to_summarise``  - the guard refused; there was no conversation to describe.
* ``unavailable``           - the Agent was asked and could not answer (no gateway
                              configured, error, timeout, or an empty reply).

A document with no ``summary_state`` at all was written by a lane where summarisation
was switched off, or before this ticket - "nobody asked", which is a fourth thing again.

There is deliberately no "still coming" state. The summary is settled BEFORE the call's
document is written, so a Call is either absent from the archive or complete in it; the
Calls screen never has a half-filled row to spin on. The cost of that choice is honest
and bounded: the archive write waits up to ``DEFAULT_TIMEOUT_S`` for the summariser -
the same order as the retain's own 30s HTTP timeout, and entirely after the call has
already ended.
"""
import asyncio
import logging
import os

from . import hermes_gateway

logger = logging.getLogger("voice.summary")

# The transcript vocabulary all three lanes write (voice/server.py,
# talk-voice-bridge/realtime_bridge.py, voicecore/cascade_live.py).
SPEAKER_AGENT = "AI"
SPEAKER_OTHER = "Them"

# The guard (rule 2), in three parts. One is structural - BOTH parties have to have
# spoken - and two are floors on how much was actually said.
#
# WHY THE CALLER HAS ITS OWN FLOOR, and why a combined floor cannot do this job. The
# first version of this guard required 40 characters ACROSS BOTH SPEAKERS, with a test
# fixture of "AI: Hello, Robot speaking." + "Them: oh" (24 characters) standing in for
# "hung up on the greeting". That fixture was the bug: a greeting a realtime model
# actually produces ("Hi, you've reached Jamie's assistant, how can I help you today?")
# is 65 characters on its own, so the combined floor was already cleared before the
# caller made a sound. In production the guard let a greeting plus "oh" through to the
# model, which answered "The caller did not speak and hung up immediately" - a sentence
# invented from silence, stored as a real summary. That is exactly the failure rule 2
# exists to prevent, and it is also what an STT hallucination of a noise word looks like.
#
# So the load-bearing floor is on THE OTHER PARTY, who is the one that can be silent
# while the Agent fills the line by itself. The Agent needs no floor beyond having
# spoken: a call where the caller asks a real question and the line drops before the
# Agent answers is genuinely summarisable ("they asked X; the call ended before an
# answer"), and refusing it would lose a true summary.
#
# Both numbers are deliberately generous. Refusing to summarise a very short real call
# costs a line on a screen that still has the transcript beside it. Inventing one costs
# the archive its trustworthiness, which is the whole point of ticket 05 and 06.
MIN_CALLER_CHARS = 25
MIN_SPOKEN_CHARS = 40

# How long the detached retain will wait for the Agent before writing the document
# without a summary. Same order as hindsight's own retain timeout.
DEFAULT_TIMEOUT_S = 30.0

# A very long call still has to fit in one request. Cut from the FRONT of the transcript
# (a summary needs how the call ended more than how it opened) and say so, so the model
# is never silently told a partial call is the whole call.
INPUT_MAX_CHARS = 24000
CUT_MARK = "[earlier turns omitted]\n"

ENABLED_ENV = "VOICE_SUMMARY_ENABLED"
TIMEOUT_ENV = "VOICE_SUMMARY_TIMEOUT_S"

STATE_WRITTEN = "written"
STATE_NOTHING = "nothing_to_summarise"
STATE_UNAVAILABLE = "unavailable"

PROMPT = (
    "Summarise the phone call transcribed below in two or three plain sentences: who "
    "wanted what, what was said or done about it, and anything left outstanding. "
    "'Them:' is the other party on the call; 'AI:' is you. Write ONLY the summary - no "
    "preamble, no title, no markdown, no bullet points, no quotation marks. Use ONLY "
    "what the transcript says; if it does not say something, do not write it, and do "
    "not speculate about why anyone said anything.\n\nTRANSCRIPT\n"
)


def _speaker(line: str) -> "tuple[str, str]":
    """``("AI"|"Them"|"", text)`` for one transcript line."""
    text = (line or "").strip()
    for who in (SPEAKER_AGENT, SPEAKER_OTHER):
        prefix = who + ":"
        if text.startswith(prefix):
            return who, text[len(prefix):].strip()
    return "", text


def spoken_chars(transcript) -> dict:
    """How much each speaker actually said, keyed by speaker. Unlabelled lines count
    for nobody: they cannot be attributed, and attributing them is guessing."""
    said = {SPEAKER_AGENT: 0, SPEAKER_OTHER: 0}
    for line in (transcript or []):
        who, text = _speaker(line)
        if text and who:
            said[who] += len(text)
    return said


def is_summarisable(transcript) -> bool:
    """True when this transcript holds a conversation worth describing (rule 2).

    Three conditions, all required (see the constants above for why the caller's own
    floor is the load-bearing one):

    * both parties said something at all;
    * the OTHER PARTY said at least ``MIN_CALLER_CHARS`` - so a real Agent greeting
      answered with "oh", "yeah" or an STT hallucination of a noise word is not a
      conversation, however long the greeting was;
    * between them they said at least ``MIN_SPOKEN_CHARS``.

    Everything else - an unanswered call, a call hung up during or just after the
    greeting, one stray word - is "there is not enough here", and gets NO summary
    rather than a plausible one.
    """
    said = spoken_chars(transcript)
    if not all(said[who] for who in (SPEAKER_AGENT, SPEAKER_OTHER)):
        return False
    if said[SPEAKER_OTHER] < MIN_CALLER_CHARS:
        return False
    return sum(said.values()) >= MIN_SPOKEN_CHARS


def _prompt_for(transcript) -> str:
    body = "\n".join(line for line in (transcript or []) if line)
    if len(body) > INPUT_MAX_CHARS:
        body = CUT_MARK + body[-INPUT_MAX_CHARS:]
    return PROMPT + body


async def summarise_call(transcript, *, gateway_url: "str | None", token: str = "",
                         timeout_s: float = DEFAULT_TIMEOUT_S,
                         ask=None) -> "tuple[str | None, str]":
    """``(summary_or_None, state)`` for one finished call. NEVER raises.

    ``ask`` is the gateway seam (an async ``(prompt) -> str | None``); the suites
    substitute it so no test needs a live Hermes. The default path is
    ``hermes_gateway.ask_chat`` with this module's ``timeout_s`` passed through
    (do not silently take that client's default) and is listen-only.
    """
    if not is_summarisable(transcript):
        return None, STATE_NOTHING
    if not gateway_url:
        # The Agent's hermes_profile has no gateway configured. That is a gap in the
        # record, NOT "nothing happened on this call" - say the honest one.
        logger.warning("call summary: this Agent has no gateway URL - no summary written")
        return None, STATE_UNAVAILABLE
    prompt = _prompt_for(transcript)
    try:
        if ask is None:
            text = await hermes_gateway.ask_chat(
                prompt, gateway_url=gateway_url, token=token,
                timeout_s=timeout_s)
        else:
            text = await ask(prompt)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.warning("call summary: the summariser failed - no summary written",
                       exc_info=True)
        return None, STATE_UNAVAILABLE
    if not isinstance(text, str) or not text.strip():
        if ask is None:
            logger.warning("call summary: the Agent's gateway could not answer (%s)",
                           gateway_url)
        return None, STATE_UNAVAILABLE
    return text.strip(), STATE_WRITTEN


def _flag(value, default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def timeout_from_env(env=None) -> float:
    env = os.environ if env is None else env
    try:
        seconds = float(env.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    return seconds if seconds > 0 else DEFAULT_TIMEOUT_S


def make_summariser(*, gateway_url: "str | None", token: str = "", env=None, ask=None):
    """The summariser one lane hands to ``call_record.retain_call``, or None.

    None means "switched off" (``VOICE_SUMMARY_ENABLED=false``) - the document then
    carries no ``summary_state`` at all, because nobody was asked. A configured-but-
    unreachable Agent is NOT this case: it returns a callable that reports
    ``unavailable``, because there the record has a gap in it.
    """
    env = os.environ if env is None else env
    if not _flag(env.get(ENABLED_ENV), True):
        return None
    timeout_s = timeout_from_env(env)

    async def _summarise(transcript):
        return await summarise_call(transcript, gateway_url=gateway_url, token=token,
                                    timeout_s=timeout_s, ask=ask)

    return _summarise
