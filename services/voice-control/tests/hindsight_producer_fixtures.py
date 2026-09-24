"""Hindsight documents built ONLY from what the retainers actually write.

Read from the producers, not from any earlier test in this repo (three rounds of
ticket 01 passed their own author's fixtures and failed a browser).

TWO GENERATIONS OF DOCUMENT LIVE IN THE STORE, and the screens must tell them
apart without inventing anything for the older one.

**Before ticket 05** -- everything the system had retained up to 2026-08-18:

    services/voice/server.py
        metadata={"platform": "voice_twilio", "direction": ..., "target": ...,
                  "date": "%Y-%m-%d"}          tags=["voice", "twilio", direction]
    services/talk-voice-bridge/realtime_bridge.py
        metadata={"platform": "voice_talk", "direction": ..., "target": ...,
                  "date": "%Y-%m-%d"}          tags=["voice", "talk", direction]
    services/voicecore/cascade_live.py
        metadata={"platform": "voice_cascade", "direction": ..., "target": ...,
                  "agent": agent_id}           tags=["voice", "cascade", direction, agent_id]

None of them writes an outlet, a mission, an outcome, a duration, a summary, a
turn count or a caller. **The cascade retainer is the one exception on `agent`:
it wrote one from the start**, so a pre-05 corpus is not uniformly agent-less,
and a test that asserts "every old row says not retained in the Agent column"
is wrong rather than strict. `retained_doc` builds these; that is what
`three_real_calls` returns, and every screen assertion about "not retained"
rests on them.

**Since ticket 05** every retainer goes through ONE builder,
`services/voicecore/call_record.py`, which writes platform, outlet, direction,
target, agent, mission, outcome, duration_s and an ISO-8601 UTC `timestamp` --
and OMITS any of them it was not given. `retained_doc_v5` builds these. It
deliberately does NOT accept a value for every field: an inbound call has no
mission and a call with no assigned Agent has no agent, and the fixtures have to
be able to express that, because "absent" is the only way either producer says
"nothing was recorded".

    services/voicecore/hindsight.py
        every metadata value is stringified before it is posted -- including the
        duration, which is why `duration_s` arrives as a string.

A retained document exists BECAUSE the call reached teardown with a non-empty
transcript, so a document with no summary is an ordinary completed call, not a
call that broke.

**Since ticket 06** the same builder also writes `summary` and `summary_state`
(`services/voicecore/summary.py`). Four states are producible and the screens must
tell them apart: a summary the Agent wrote; `nothing_to_summarise` (the call held
no conversation to describe); `unavailable` (the Agent was asked and could not
answer); and NEITHER key, which means nobody was asked. There is deliberately no
"still being written" state - the summary is settled before the document exists.

ONE THING HERE IS NOT A PRODUCER FACT: ``created_at``. No retainer sends it;
it is assumed to be stamped by Hindsight and returned by its documents endpoint,
and that assumption cannot be settled without the live store. Because it hid a
defect once already, ``drop_created_at`` builds the other case -- a document
where the only timestamp is the retainers' date-only ``metadata["date"]``, which
must render as a date and never as a clock time. Keep both cases covered.
"""


def retained_doc(doc_id, platform, direction, target, created_at,
                 agent=None, tags=None, text=None, extra_metadata=None):
    """One document exactly as a retainer wrote it, as Hindsight hands it back.

    Hindsight returns the stored verbatim text as ``original_text`` and stamps
    its own ``created_at``; ``document_id`` is the id the retainer chose.
    """
    metadata = {"platform": platform, "direction": direction, "target": target}
    if agent is None:
        metadata["date"] = created_at[:10]
    else:
        metadata["agent"] = agent
    if extra_metadata:
        metadata.update(extra_metadata)
    return {
        "id": doc_id,
        "document_id": doc_id,
        "created_at": created_at,
        "original_text": text if text is not None else (
            "Them: exact original transcript " + doc_id + "\n"
            "AI: exact reply " + doc_id
        ),
        "metadata": {k: str(v) for k, v in metadata.items()},
        "tags": tags if tags is not None else (
            ["voice", platform.split("_")[-1], direction]
            + ([agent] if agent is not None else [])  # cascade_live.py:726
        ),
    }


def as_store_returns(doc):
    """One producer document in the shape the STORE HANDS BACK, not the shape we POST.

    THE DIFFERENCE THAT COST A RELEASE. Every builder above models the retainers'
    POST body, where the fields live under ``metadata`` (`hindsight.retain_result`
    builds ``item["metadata"]``). Hindsight does not serve them back under that
    key. It serves ``document_metadata``, and a returned document has NO
    ``metadata`` key at all. Verified against the live store on 2026-08-19, on
    both banks and on both endpoints; a listed `voice` document's top-level keys
    are::

        bank_id content_hash created_at document_metadata id memory_unit_count
        retain_params tags text_length updated_at

    Reading the POST key made the dashboard render EVERY retained field as "not
    retained" - ticket 05's agent/outlet/outcome/duration, ticket 07's recording,
    ticket 06's summary - while the store held them, and no fixture here could
    see it, because they all agreed with the code instead of with the store.

    So: **a test that exercises the reader must pass documents through this.**
    The browser harness does it for every browser test. The POST shape is still
    worth testing directly (the reader accepts both), but it proves nothing about
    what the screens show.

    ONE DIFFERENCE THIS DOES NOT MODEL, DELIBERATELY AND ON RECORD: the live LIST
    endpoint omits ``original_text`` (it returns ``text_length`` instead); only
    fetch-by-id carries the text. So a real list row has no transcript in it and
    a real detail row does. Modelling that would change what the list tests mean,
    which is a bigger question than the metadata key and belongs to whoever next
    touches the transcript column - not silently, here. If a transcript defect
    ever shows up on the LIST specifically, this is the first thing to model.
    """
    out = {k: v for k, v in doc.items() if k != "metadata"}
    if "metadata" in doc:
        out["document_metadata"] = doc["metadata"]
    return out


def drop_created_at(doc):
    """The same document as a store that does not stamp `created_at` returns it.

    For twilio and talk documents the only timestamp left is the retainers'
    date-only `metadata["date"]`; a cascade document is left with no timestamp
    at all, which is the case the screens already render as not retained.
    """
    return {k: v for k, v in doc.items() if k != "created_at"}


def twilio_inbound(doc_id, created_at, **kw):
    """Inbound Twilio call. Producers leave `target` empty for inbound."""
    return retained_doc(doc_id, "voice_twilio", "inbound", "", created_at, **kw)


def talk_outbound(doc_id, created_at, target="+61491570157", **kw):
    return retained_doc(doc_id, "voice_talk", "outbound", target, created_at, **kw)


def cascade_outbound(doc_id, created_at, target="+61491570158", agent="hermes-main", **kw):
    return retained_doc(doc_id, "voice_cascade", "outbound", target, created_at,
                        agent=agent, **kw)


def non_call_memory(doc_id, created_at, text="Jamie prefers espresso over filter coffee."):
    """A document in the shared `hermes` bank that is NOT a call.

    The fallback bank is Hermes's general memory bank. Anything in it that no
    voice retainer wrote must not be listed as a phone call.
    """
    return {
        "id": doc_id,
        "document_id": doc_id,
        "created_at": created_at,
        "original_text": text,
        "metadata": {"source": "hermes-chat"},
        "tags": ["preference"],
    }


def three_real_calls():
    """One document from each of the three retainers, newest last."""
    return [
        twilio_inbound("voice-twilio-inbound-real", "2026-08-17T09:05:00Z"),
        talk_outbound("voice-talk-outbound-real", "2026-08-17T10:15:00Z"),
        cascade_outbound("voice-cascade-outbound-real", "2026-08-17T11:25:00Z"),
    ]


def hypothetical_call_with_outcome():
    """A PRE-05 document that nonetheless carries an outcome and a summary.

    It exists to prove the screens still relay a retained value when there is one, and
    so the "not retained" rendering can be told apart from a rendering that simply
    always says "not retained". Since ticket 06 a summary IS written by a producer - by
    the Agent that was on the call - but only in the post-05 shape, and only when the
    call held a conversation; see the `summary_*_v5` builders below.
    """
    return talk_outbound(
        "voice-talk-with-summary", "2026-08-17T12:35:00Z", target="+61491570159",
        extra_metadata={"outcome": "ok", "summary": "Booked the table for 7pm."},
    )


# -- since ticket 05: the shared builder's shape -----------------------------------


def retained_doc_v5(doc_id, platform, lane, created_at, *, direction="outbound",
                    outlet="phone", target=None, agent=None, mission=None,
                    outcome="ok", duration_s=63.4, text=None,
                    summary=None, summary_state=None, recording=None):
    """One document exactly as `voicecore.call_record` writes it today.

    Every optional field is omitted when it is None, which is the builder's own
    rule and the only way a document says "not recorded". Pass ``agent=None`` for
    a call with no assigned Agent and ``mission=None`` for an inbound call; do
    NOT pass "" or "unknown", because no producer can write those.
    """
    metadata = {"platform": platform, "outlet": outlet, "direction": direction,
                "timestamp": created_at}
    if target is not None:
        metadata["target"] = target
    if agent is not None:
        metadata["agent"] = agent
    if mission is not None:
        metadata["mission"] = mission
    if outcome is not None:
        metadata["outcome"] = outcome
    if duration_s is not None:
        metadata["duration_s"] = f"{float(duration_s):.1f}"
    if recording is not None:
        metadata["recording"] = recording
    # Ticket 06. The builder keeps the two consistent, so the fixtures do too: a
    # document never carries prose under a state that says there is none, and a
    # `written` state always has a summary under it. Pass summary_state alone to
    # build the two ABSENCES the screen has to tell apart.
    if summary is not None:
        metadata["summary"] = summary
        metadata["summary_state"] = "written"
    elif summary_state is not None:
        metadata["summary_state"] = summary_state

    tags = ["voice"]
    for value in (lane, direction, outlet, agent):
        if value and value not in tags:
            tags.append(value)

    return {
        "id": doc_id,
        "document_id": doc_id,
        "created_at": created_at,
        "original_text": text if text is not None else (
            "Them: exact original transcript " + doc_id + "\n"
            "AI: exact reply " + doc_id
        ),
        "metadata": {k: str(v) for k, v in metadata.items()},
        "tags": tags,
    }


def phone_outbound_v5(doc_id, created_at, agent="hermes-main", **kw):
    return retained_doc_v5(doc_id, "voice_twilio", "twilio", created_at,
                           direction="outbound", outlet="phone",
                           target="+61400000000", agent=agent,
                           mission="Book a table for 7pm.", **kw)


def talk_inbound_v5(doc_id, created_at, agent="talk-answerer", **kw):
    """Inbound: no mission, and the producers leave `target` unset for inbound."""
    return retained_doc_v5(doc_id, "voice_talk", "talk", created_at,
                           direction="inbound", outlet="talk", agent=agent, **kw)


def phone_inbound_v5_no_agent(doc_id, created_at, **kw):
    """A call on an Outlet with no assigned Agent: agent absent, not a stand-in."""
    return retained_doc_v5(doc_id, "voice_twilio", "twilio", created_at,
                           direction="inbound", outlet="phone", agent=None, **kw)


# -- since ticket 06: the four things a document can say about a summary -----------


def summary_written_v5(doc_id, created_at, **kw):
    """The Agent on the call wrote one."""
    return phone_outbound_v5(
        doc_id, created_at,
        summary="Chased the Tuesday delivery; it shipped Monday and lands tomorrow.",
        **kw)


def summary_nothing_to_say_v5(doc_id, created_at, **kw):
    """The call held no conversation to describe, so none was asked for or written.

    A fact ABOUT THE CALL, and the screen must not render it the same way it renders a
    summariser that fell over - or the same way it renders a call nobody asked about.
    """
    return phone_outbound_v5(doc_id, created_at, summary_state="nothing_to_summarise",
                             **kw)


def summary_unavailable_v5(doc_id, created_at, **kw):
    """The Agent was asked and could not answer: a gap in the record."""
    return phone_outbound_v5(doc_id, created_at, summary_state="unavailable", **kw)


def summary_never_asked_v5(doc_id, created_at, **kw):
    """Summarisation was off for that lane (or the call predates ticket 06).

    No `summary` and no `summary_state` at all - the document claims nothing either way.
    """
    return phone_outbound_v5(doc_id, created_at, **kw)
