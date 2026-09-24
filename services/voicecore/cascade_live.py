"""LIVE cascade lane (s7): streaming STT -> chat-completions LLM or the Agent's own
Hermes profile -> streaming TTS over a Twilio Media Stream (or the Talk lane's Pulse
pipes) — mode-c's second pipeline behind the same call setup (token/mission auth, one
profile snapshot, CallRecorder, teardown) as the realtime lane.

Turn loop
---------
Caller audio feeds the TurnDetector and, echo-gated, the STT (ElevenLabs Scribe or
Deepgram, ``open_stt``). A VAD silence run proposes a turn-end; the engine takes the
utterance and asks the STT provider whether the caller finished: Scribe marks speech
that trailed off ("I just...") and the window is re-armed; Deepgram gives no verdict and
the VAD's stands. The turn record says which (``end_of_turn``).

While a turn is in flight the caller's speech is still heard. If they speak again before
any of the answer was heard and before the turn started a tool, the turn is withdrawn
and answered with everything they said (``_resume_caller_turn``); otherwise the words
become the next turn. Nothing the agent says starts while the caller is talking.

Speech out
----------
One TTS stream per reply: ElevenLabs stream-input (``elevenlabs_live.TTSStream``) takes
each sentence as Hermes writes it and its audio is framed into the wire WHILE it is
still being generated. Audio is paced to real time (the wire holds at most
``PACING_LEAD_S``) and sent in bursts; each burst ends with a mark, and only the mark of
the burst now playing clears ``_playing``. ``ttfb_ms`` is turn-end (verdict accepted) →
first media frame handed to the wire, on this process's monotonic clock.

Barge-in (c5)
-------------
A confirmed barge (sustained, raised-threshold speech while the agent is audible, from
any detector state) cancels the in-flight agent turn, sends ``clear`` (idempotent),
truncates the interrupted assistant message to roughly what was actually heard (played
time × spoken-chars rate), resets the STT's utterance assembly, and replays the
withheld barge head into STT. Speech during the filler barges identically — the filler
is ordinary agent audio.

Tool path (s8 c1/c2, was s7 c6)
-------------------------------
``hermes_agent`` is advertised to the LLM ONLY when the profile carries
``guardrails.on_call_tools: true`` (the outbound sandbox default keeps the schema
empty — not just the prompt); a tool call arriving without that opt-in is denied with
a SPOKEN line, never a null-content hang. The filler watchdog starts AT DISPATCH: if
the backend hasn't answered within ``filler_debounce_s`` a filler line is spoken as a
SEPARATE task, and a filler cut short is cleared off the wire. A caller barge while the tool is in flight is SOFT — it stops the
filler audio but leaves the backend call running and the caller's speech flowing to
STT, so the result is still spoken on return and the interjection becomes the next
turn (s7 shipped the opposite: a barge killed the pending tool → dead air). A hard
``TOOL_BUDGET_S`` cap turns a hung backend into a spoken apology; only teardown/hangup
cancels the dispatch — exactly once, never a duplicate.

Hermes as the LLM stage (VC24, the direct lane)
-----------------------------------------------
When the Agent's llm stage is ``hermes-agent`` the round is not a chat-completions POST
to a vendor: it is one streamed turn of the Agent's OWN Hermes profile
(``hermes_voice.HermesConversation``). Hermes holds the history, so nothing is resent;
it holds the tools, so none are advertised here; and the reply is spoken SENTENCE BY
SENTENCE while Hermes is still writing. The filler speaks only on a real wait (Hermes
reports tool progress, or ``HERMES_FILLER_WAIT_S`` passes with no answer), then "still
checking" every ``TOOL_REASSURE_AFTER_S``, and a spoken apology at ``TOOL_BUDGET_S``. Until
the first sentence of the reply is ready a barge is SOFT, exactly as it is during a tool
call - a cough must not kill an agent turn that may be half way through sending an
email. Once the reply is playing a barge is hard: speech stops, the stream is closed
(upstream interrupts the agent on disconnect), and the next turn tells Hermes how much
of its reply was actually heard, because its own history holds the full text.

Teardown (c8)
-------------
One path: cancel the turn task, close Deepgram, emit EXACTLY ONE ``call`` record and
at most one archive retain (D8: default ON, bank ``hermes``, tag = agent id;
``memory.retain: false`` opts out). Guarded by a flag so hangup-mid-TTS and normal
stop can't double-emit.
"""
import asyncio
import base64
import json
import logging
import time

import httpx

from . import cascade_config
from . import call_record
from . import elevenlabs_live
from . import hermes_voice
from . import hindsight
from . import profiles
from . import recording as call_recording
from .turn_detect import TurnDetector

logger = logging.getLogger("mode-c.cascade")

FRAME_BYTES = 160                 # 20ms of μ-law @ 8k
FRAME_PERIOD_S = FRAME_BYTES / 8000.0   # 20ms of playout per frame
CHARS_PER_SECOND = 14.0           # spoken-rate heuristic for barge-in truncation
# "Caller not finished" verdicts per turn before the VAD's verdict wins. Each costs one
# more silence run, so a wrong one costs at most ~1.2 s.
MAX_EXTENDS = 2
# Nothing the agent says starts while the caller is talking: it waits, up to this long.
HOLD_FOR_CALLER_MAX_S = 5.0
HOLD_POLL_S = 0.02
# A burst of agent audio ends (and its mark is sent) when the provider has sent nothing
# for this long past the moment the wire runs out of audio.
BURST_GAP_S = 0.25
# The direct lane's filler waits for a real wait: tool progress from Hermes, or this.
# Hermes's first sentence usually lands at 2.5-3.7 s (report 2026-09-23, 1.2).
HERMES_FILLER_WAIT_S = 4.5
LLM_MAX_TOKENS = 512
STAGE_TIMEOUT_S = 60.0
# Twilio jitter buffer: pace outbound TTS frames so Twilio holds at most ~this much
# un-played audio. Without pacing a multi-second reply floods Twilio's buffer in
# ~700ms, so a barge 'clear' fought seconds of already-buffered speech ("won't shut
# up"). Keeping the buffer shallow means a clear stops playback within ~this window.
PACING_LEAD_S = 0.30
# s16 c1: ONE declared budget for a hermes_agent round-trip, measured wall-clock from
# dispatch to spoken recovery. Before s16 this was `debounce THEN 45s`, two timers in
# series: asyncio.wait(debounce) burned first and only then did wait_for(45) start its
# clock, so the real ceiling was debounce + 45 = 47.0s. That is the exact figure s14b's
# two failed tool turns logged, twice, identically. The debounce is now a filler-start
# deadline INSIDE this budget, never runway added on top of it.
#
# 60s (up from the effective 47) on the Hermes oracle's own numbers: a calendar lookup
# costs it ~1.5s of Google API and tens of seconds of agent-loop tax (skills_list ->
# skill_view -> tool spawn -> several model round-trips), a heavy-tailed distribution
# whose p95 plausibly exceeds 45s. Raising the cap is only defensible BECAUSE c2 makes
# the wait audible — a longer cap with silent filler is just longer dead air.
TOOL_BUDGET_S = 60.0
TOOL_REASSURE_AFTER_S = 10.0    # "still working" cadence — 16s is a long silence on a phone
TOOL_TIMEOUT_REPLY = (          # spoken when the backend blows the cap — never silence
    "I couldn't pull that up in time, so I'll follow up on it separately. Anyway,")
TOOL_DENIED_REPLY = (           # spoken if the model calls a tool we didn't advertise
    "Sorry, I'm not able to do that on this call.")
FILLER_REASSURE_TEXT = "Still checking on that, bear with me a moment."

LIVE_BASE_PROMPT = (
    "You are on a live phone call, speaking out loud. CRITICAL: keep EVERY turn to ONE "
    "or at most TWO short spoken sentences — this is a phone call, not a monologue. "
    "Never lecture or over-explain; make the single key point and let the caller "
    "respond. Natural speech, contractions welcome. Never output markdown, lists, code, "
    "or formatting — speak plainly. When the caller asks you to DO something beyond "
    "talking — look up, check, record, log, note, send, or schedule anything — you MUST "
    "call the hermes_agent tool to actually do it: briefly say you're on it, then relay "
    "the result in one sentence. Never claim you did something unless the tool did it.")

# VC24: what travels with EVERY turn on the direct lane. A profile's SOUL.md says who the
# Agent is; it says nothing about being heard instead of read, so the spoken-style rules
# ride along as the turn's ephemeral system prompt. Deliberately no "confirm before
# acting" line: the owner chose full tools for a listed caller (q5-tools).
HERMES_VOICE_RULES = (
    "You are on a live voice call. Everything you write is spoken aloud by a "
    "text-to-speech voice, so write for the ear. Reply in one to three short spoken "
    "sentences and let the other person answer. Plain words only: no markdown, lists, "
    "code, emoji, links or headings. Say numbers, dates and times the way a person says "
    "them. You have your normal tools, skills and memory on this call; when the caller "
    "asks for something, do it, then say the result in a sentence instead of reading raw "
    "output. If something will take a while, say so in a few words first. Never claim "
    "you did something unless you did it.")
# The same rules for an Agent whose tools setting is off: the request cuts the tools
# (``tool_choice: none``), so the prompt must not promise any.
HERMES_VOICE_RULES_NO_TOOLS = (
    "You are on a live voice call. Everything you write is spoken aloud by a "
    "text-to-speech voice, so write for the ear. Reply in one to three short spoken "
    "sentences and let the other person answer. Plain words only: no markdown, lists, "
    "code, emoji, links or headings. Say numbers, dates and times the way a person says "
    "them. You have no tools on this call: you can talk, but you cannot look anything up "
    "or do anything. If the caller asks for something that needs a tool, say plainly "
    "that you cannot do that on this call. Never claim you did something unless you "
    "did it.")
HERMES_OPENER_INBOUND = (
    "(The call just connected - you are answering it. Greet the caller warmly and "
    "briefly, in one sentence.)")
HERMES_OPENER_FILLER = "Hi, give me just a second."
HERMES_LOST_TEXT = "I lost my connection for a moment, bear with me."
HERMES_FAILED_REPLY = "Sorry, I lost my train of thought there. Could you say that again?"
HERMES_TIMEOUT_REPLY = (
    "Sorry, that is taking too long on my end. Give me a moment and ask me again.")
HERMES_RETRY_BACKOFF_S = (1.0, 2.0, 4.0, 8.0)
_EOS = object()


def base_prompt_for(doc) -> str:
    """The lane's base prompt for this Agent: the spoken-style rules alone when Hermes is
    the llm stage (worded for the Agent's tools setting), else the vendor-LLM prompt that
    tells the model about hermes_agent."""
    if not profiles.is_hermes_direct(doc):
        return LIVE_BASE_PROMPT
    return (HERMES_VOICE_RULES if profiles.on_call_tools_of(doc)
            else HERMES_VOICE_RULES_NO_TOOLS)


def hermes_conversation_for(config: dict, *, call_id: str, token: str,
                            mission_brief: str = "", caller: str = "",
                            transport=None) -> "hermes_voice.HermesConversation | None":
    """The on-call Hermes client for a built cascade config, or None when the llm stage
    is an outside vendor. ONE construction for both bridges, so the phone and Talk
    Outlets cannot drift in what they tell Hermes about a call.

    Raises ValueError when the Agent's hermes_profile is not routable: the construction
    site refuses the call (or, at pickup, answers on the Realtime lane) instead of
    routing the conversation to another Agent's backend."""
    llm = config.get("llm") or {}
    if llm.get("kind") != "hermes":
        return None
    if not llm.get("endpoint"):
        raise ValueError(
            f"hermes_profile '{llm.get('hermes_profile')}' is not routable (not in "
            "HERMES_PROFILE_GATEWAY_URLS, not ok in gateways.json)")
    instructions = llm["system_prompt"]
    if caller:
        instructions += f"\n\nYou are speaking with {caller}."
    if mission_brief:
        instructions += "\n\n== YOUR MISSION FOR THIS CALL ==\n" + mission_brief
    return hermes_voice.HermesConversation(
        gateway_url=llm["endpoint"], token=token, call_id=call_id,
        tool_choice=llm.get("tool_choice"),
        instructions=instructions, model=llm.get("model"), transport=transport)


def open_stt(config: dict, env: dict,
             audio_format: "cascade_config.AudioFormat | None" = None):
    """The live STT client for a built cascade config. ONE construction for both
    bridges: ElevenLabs Scribe or Deepgram, fed the wire's own format."""
    fmt = audio_format or cascade_config.MULAW_8K
    stt = config["stt"]
    key = (env.get(stt.get("secret_env") or "") or "").strip()
    if stt["provider"] == cascade_config.STT_ELEVENLABS:
        return elevenlabs_live.ScribeLive(
            key, model=stt.get("model") or elevenlabs_live.STT_MODEL,
            language=stt.get("language"), keyterms=stt.get("keyterms"),
            wire_format=fmt.name)
    from . import deepgram_live     # needs websockets, which the dashboard does not install
    return deepgram_live.DeepgramLive(
        key, model=stt.get("model") or deepgram_live.DEFAULT_MODEL,
        language=stt.get("language"), keyterms=stt.get("keyterms"),
        encoding=fmt.deepgram_encoding, sample_rate=fmt.sample_rate,
        smart_format=stt.get("smart_format"), numerals=stt.get("numerals"))


# hermes_agent in chat-completions tool shape — the canonical schema now lives in
# cascade_config (shared with the s10 eval harness so the lane that RUNS the tool and the
# harness that GRADES its detection can never drift). The realtime lane's server.TOOLS is
# the flat Realtime-API shape (same name/contract, different envelope) and stays separate.
CHAT_TOOLS = cascade_config.CHAT_TOOLS


# ------------------------------------------------------------------ wire seam --
# s12b: the turn engine below is transport-agnostic. It talks to the outside world
# through a ``wire`` with three egress verbs (emit_frame / emit_mark / emit_clear) and
# one ingress async iterator (events() → ("media", pcm) | ("mark", seq) | ("stop",
# None)). The mode-c default is a Twilio media-stream WebSocket; the Talk lane (s12b)
# supplies a PacatWire in talk-voice-bridge/cascade_bridge.py. The egress JSON here is
# byte-identical to pre-s12b (goldens unchanged): the seam is a hoist, not a rewrite.
def _mark_seq_of(name) -> "int | None":
    """``utt-7`` -> 7; anything else -> None (a mark this engine did not send)."""
    if isinstance(name, str) and name.startswith("utt-"):
        try:
            return int(name[4:])
        except ValueError:
            return None
    return None


class TwilioWire:
    """The mode-c wire skin: a Twilio media-stream WebSocket. ``emit_*`` write the exact
    Twilio JSON envelopes the engine used inline pre-s12b; ``events()`` decodes the
    inbound stream. A real Twilio ``mark`` echo (remote finished playing) surfaces as a
    ("mark", seq) event carrying the burst number from the mark's name; the engine ends
    playback only for the burst that is playing now."""

    def __init__(self, ws, stream_sid: str):
        self._ws = ws
        self._sid = stream_sid

    async def events(self):
        async for message in self._ws.iter_text():
            data = json.loads(message)
            event = data.get("event")
            if event == "media":
                yield ("media", base64.b64decode(data["media"]["payload"]))
            elif event == "mark":
                yield ("mark", _mark_seq_of((data.get("mark") or {}).get("name")))
            elif event == "stop":
                yield ("stop", None)
                return

    async def emit_frame(self, frame: bytes) -> None:
        await self._ws.send_json({
            "event": "media", "streamSid": self._sid,
            "media": {"payload": base64.b64encode(frame).decode()}})

    async def emit_mark(self, seq: int) -> None:
        await self._ws.send_json({"event": "mark", "streamSid": self._sid,
                                  "mark": {"name": f"utt-{seq}"}})

    async def emit_clear(self) -> None:
        await self._ws.send_json({"event": "clear", "streamSid": self._sid})

    async def aclose(self) -> None:
        """Teardown hook (parity with PacatWire, which owns parec/pacat). No-op: the
        Twilio WS lifecycle is owned by the FastAPI endpoint, not the engine."""
        return None


class CascadeLiveSession:
    """One live cascade call. Dependencies are injectable for the unit suites; the
    defaults dial the real vendors."""

    def __init__(self, *, twilio_ws, stream_sid: str, config: dict, profile,
                 recorder, env: dict, mission_brief: str = "",
                 stt, hermes_call=None, tools_enabled: bool = False,
                 filler_text: str = "One sec, let me check that.",
                 filler_debounce_s: float = 2.0,
                 detector: "TurnDetector | None" = None,
                 transport=None, tts_connect=None, clock=time.monotonic,
                 retain_default: bool = True, hindsight_url: str = "",
                 hindsight_bank: str = hindsight.DEFAULT_BANK,
                 audio_format: "cascade_config.AudioFormat | None" = None,
                 wire=None, recording=None, summariser=None,
                 hermes_conversation=None, direction: str = "outbound"):
        # s12b: the wire is the transport seam. mode-c passes twilio_ws/stream_sid and
        # gets the default TwilioWire (byte-identical to pre-s12b); the Talk lane passes
        # an explicit wire (PacatWire) and leaves twilio_ws/stream_sid None.
        self._wire = wire or TwilioWire(twilio_ws, stream_sid)
        self._config = config
        # Transport audio format (s12): mode-c defaults to μ-law/8k so the frame size,
        # TTS output_format, pacing period and truncation math are all byte-identical to
        # pre-s12. The Talk lane (s12b) passes PCM_24K.
        self._fmt = audio_format or cascade_config.MULAW_8K
        self._profile = profile
        self._recorder = recorder
        self._env = env
        # The STT client: DeepgramLive or ScribeLive (``open_stt``). Same surface; Scribe
        # also says whether the caller finished (``turn_verdict``).
        self._stt = stt
        self._hermes_call = hermes_call
        self._tools_enabled = tools_enabled and hermes_call is not None
        self._filler_text = filler_text
        self._filler_debounce_s = filler_debounce_s
        self._detector = detector or TurnDetector()
        self._transport = transport
        # ElevenLabs speech goes out over one websocket per reply; injectable like the
        # HTTP transport, so units drive a fake socket.
        self._tts_connect = tts_connect
        self._clock = clock
        self._retain_default = retain_default
        self._hindsight_url = hindsight_url
        self._hindsight_bank = hindsight_bank
        # s5: the brief is kept so the retained call record can say what the call was FOR.
        # Inbound has none, and records none.
        self._mission_brief = mission_brief or None
        # Ticket 07: both legs of this call, teed into a stereo Opus file. The default is
        # a no-op object, never None, so the media path below has ONE shape and a
        # construction site that does not care about recording changes nothing.
        self._recording = recording or call_recording.NullRecording()
        # Ticket 06: the post-call summariser for THIS call's Agent, injected by the
        # construction site exactly like `hermes_call` is, because the two services
        # resolve an Agent's gateway with their own (identical) helpers. None = off.
        self._summariser = summariser
        # VC24: set when the llm stage IS a Hermes profile. Injected by the construction
        # site like `hermes_call`, because resolving a gateway is the bridge's business.
        self._hermes = hermes_conversation
        self._hermes_task: "asyncio.Task | None" = None
        self._hermes_heard: "str | None" = None   # what a hard barge let the caller hear
        self._hermes_merged = False               # this turn's input merges a cut turn
        self._direction = direction

        system = config["llm"]["system_prompt"]
        if mission_brief:
            system += ("\n\n== YOUR MISSION FOR THIS CALL ==\n" + mission_brief)
        self.messages: list = [{"role": "system", "content": system}]
        self.transcript: list = []

        # Speaking state. ``_playing`` means "the other party can hear the agent now".
        # It is set when a burst of agent audio starts and cleared ONLY by the mark echo
        # of that same burst: a stale mark from an earlier burst cannot switch it off
        # (report 2026-09-23, 2.3 mechanism 1 - the one that disabled barge-in for every
        # sentence after the first).
        self._playing = False
        self._mark_seq = 0                  # the newest burst's mark number
        self._burst_open = False            # frames are flowing for burst _mark_seq
        self._speak_text = ""               # the text of the reply being spoken
        self._current_is_filler = False     # s12c: gates on_answer_audio (filler ≠ answer)
        self._first_frame_mono = None
        self._frames_sent = 0
        self._playout_end = 0.0             # when the audio handed to the wire runs out
        self._burst_sent_ms = 0.0
        self._stream_sent_ms = 0.0
        self._heard_frozen_ms: "float | None" = None   # set while a barge is handled
        self._frame_buf = b""

        self._turn_task: "asyncio.Task | None" = None
        self._tool_task: "asyncio.Task | None" = None
        self._filler_task: "asyncio.Task | None" = None
        self._resume_task: "asyncio.Task | None" = None
        # A caller utterance can finish (vad_turn_end) WHILE a turn is busy. We never
        # race a second turn against the live one (c1): either the new words are merged
        # into it (``_resume_caller_turn``) or the turn-end is remembered and drained the
        # moment the current turn frees up, so the utterance becomes the NEXT turn.
        self._pending_turn_end = False
        self._deciding = False              # an end-of-turn verdict is being taken
        self._extends = 0
        self._utterance_parts: list = []    # text taken so far for the turn being decided
        self._turn_user_text: "str | None" = None   # what the in-flight turn answers
        self._turn_spoke = False            # the in-flight turn's answer became audible
        self._turn_acted = False            # the in-flight turn started a tool
        self._turn_extra: dict = {}
        self._finished = False
        self.outcome = "ok"
        # VC24: when the other party last said something this engine understood. The
        # Talk bridge's inbound idle backstop reads it; the phone has Twilio's `stop`.
        self.last_caller_speech = self._clock()

    # ------------------------------------------------------------------ run --

    async def run(self) -> None:
        """Consume the media stream until stop/disconnect. The caller (server.py / the
        Talk CascadeBridge) wraps this in its own try/finally and calls ``teardown``
        exactly once. Transport-agnostic: events come from the wire (Twilio WS in mode-c,
        parec+synthesized marks in the Talk lane)."""
        if hasattr(self._stt, "on_event"):
            self._stt.on_event = self._on_stt_event
        await self._stt.start()
        self._turn_task = asyncio.create_task(self._agent_turn(opener=True),
                                              name="cascade-turn")
        async for kind, payload in self._wire.events():
            if kind == "media":
                await self._on_media(payload)
            elif kind == "mark":
                self._on_mark(payload)
            elif kind == "stop":
                logger.info("media stream stopped (cascade)")
                break

    @property
    def stt_lost(self) -> bool:
        """The STT session died and could not be brought back: this call went deaf, so
        it must not become the Outlet's last-known-good."""
        return bool(getattr(self._stt, "lost", False))

    def _on_stt_event(self, record: dict) -> None:
        """A reconnect or a lost STT session, into the event log where it will be read."""
        record = dict(record, provider=self._config["stt"].get("provider"))
        logger.warning("STT %s on %s: %s", record.get("event"),
                       getattr(self._recorder, "call_id", "?"), record.get("reason"))
        report = getattr(self._recorder, "record_stt", None)
        if report is not None:
            report(**record)

    def _on_mark(self, seq) -> None:
        """Playback finished for the marked burst (Twilio's mark echo, or the Talk wire's
        synthesized drain-done). Only the mark of the burst now playing ends playback;
        Twilio echoes each mark when the audio before it has played, which is after the
        NEXT burst may already have started."""
        if seq == self._mark_seq and not self._burst_open:
            self._playing = False

    async def _on_media(self, raw: bytes) -> None:
        # Ticket 07: the caller's leg, exactly as it arrived off the wire. Enqueue only
        # — the decode and the disk happen on the recorder's own thread.
        self._recording.caller_audio(raw)
        was_idle = self._detector.state == "idle"
        verdict = self._detector.feed(raw, agent_playing=self._playing)
        if "barge_in" in verdict.events:
            soft = await self._handle_barge_in()
            if not was_idle:
                # The caller was already talking (their words are in the STT, maybe a
                # queued turn too): the queued words and the new ones are ONE utterance,
                # decided when they stop. Draining the queued turn now would end their
                # turn mid-sentence and let the agent start over them.
                self._pending_turn_end = False
            if not soft and was_idle:
                # Hard barge from idle (a normal reply was cut off): the STT only held
                # echo-gated silence, so drop its stale partials. A SOFT barge protects an
                # in-flight tool, and a barge from speech/pending holds the caller's own
                # words - neither resets the STT (c1/c2, ticket 21).
                self._stt.reset()
            if verdict.replay:
                await self._stt.feed(verdict.replay)
        if verdict.forward_to_stt:
            await self._stt.feed(raw)
        if "vad_turn_end" in verdict.events:
            if self._turn_idle():
                self._turn_task = asyncio.create_task(self._finish_caller_turn(),
                                                      name="cascade-turn")
            elif self._resume_task is None and self._mergeable():
                # The caller kept talking before the answer started: decide now, and
                # merge the new words into the turn in flight instead of queuing them.
                self._resume_task = asyncio.create_task(self._resume_caller_turn(),
                                                        name="cascade-resume")
            else:
                self._pending_turn_end = True     # a turn is busy — queue, never race
        elif self._pending_turn_end and self._turn_idle():
            # The busy turn just finished; process the utterance that landed during it.
            self._pending_turn_end = False
            self._turn_task = asyncio.create_task(self._finish_caller_turn(),
                                                  name="cascade-turn")

    def _turn_idle(self) -> bool:
        return self._turn_task is None or self._turn_task.done()

    def _mergeable(self) -> bool:
        """Can the in-flight turn still be withdrawn and answered with the caller's full
        words? Only while it answers a caller utterance (never the opener), before any of
        its answer was heard, and before it started a tool: cancelling a turn that has
        acted could leave an action half done (report 5.1 item 6)."""
        if self._turn_idle() or self._turn_user_text is None:
            return False
        if self._turn_spoke or self._turn_acted:
            return False
        return not (self._hermes is not None and self._hermes.tool_events)

    # ---------------------------------------------------------- turn taking --

    async def _decide_turn(self) -> "str | None":
        """A VAD turn-end proposal: take the utterance and ask the STT provider whether
        the caller finished. Returns the whole utterance when the turn ended ("" when
        nothing was said), or None when the listening window was re-armed."""
        t0 = self._clock()
        if self._extends and not self._detector.spoke_since_extend:
            text = ""                         # silent through the extension: accept
        else:
            text = await self._stt.take_utterance()
        stt_flush_ms = round((self._clock() - t0) * 1000.0, 1)
        if text:
            self._utterance_parts.append(text)
        judge = getattr(self._stt, "turn_verdict", None)
        verdict = judge(text) if (judge is not None and text) else None
        if verdict == "incomplete" and self._extends < MAX_EXTENDS:
            self._extends += 1
            self._detector.extend()
            return None
        self._turn_extra = {
            "end_of_turn": {"source": "stt" if judge is not None else "vad",
                            "verdict": verdict, "extends": self._extends},
            "stage_ms": {"stt_flush": stt_flush_ms}}
        self._detector.turn_ended()
        self._extends = 0
        full = " ".join(self._utterance_parts).strip()
        self._utterance_parts = []
        return full

    def _accept_caller_text(self, text: str) -> None:
        self.last_caller_speech = self._clock()
        self.transcript.append(f"Them: {text}")
        self.messages.append({"role": "user", "content": text})

    async def _finish_caller_turn(self) -> None:
        """A VAD turn-end proposal with no turn in flight: decide, then answer."""
        self._deciding = True
        try:
            turn_end = self._clock()
            text = await self._decide_turn()
        finally:
            self._deciding = False
        if not text:
            return                            # re-armed, or noise/echo: nothing to answer
        # ttfb clock (c8): turn-end verdict → first media frame.
        self._recorder.on_speech_stopped(turn_end)
        self._accept_caller_text(text)
        await self._agent_turn()

    async def _resume_caller_turn(self) -> None:
        """The caller spoke again while the answer to their last words was still being
        worked out. Decide whether they finished, then withdraw that turn and answer
        both utterances as one (Deepgram Flux's TurnResumed idea). The answer is held
        while this runs (``_caller_active``), so nothing is said over the decision."""
        self._deciding = True
        try:
            turn_end = self._clock()
            text = await self._decide_turn()
            if not text:
                return                        # re-armed, or noise: the turn carries on
            if self._turn_idle():
                self._recorder.on_speech_stopped(turn_end)
                self._accept_caller_text(text)
                self._turn_task = asyncio.create_task(self._agent_turn(),
                                                      name="cascade-turn")
                return
            if not self._mergeable():
                # It started speaking or acting while we listened: the new words become
                # the next turn, exactly as a queued utterance does.
                self._utterance_parts = [text]
                self._pending_turn_end = True
                return
            earlier = self._turn_user_text or ""
            self._turn_task.cancel()
            try:
                await self._turn_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            combined = f"{earlier} {text}".strip()
            self._replace_last_user_text(earlier, combined)
            self._hermes_merged = True
            self._turn_extra = dict(self._turn_extra, merged=True)
            self._recorder.on_speech_stopped(turn_end)
            logger.info("caller resumed before the answer: merged into one turn")
            self._turn_task = asyncio.create_task(self._agent_turn(),
                                                  name="cascade-turn")
        finally:
            self._deciding = False
            self._resume_task = None

    def _replace_last_user_text(self, earlier: str, combined: str) -> None:
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user" and \
                    self.messages[i].get("content") == earlier:
                self.messages[i]["content"] = combined
                break
        for i in range(len(self.transcript) - 1, -1, -1):
            if self.transcript[i] == f"Them: {earlier}":
                self.transcript[i] = f"Them: {combined}"
                break
        self.last_caller_speech = self._clock()

    def _caller_active(self) -> bool:
        """Is the caller mid-utterance, or is their turn being decided? Nothing the agent
        says may start then (report 5.1 item 3)."""
        return self._detector.state == "speech" or self._deciding

    async def _hold_for_caller(self) -> None:
        """Wait, up to HOLD_FOR_CALLER_MAX_S, while the caller is talking."""
        waited = 0.0
        while self._caller_active() and waited < HOLD_FOR_CALLER_MAX_S:
            await asyncio.sleep(HOLD_POLL_S)
            waited += HOLD_POLL_S

    def _last_user_text(self) -> "str | None":
        for message in reversed(self.messages):
            if message.get("role") == "user":
                return message.get("content")
        return None

    async def _agent_turn(self, opener: bool = False) -> None:
        self._turn_user_text = None if opener else self._last_user_text()
        self._turn_spoke = False
        self._turn_acted = False
        try:
            if opener:
                self._recorder.on_speech_stopped(self._clock())
                self._turn_extra = {"end_of_turn": None, "stage_ms": {}}
                self.messages.append({
                    "role": "user",
                    "content": (HERMES_OPENER_INBOUND if self._direction == "inbound"
                                else "(The call just connected - the other party has "
                                     "answered. Open the conversation now per your "
                                     "instructions, briefly.)")})
            if self._hermes is not None:
                usage = await self._hermes_round(opener=opener)
                self._recorder.on_response_done(usage=usage, extra=self._turn_extra)
                return
            reply, usage = await self._llm_round()
            if reply:
                self.messages.append({"role": "assistant", "content": reply})
                self.transcript.append(f"AI: {reply}")
                t0 = self._clock()
                await self._speak(reply)
                self._turn_extra.setdefault("stage_ms", {})["tts_stream"] = round(
                    (self._clock() - t0) * 1000.0, 1)
            self._recorder.on_response_done(usage=usage, extra=self._turn_extra)
        except asyncio.CancelledError:
            raise                             # barge-in/merge/teardown — handled there
        except Exception:  # noqa: BLE001
            logger.exception("Cascade agent turn failed - apologising and continuing")
            try:
                await self._speak("Sorry, something went wrong on my end.")
            except Exception:  # noqa: BLE001
                pass
            self._recorder.on_response_done(usage=None, extra=self._turn_extra)

    # --------------------------------------------------------------- Hermes --

    def _hermes_input(self) -> str:
        """This turn's one user message. Hermes holds the history, so the only extra it
        needs is what ITS history cannot know: how much of its last reply was heard, and
        that the turn before this one was withdrawn because the caller kept talking."""
        text = self._last_user_text() or ""
        if self._hermes_merged:
            self._hermes_merged = False
            text = ("(You were cut off before answering: the caller had not finished. "
                    f"Everything they said:)\n{text}")
        heard, self._hermes_heard = self._hermes_heard, None
        if heard is None:
            return text
        return ("(The caller interrupted your last reply. They heard only this much of "
                f"it: \"{heard}\")\n{text}")

    async def _hermes_produce(self, text: str, queue: asyncio.Queue,
                              deadline: float) -> None:
        """Stream one Hermes turn into ``queue`` as sentences, then ``_EOS``. A failure is
        queued, never raised: the consumer owns what the caller hears about it.

        Retries ONLY a failure that arrived before any byte of a response, so a resend
        cannot make Hermes act twice, and only while the turn's one budget has room."""
        attempt = 0
        while True:
            try:
                async for sentence in self._hermes.stream_turn(text):
                    await queue.put(sentence)
                await queue.put(_EOS)
                return
            except hermes_voice.HermesTurnFailed as exc:
                backoff = HERMES_RETRY_BACKOFF_S[min(attempt, len(HERMES_RETRY_BACKOFF_S) - 1)]
                if not exc.retryable or self._clock() + backoff >= deadline:
                    await queue.put(exc)
                    return
                logger.warning("Hermes turn failed before answering (%s) - retrying in "
                               "%.0fs", exc, backoff)
                if attempt == 0:
                    await queue.put(("lost", None))
                attempt += 1
                await asyncio.sleep(backoff)

    async def _hermes_filler(self, opener: bool) -> None:
        """The filler on the direct lane speaks only on a REAL wait: once Hermes reports
        tool progress, or after HERMES_FILLER_WAIT_S with no answer. Hermes's first
        sentence usually lands at 2.5-3.7 s, so the old fixed 2 s filler was cut off by
        the answer on almost every turn ("One sec, let me-", report 2.3 mechanism 5)."""
        try:
            await asyncio.wait_for(self._hermes.tool_progress.wait(),
                                   timeout=HERMES_FILLER_WAIT_S)
        except asyncio.TimeoutError:
            pass
        await self._filler_loop(opening_line=HERMES_OPENER_FILLER if opener else None)

    async def _reply_sentences(self, queue: asyncio.Queue, deadline: float,
                               started: float, stage: dict):
        """The reply, sentence by sentence. Until the first sentence the turn's budget
        applies and a retry is announced; the first sentence ends the filler and makes a
        barge HARD. Raises what the producer queued."""
        first = True
        while True:
            if first:
                item = await asyncio.wait_for(
                    queue.get(), timeout=max(0.0, deadline - self._clock()))
            else:
                item = await queue.get()          # the client's own stall timeout bounds it
            if item is _EOS:
                if first:
                    # A clean ending with nothing to say. Silence is the one thing a
                    # caller cannot interpret, so it is handled like any failed turn.
                    raise hermes_voice.HermesTurnFailed("the turn produced no text")
                return
            if isinstance(item, Exception):
                raise item
            if isinstance(item, tuple):           # ("lost", None): a retry is under way
                await self._cancel_filler()
                self._filler_task = asyncio.create_task(
                    self._filler_loop(opening_line=HERMES_LOST_TEXT),
                    name="cascade-filler-lost")
                continue
            if first:
                first = False
                self._tool_task = None            # the reply is starting: a barge is HARD
                await self._cancel_filler()
                stage["llm_first_sentence"] = round((self._clock() - started) * 1000.0, 1)
            yield item

    async def _hermes_round(self, *, opener: bool = False) -> "dict | None":
        """One turn of the Agent's own Hermes profile, spoken as it is written, over ONE
        TTS stream that is opened while Hermes is still thinking."""
        started = self._clock()
        deadline = started + TOOL_BUDGET_S
        stage = self._turn_extra.setdefault("stage_ms", {})
        queue: asyncio.Queue = asyncio.Queue()
        self._hermes.tool_progress.clear()
        producer = asyncio.create_task(
            self._hermes_produce(self._hermes_input(), queue, deadline),
            name="cascade-hermes")
        self._hermes_task = producer
        # Until the reply starts, this turn is protected like a tool call: a barge stops
        # the filler, not the agent (see _handle_barge_in).
        self._tool_task = producer
        self._filler_task = asyncio.create_task(self._hermes_filler(opener),
                                                name="cascade-filler")
        spoken: list = []
        try:
            await self._speak_stream(self._reply_sentences(queue, deadline, started, stage),
                                     spoken=spoken, prepared=self._prepare_tts())
            reply = " ".join(spoken)
            self.messages.append({"role": "assistant", "content": reply})
            self.transcript.append(f"AI: {reply}")
        except asyncio.CancelledError:
            self._note_hermes_interruption(spoken)
            raise
        except (asyncio.TimeoutError, hermes_voice.HermesTurnFailed) as exc:
            timed_out = isinstance(exc, asyncio.TimeoutError)
            logger.warning("Hermes turn %s after %.1fs",
                           "blew its budget" if timed_out else f"failed ({exc})",
                           self._clock() - started)
            self._turn_extra["hermes_turn"] = "timeout" if timed_out else "failed"
            self._tool_task = None
            await self._cancel_filler()            # never overlap filler with the apology
            if spoken:
                self.transcript.append(f"AI: {' '.join(spoken)}")
            apology = HERMES_TIMEOUT_REPLY if timed_out else HERMES_FAILED_REPLY
            try:
                await self._speak(apology)
                self.transcript.append(f"AI: {apology}")
            except Exception:  # noqa: BLE001
                logger.exception("failed to speak the Hermes-turn apology")
        finally:
            self._tool_task = None
            await self._cancel_filler()
            # Closing the stream is what makes upstream interrupt the agent, so a turn we
            # have given up on (or the caller talked over) does not keep acting.
            if not producer.done():
                producer.cancel()
            try:
                await producer
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._hermes_task = None
        stage["llm"] = round((self._clock() - started) * 1000.0, 1)
        if self._hermes.tool_events:
            self._turn_extra["hermes_tool_events"] = self._hermes.tool_events
        return self._hermes.usage

    def _note_hermes_interruption(self, spoken: list) -> None:
        """A hard barge cut the reply. Record what was HEARD, in the transcript and for
        the next turn - the same estimate the recording truncation uses. A turn whose
        answer was never heard (withdrawn by a merge, or cancelled while its first
        sentence was held) has nothing to report: the merge note says why."""
        if not spoken or not self._turn_spoke:
            return
        reply = " ".join(spoken)
        chars = int(self._heard_ms() / 1000.0 * CHARS_PER_SECOND)
        heard = reply[:chars].rstrip()
        self._hermes_heard = heard
        self.messages.append({"role": "assistant",
                              "content": heard + " ...(interrupted by the caller)"})
        self.transcript.append(f"AI: {heard} [interrupted]")

    # ------------------------------------------------------------------ LLM --

    async def _llm_round(self, depth: int = 0) -> "tuple[str | None, dict | None]":
        llm = self._config["llm"]
        key = (self._env.get(llm.get("secret_env") or "") or "").strip()
        if not llm.get("endpoint") or not key:
            raise RuntimeError(f"llm stage unavailable (endpoint/key missing for "
                               f"'{llm.get('provider')}')")
        body = {
            "model": llm["model"],
            "messages": self.messages,
            "temperature": llm["temperature"],
            "max_tokens": LLM_MAX_TOKENS,
        }
        body.update(llm.get("extra_body") or {})
        if self._tools_enabled:
            body["tools"] = CHAT_TOOLS       # advertised ONLY with the profile opt-in
        headers = {"Authorization": f"Bearer {key}"}
        # Per-provider header overrides (opencodego's CF-1010 browser UA, from the shared
        # cascade_config.LLM_EXTRA_HEADERS table). Merged over auth — never overwrites it.
        headers.update(llm.get("extra_headers") or {})
        t0 = self._clock()
        async with httpx.AsyncClient(transport=self._transport,
                                     timeout=STAGE_TIMEOUT_S) as client:
            resp = await client.post(llm["endpoint"], headers=headers, json=body)
        if resp.status_code != 200:
            raise RuntimeError(f"llm HTTP {resp.status_code} from "
                               f"{cascade_config.host_of(llm['endpoint'])}")
        self._turn_extra.setdefault("stage_ms", {})[f"llm{depth or ''}"] = round(
            (self._clock() - t0) * 1000.0, 1)
        data = resp.json() or {}
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        usage = data.get("usage")
        tool_calls = msg.get("tool_calls") or []
        if tool_calls and not self._tools_enabled:
            # Defence in depth: we never advertised tools, but if the model calls one
            # anyway, deny it with a SPOKEN line — a null-content tool message would
            # leave the caller hearing dead air (c3).
            return TOOL_DENIED_REPLY, usage
        if tool_calls and self._tools_enabled and depth < 2:
            self.messages.append(msg)
            for tc in tool_calls:
                await self._run_tool(tc)
            reply, usage2 = await self._llm_round(depth + 1)
            return reply, usage2 or usage
        # Strip any <think>…</think> reasoning the model leaked into content — the lane
        # must never SPEAK an inner monologue (minimax-m3 leaks it; universal no-op else).
        content = cascade_config.strip_reasoning(msg.get("content"))
        return (content or None), usage

    async def _run_tool(self, tool_call: dict) -> None:
        """One hermes_agent dispatch, protected from caller barge-in (s8 c1).

        The filler is a SEPARATE cancellable task, not an inline await: a barge while
        it plays stops the filler AUDIO (``_handle_barge_in`` cancels ``_filler_task``)
        but leaves the backend call running, so the caller's interjection can't kill
        the pending result. A single ``TOOL_BUDGET_S`` wall-clock cap keeps a hung backend from
        wedging the turn — timeout and error both yield a SPOKEN-through tool message,
        never silence. External cancellation (teardown/hangup) still cancels the
        dispatch exactly once."""
        tc_id = tool_call.get("id", "")
        try:
            args = json.loads((tool_call.get("function") or {}).get("arguments", "{}"))
        except ValueError:
            args = {}
        instruction = args.get("instruction", "")
        self._turn_acted = True                       # this turn can no longer be merged
        started = self._clock()
        deadline = started + TOOL_BUDGET_S            # s16 c1: ONE budget, not two in series
        task = asyncio.create_task(self._hermes_call(instruction), name="cascade-tool")
        self._tool_task = task
        ok = True
        try:
            done, _ = await asyncio.wait({task}, timeout=self._filler_debounce_s)
            if task not in done:                          # slow tool → audible filler
                self._filler_task = asyncio.create_task(
                    self._filler_loop(), name="cascade-filler")
            # The debounce we just spent comes OUT of the budget, it is not added to it.
            result = await asyncio.wait_for(
                task, timeout=max(0.0, deadline - self._clock()))
        except asyncio.CancelledError:
            task.cancel()                    # teardown/hangup: NEVER a duplicate dispatch
            self.messages.append({"role": "tool", "tool_call_id": tc_id,
                                  "content": "(tool call interrupted)"})
            raise
        except asyncio.TimeoutError:
            task.cancel()
            ok = False
            # s16 c2: SPEAK it here. TOOL_TIMEOUT_REPLY used to be tool-message content
            # only, so the caller heard nothing until a FURTHER llm round + TTS completed —
            # stacking more silence on top of the wait that had just failed, and producing
            # none at all if that round hung. Speaking directly means the budget expiring
            # is always audible. The tool message then tells the model the apology has
            # already been delivered, so its continuation does not repeat it.
            await self._cancel_filler()          # never overlap filler with the apology
            try:
                await self._speak(TOOL_TIMEOUT_REPLY)
            except Exception:  # noqa: BLE001
                logger.exception("failed to speak the tool-timeout apology")
            result = ("(the lookup timed out and you have ALREADY told the caller you "
                      "will follow up on it separately — continue the conversation "
                      "naturally without repeating that apology)")
        except Exception as exc:  # noqa: BLE001
            result, ok = f"The tool failed: {type(exc).__name__}", False
        finally:
            await self._cancel_filler()
            self._tool_task = None
        self._recorder.on_tool_call("hermes_agent",
                                    (self._clock() - started) * 1000.0, ok=ok)
        self.messages.append({"role": "tool", "tool_call_id": tc_id, "content": result})

    async def _filler_loop(self, initial_delay: float = 0.0,
                           opening_line: "str | None" = None) -> None:
        """Speak the first filler, then a 'still working' reassurance every
        TOOL_REASSURE_AFTER_S until cancelled — so a long backend lookup never leaves
        the caller in silence. Cancelled by tool completion, barge, or teardown.

        ``initial_delay``/``opening_line`` exist for the RE-ARM after a soft barge (s16
        c2): the caller has just spoken, so we wait out a debounce before resuming rather
        than talking straight over them, and we resume with the 'still working' line
        instead of repeating the opener as if nothing had happened."""
        if initial_delay:
            await asyncio.sleep(initial_delay)
        await self._speak(opening_line or self._filler_text, is_filler=True)
        while True:
            await asyncio.sleep(TOOL_REASSURE_AFTER_S)
            await self._speak(FILLER_REASSURE_TEXT, is_filler=True)

    async def _cancel_filler(self) -> None:
        """Stop the filler audio task if it is still playing (idempotent).

        A filler cut mid-line is cleared off the wire too: the audio already handed to
        Twilio would otherwise play on into the answer ("One sec, let me- Take your
        time", report 2.3 mechanism 5).

        s16 c2: a task that already FINISHED still has to be retrieved. The old guard was
        ``if task is not None and not task.done()``, so a filler that RAISED (a TTS 5xx, a
        missing voice) was never awaited, its exception was never retrieved, and the caller
        got silence with nothing in the log to explain it — the same silent-failure family
        as append_event swallowing eventlog writes and hangup_loop swallowing a 404."""
        task = self._filler_task
        self._filler_task = None
        if task is None:
            return
        cut = not task.done() and self._current_is_filler and self._playing
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("filler task failed — the caller heard silence while a tool "
                             "call was in flight")
        if cut:
            await self._clear_wire()

    # ------------------------------------------------------------------ TTS --

    async def _speak(self, text: str, *, is_filler: bool = False) -> None:
        """Say one line (a filler, an apology, a vendor-LLM reply)."""
        async def one():
            yield text
        await self._speak_stream(one(), is_filler=is_filler)

    def _prepare_tts(self) -> "asyncio.Task | None":
        """Open the reply's ElevenLabs socket now, while Hermes is still thinking: the
        TLS handshake costs ~0.46 s (measured 2026-09-23), which would otherwise land
        on the caller's wait for the first word."""
        if self._config["tts"]["provider"] == "deepgram-aura":
            return None
        return asyncio.create_task(self._open_tts(), name="cascade-tts-open")

    async def _open_tts(self) -> elevenlabs_live.TTSStream:
        tts = self._config["tts"]
        key = (self._env.get(tts.get("secret_env") or "") or "").strip()
        if not tts.get("voice") or not key:
            raise RuntimeError("tts stage unavailable (voice/key missing)")
        stream = elevenlabs_live.TTSStream(
            api_key=key, voice=tts["voice"], model=tts.get("model"),
            output_format=self._fmt.elevenlabs_output_format, speed=tts.get("speed"),
            connect=self._tts_connect)
        await stream.open()
        return stream

    async def _speak_stream(self, sentences, *, is_filler: bool = False,
                            spoken: "list | None" = None, prepared=None) -> None:
        """Speak ``sentences`` (an async iterator) as ONE utterance: one ElevenLabs
        socket for the whole reply, fed each sentence as it arrives, its audio framed
        into the wire WHILE it is still being generated (Aura: one HTTP stream per
        sentence into the same framer). ``spoken`` collects the sentences handed to TTS.
        ``prepared`` is an already-opening socket this call takes ownership of.

        ``is_filler`` (s12c): a filler line ("one sec…") counts toward ttfb (first
        audible) but NOT toward answer_latency_ms (first spoken ANSWER)."""
        spoken = [] if spoken is None else spoken
        try:
            first = await sentences.__anext__()
        except StopAsyncIteration:
            await self._discard(prepared)
            return
        except BaseException:
            await self._discard(prepared)
            raise
        self._current_is_filler = is_filler
        self._first_frame_mono = None
        self._frames_sent = 0
        self._stream_sent_ms = 0.0
        self._speak_text = ""
        try:
            if self._config["tts"]["provider"] == "deepgram-aura":
                await self._speak_aura(first, sentences, spoken)
            else:
                await self._speak_elevenlabs(first, sentences, spoken, prepared)
                prepared = None
        finally:
            await self._discard(prepared)
        await self._end_burst()

    def _note_sent(self, text: str, spoken: list) -> None:
        spoken.append(text)
        self._speak_text = " ".join(spoken)

    async def _speak_elevenlabs(self, first: str, sentences, spoken: list,
                                prepared) -> None:
        stream = await prepared if prepared is not None else await self._open_tts()

        async def feed_text():
            try:
                self._note_sent(first, spoken)
                await stream.send(first)
                async for sentence in sentences:
                    self._note_sent(sentence, spoken)
                    await stream.send(sentence)
            except asyncio.CancelledError:
                raise
            except BaseException:
                # Let what was already sent finish playing, then report the failure.
                await stream.finish()
                raise
            await stream.finish()

        sender = asyncio.create_task(feed_text(), name="cascade-tts-text")
        try:
            await self._pump_audio(stream.audio())
            await sender                       # re-raises a Hermes failure after the audio
        finally:
            if not sender.done():
                sender.cancel()
                try:
                    await sender
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await stream.aclose()

    async def _speak_aura(self, first: str, sentences, spoken: list) -> None:
        tts = self._config["tts"]
        key = (self._env.get(tts.get("secret_env") or "") or "").strip()
        if not tts.get("voice") or not key:
            raise RuntimeError("tts stage unavailable (voice/key missing)")
        params = {"model": tts["voice"], "encoding": self._fmt.aura_encoding,
                  "sample_rate": str(self._fmt.sample_rate), "container": "none"}
        headers = {"Authorization": f"Token {key}", "Content-Type": "application/json"}
        text = first
        while text is not None:
            self._note_sent(text, spoken)
            async with httpx.AsyncClient(transport=self._transport,
                                         timeout=STAGE_TIMEOUT_S) as client:
                async with client.stream("POST", cascade_config.AURA_URL, params=params,
                                         headers=headers, json={"text": text}) as resp:
                    if resp.status_code != 200:
                        raise RuntimeError(
                            f"tts HTTP {resp.status_code} from "
                            f"{cascade_config.host_of(cascade_config.AURA_URL)}")
                    await self._pump_audio(resp.aiter_raw())
            try:
                text = await sentences.__anext__()
            except StopAsyncIteration:
                text = None

    async def _pump_audio(self, chunks) -> None:
        """Frame provider audio into the wire, paced to real time. When the audio runs
        dry for longer than it takes the wire to play what it has, the burst ends with
        its mark, so a real pause mid-reply (Hermes running a tool) is heard as silence
        by the detector and the caller's words are not withheld as echo."""
        pending = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(chunks.__anext__())
                if self._burst_open:
                    wait = max(0.0, self._playout_end - self._clock()) + BURST_GAP_S
                    done, _ = await asyncio.wait({pending}, timeout=wait)
                    if not done:
                        await self._end_burst()
                finished, pending = pending, None
                try:
                    chunk = await finished
                except StopAsyncIteration:
                    return
                await self._emit_audio(chunk)
        finally:
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                try:
                    await pending
                except (asyncio.CancelledError, StopAsyncIteration, Exception):  # noqa: BLE001
                    pass

    async def _emit_audio(self, chunk: bytes) -> None:
        if not chunk:
            return
        if not self._burst_open:
            await self._begin_burst()
        self._frame_buf += chunk
        frame_bytes = self._fmt.frame_bytes
        while len(self._frame_buf) >= frame_bytes:
            frame, self._frame_buf = (self._frame_buf[:frame_bytes],
                                      self._frame_buf[frame_bytes:])
            await self._send_frame(frame)
            await self._pace()

    async def _begin_burst(self) -> None:
        """Agent audio is about to be heard. Never start over a caller who is talking
        (report 5.1 item 3): hold, up to HOLD_FOR_CALLER_MAX_S, until they stop."""
        await self._hold_for_caller()
        self._mark_seq += 1
        self._burst_open = True
        self._burst_sent_ms = 0.0
        self._playing = True
        if not self._current_is_filler:
            self._turn_spoke = True

    async def _end_burst(self) -> None:
        if not self._burst_open:
            return
        if self._frame_buf:
            tail, self._frame_buf = self._frame_buf, b""
            await self._send_frame(tail)       # short tail frame
        self._burst_open = False
        await self._wire.emit_mark(self._mark_seq)

    async def _send_frame(self, frame: bytes) -> None:
        now = self._clock()
        if self._first_frame_mono is None:
            self._first_frame_mono = now
            self._recorder.on_audio_delta(self._first_frame_mono)   # ttfb lands here
            if not self._current_is_filler:                          # s12c
                self._recorder.on_answer_audio(self._first_frame_mono)  # answer_latency
        self._frames_sent += 1
        frame_ms = len(frame) / self._fmt.bytes_per_ms
        self._burst_sent_ms += frame_ms
        self._stream_sent_ms += frame_ms
        self._playout_end = max(self._playout_end, now) + frame_ms / 1000.0
        # Record side-effect (ttfb, counter) stays here — wire-agnostic; the wire only
        # ships bytes. emit_mark carries the seq so a synthesizing wire can key on it.
        await self._wire.emit_frame(frame)
        # Ticket 07: capture what we actually put on the wire, AFTER we put it there.
        # The burst's mark number is the utterance boundary the truncation keys on.
        self._recording.agent_audio(frame, item_id=self._mark_seq)

    async def _pace(self) -> None:
        """Throttle to ~real-time so the wire buffers at most PACING_LEAD_S of audio.
        Without pacing a multi-second reply floods Twilio's buffer in ~700ms, and a barge
        'clear' fights seconds of already-buffered speech ("won't shut up"). Only ever
        sleeps when we're ahead, so first-frame timing is untouched."""
        delay = self._playout_end - PACING_LEAD_S - self._clock()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _discard(self, prepared) -> None:
        """Close a TTS socket that was opened for a reply that never came."""
        if prepared is None:
            return
        if not prepared.done():
            prepared.cancel()
        try:
            stream = await prepared
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            return
        await stream.aclose()

    # ------------------------------------------------------------- barge-in --

    async def _clear_wire(self) -> None:
        """Drop the audio the wire still holds, and keep the recording honest about it."""
        self._recording.agent_truncate(self._audible_ms())   # ticket 07
        self._playing = False
        self._burst_open = False
        self._frame_buf = b""
        self._playout_end = self._clock()
        try:
            await self._wire.emit_clear()
        except Exception:  # noqa: BLE001
            logger.warning("wire clear failed", exc_info=True)

    async def _handle_barge_in(self) -> bool:
        """Handle a confirmed barge. Returns True for a SOFT barge (a tool is in
        flight), False for the normal HARD barge.

        Soft (s8 c1/c2): the caller talks over the filler while the backend call is
        running. Stop ONLY the filler audio and flush the wire; the tool keeps running
        and its result is still spoken on return, and the caller's speech keeps
        flowing to STT (``_on_media`` skips the STT reset) so it becomes the next
        turn. Never cancels the turn task — that would kill the pending result, the
        exact dead-air bug s7 shipped.

        Hard: cancel the in-flight reply turn, flush the wire, truncate the interrupted
        assistant text so the next LLM turn sees only what was actually heard."""
        tool_active = self._tool_task is not None and not self._tool_task.done()
        if tool_active:
            task, self._filler_task = self._filler_task, None
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            # Ticket 07: the filler's un-played tail was dropped by the wire, so it must
            # not survive in the recording either.
            await self._clear_wire()
            # s16 c2: RE-ARM. Before this, the filler was cancel-ONCE: one interjection —
            # or, on the Talk lane where there is no AEC on the null-sinks, one cough —
            # silenced the entire remainder of the tool call. The tool itself keeps
            # running (s8 c1); only its audible cover was being destroyed. Re-armed after
            # a debounce, and the filler never starts while the caller is still talking.
            if self._tool_task is not None and not self._tool_task.done():
                self._filler_task = asyncio.create_task(
                    self._filler_loop(initial_delay=self._filler_debounce_s,
                                      opening_line=FILLER_REASSURE_TEXT),
                    name="cascade-filler-rearm")
            return True
        was_playing = self._playing
        # The caller must stop hearing us NOW, not after the cut turn has closed its TTS
        # socket and its Hermes stream. What they heard is frozen first, because the
        # clear empties the queue that figure is computed from.
        self._heard_frozen_ms = self._heard_ms()
        task = self._turn_task if (self._turn_task is not None
                                   and not self._turn_task.done()) else None
        if task is not None:
            task.cancel()
        try:
            await self._clear_wire()
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if was_playing:
                self._truncate_interrupted_reply()
        finally:
            self._heard_frozen_ms = None
        return False

    def _queued_ms(self) -> float:
        return max(0.0, self._playout_end - self._clock()) * 1000.0

    def _audible_ms(self) -> float:
        """How much of the current burst the other party actually HEARD: what was handed
        to the wire minus what it still holds. The recording truncation keys on it."""
        return max(0.0, self._burst_sent_ms - self._queued_ms())

    def _heard_ms(self) -> float:
        """The same, over the whole reply being spoken: the text truncation keys on it."""
        if self._heard_frozen_ms is not None:
            return self._heard_frozen_ms
        return max(0.0, self._stream_sent_ms - self._queued_ms())

    def _truncate_interrupted_reply(self) -> None:
        """Approximate truncation (D2 - no word timestamps): keep roughly the chars
        that fit in the time the reply was audible, bounded by the audio actually
        handed to the wire. The next LLM turn sees the SHORTENED text."""
        text = self._speak_text
        if not text:
            return
        chars = int(self._heard_ms() / 1000.0 * CHARS_PER_SECOND)
        truncated = text[:chars].rstrip()
        marker = " ...(interrupted by the caller)"
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "assistant" and \
                    self.messages[i].get("content") == text:
                self.messages[i]["content"] = truncated + marker
                break
        if self.transcript and self.transcript[-1] == f"AI: {text}":
            self.transcript[-1] = f"AI: {truncated} [interrupted]"

    # ------------------------------------------------------------- teardown --

    async def teardown(self, outcome: str = "ok") -> None:
        """Idempotent: sockets closed, ONE call record, at most ONE retain (c8)."""
        if self._finished:
            return
        self._finished = True
        tasks = (self._resume_task, self._turn_task, self._tool_task, self._filler_task,
                 self._hermes_task)
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        for task in tasks:
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        try:
            await self._stt.close()
        except Exception:  # noqa: BLE001
            logger.warning("STT close failed", exc_info=True)
        # Ticket 07: close the capture before the retain, so the Call's metadata can
        # reference a file that is already on the volume. Bounded, never raises, and the
        # writer-thread join happens OFF this event loop.
        rec = await call_recording.finish_async(self._recording)
        retain_status, doc_id = "skipped", None
        retain_on = (self._profile.retain_enabled(self._retain_default)
                     if self._profile is not None else self._retain_default)
        # No URL gate here: an empty Hindsight URL means the SQLite archive, and
        # `call_record.retain_call` is the one place that decides where a call goes.
        if retain_on and self.transcript:
            doc_id = f"voice-cascade-{self._recorder.call_id}"
            # s5 (ticket 05): one shared metadata builder for all three lanes. A call with
            # no selected profile records NO agent - it used to record the literal string
            # "no-profile", which the Calls screen would have shown as if it were an
            # Agent's name.
            retain_status = call_record.retain_call(
                url=self._hindsight_url, bank=self._hindsight_bank,
                recorder=self._recorder, transcript=self.transcript,
                document_id=doc_id, platform="voice_cascade", lane="cascade",
                agent=getattr(self._profile, "agent_id", None),
                mission=self._mission_brief, outcome=outcome,
                recording=rec.ref,      # ticket 07: one additive field
                summariser=self._summariser)   # ticket 06: the second
            if retain_status != "dispatched":
                doc_id = None
        try:
            self._recorder.finish(outcome=outcome, transcript_ref=doc_id,
                                  retain_status=retain_status,
                                  recording_ref=rec.ref, recording_status=rec.status)
        except Exception:  # noqa: BLE001
            logger.exception("eventlog finish failed")
