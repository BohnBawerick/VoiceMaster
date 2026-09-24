"""The honest proven/untested classification for the Settings page (ticket 14).

What "proven" means here is established from evidence in the repo, never from
intuition or from a probe's green light:

  - a real call exercised it (VC22 keeps the live phone line working; the
    deployed compose file runs these exact services with OPENAI_VOICE=ash;
    the direct Hermes lane has carried real phone calls, ``DIRECT_LANE_EVIDENCE``);
  - the decisions docs chose it and said why (VC7: "the default path is the
    one that works", "nineteen untested providers look as valid as the one
    proven one is the actual defect");
  - ticket 13 already preselects it (``hermes_profiles.PROVEN_PIPELINE`` /
    ``PROVEN_REALTIME_PROVIDER``, served to the wizard as ``proven``).

Where the evidence does not exist the answer is UNTESTED, and that is the whole
point: a wired cascade client, or a probe that returns 200, is not a real call,
and nothing here presents one as the other. ``IMPLEMENTED_REALTIME_PROVIDER``
and ``CASCADE_WIRING`` (both in voicecore) say what the bridges are capable of;
this module says what a human should trust.

Each entry carries the evidence as a string the Settings screen renders, so the
label a user reads is the reason, not a bare boolean.
"""
from voicecore import cascade_config
from voicecore import profiles

import hermes_profiles

#: What ticket 13 preselects (realtime on the only implemented realtime
#: provider). Served verbatim as the wizard's ``proven``; this module keeps the
#: same answer so the Settings page and the wizard can never disagree.
PROVEN_PIPELINE = hermes_profiles.PROVEN_PIPELINE
PROVEN_REALTIME_PROVIDER = hermes_profiles.PROVEN_REALTIME_PROVIDER

#: The voice the live phone line actually speaks. Evidence: the deployed
#: compose file sets
#: ``OPENAI_VOICE: "ash"`` on the phone bridge (mode-c), the Talk bridge and the
#: dashboard; that is the only voice a real call has exercised. Everything else
#: is untested by that same bar.
PROVEN_REALTIME_VOICE = "ash"

#: Realtime-lane voice options. THE one place they live: the Settings page serves
#: them from ``GET /api/settings`` and the wizard from ``GET /api/hermes``
#: (``realtime_voices``), so a change here moves both screens and neither can
#: drift. Suggestions are NOT verified: only ``PROVEN_REALTIME_VOICE`` has the
#: evidence above. Note the proven voice is deliberately first and is not one of
#: the suggestions - it is the answer, they are the alternatives.
REALTIME_VOICE_OPTIONS = [PROVEN_REALTIME_VOICE, "cedar", "marin", "alloy",
                          "echo", "shimmer"]

PIPELINE_EVIDENCE = {
    "realtime": (
        "The live phone line answers with this lane (VC22), ticket 13 preselects "
        "it, and voicecore.profiles.IMPLEMENTED_REALTIME_PROVIDER names the "
        "provider it runs."),
    "cascade": (
        "Wired on both bridges (voicecore.profiles.CASCADE_CAPABILITY). It places "
        "outbound calls for any cascade Agent and answers inbound calls only when "
        "Hermes itself is the llm stage. Real phone calls have proven it with Hermes "
        "as the llm stage (the Hermes directly agent type); no outside-vendor cascade "
        "has carried a real call since the rebuild. A wired cascade client is not a "
        "proven call."),
}

#: The evidence that proves the direct Hermes lane (VC24, open item O5): the calls in
#: the live event log, and the one the owner heard pass. Phone only: no Talk call has
#: carried the lane yet.
DIRECT_LANE_EVIDENCE = (
    "Real phone calls have carried it, inbound and outbound, since 2026-09-20. An "
    "inbound call on 2026-09-24 (540 s, ElevenLabs Scribe, Hermes, ElevenLabs) "
    "passed by ear. No Talk call has carried it yet.")


def pipeline_label(pipeline_id: str) -> dict:
    """proven / evidence for one pipeline id."""
    return {
        "proven": pipeline_id == PROVEN_PIPELINE,
        "evidence": PIPELINE_EVIDENCE.get(
            pipeline_id, "No evidence in this repo that this pipeline ever "
                         "carried a real call."),
    }


#: VC24: what the person is TALKING TO. An agent type is a preset over the same
#: pipeline/providers fields an Agent has always had - it invents no new field, so an
#: Agent written by hand and one made by clicking here are the same document. Every
#: outside-vendor combination stays selectable under Advanced (VC7: nothing is removed,
#: only reorganised).
AGENT_TYPE_HERMES = "hermes-direct"
#: Ticket 21: the ears and the voice a Hermes Direct Agent may have, the default first
#: (ElevenLabs in, Hermes as the brain, ElevenLabs out). Every one has a live client.
HERMES_STT_OPTIONS = (cascade_config.STT_ELEVENLABS, "deepgram")
HERMES_TTS_OPTIONS = ("elevenlabs", "deepgram-aura")
AGENT_TYPE_REALTIME = "realtime"
AGENT_TYPE_CUSTOM = "custom"

AGENT_TYPES = [
    {
        "id": AGENT_TYPE_HERMES,
        "name": "Hermes directly",
        "summary": ("ElevenLabs hears, the Hermes profile you pick thinks and acts, "
                    "ElevenLabs speaks. No OpenAI Realtime model in between."),
        "pipeline": "cascade",
        "providers": {"stt": cascade_config.STT_ELEVENLABS,
                      "llm": profiles.HERMES_LLM_PROVIDER, "tts": "elevenlabs"},
        # Ticket 21: who hears and who speaks are choices on this type, ElevenLabs by
        # default for both.
        "stt_options": list(HERMES_STT_OPTIONS),
        "tts_options": list(HERMES_TTS_OPTIONS),
        "proven": True,
        "evidence": DIRECT_LANE_EVIDENCE,
    },
    {
        "id": AGENT_TYPE_REALTIME,
        "name": "OpenAI Realtime",
        "summary": ("An OpenAI speech-to-speech model holds the conversation and calls "
                    "Hermes as a tool when it decides to."),
        "pipeline": PROVEN_PIPELINE,
        "providers": {"realtime": PROVEN_REALTIME_PROVIDER},
        "proven": True,
        "evidence": "The live phone line answers with this lane (VC22).",
    },
]


def agent_types() -> list:
    return [dict(t, providers=dict(t["providers"]),
                 **{key: list(t[key]) for key in ("stt_options", "tts_options") if key in t})
            for t in AGENT_TYPES]


def agent_type_of(doc) -> str:
    """Which agent type an Agent document is, or ``custom`` for anything else (an
    outside-vendor cascade, an unimplemented realtime provider). ``custom`` is an
    honest answer, not an error: those Agents are edited under Advanced."""
    if not isinstance(doc, dict):
        return AGENT_TYPE_CUSTOM
    stages = doc.get("providers") if isinstance(doc.get("providers"), dict) else {}
    for kind in AGENT_TYPES:
        if doc.get("pipeline") != kind["pipeline"]:
            continue
        # A stage with options matches any of them (a Hermes Direct Agent is the same
        # type whichever ears and voice it uses); every other stage must match exactly.
        options = {role: kind[f"{role}_options"] for role in ("stt", "tts")
                   if f"{role}_options" in kind}
        fixed = {k: v for k, v in kind["providers"].items() if k not in options}
        if all(stages.get(role) in allowed for role, allowed in options.items()) and \
                {k: v for k, v in stages.items() if k not in options} == fixed:
            return kind["id"]
    return AGENT_TYPE_CUSTOM


#: The ears and the voice the proven calls used: the Hermes directly type's defaults.
#: Deepgram and Aura stay choices on that type, but no real call has carried them in it.
_DIRECT_LANE_EARS_AND_VOICE = {
    stage for t in AGENT_TYPES if t["id"] == AGENT_TYPE_HERMES
    for role, stage in t["providers"].items() if role in ("stt", "tts")}


def _cascade_wired(role: str, provider_id: str) -> bool:
    """Does a live cascade client exist for this role/provider?

    ``CASCADE_WIRING`` is keyed by the role plus a ``_live`` variant where the
    live lane's streaming client differs from the bench's (stt vs stt_live,
    tts vs tts_live). A provider with a client in either set is wired.
    """
    for key in (role, f"{role}_live"):
        members = cascade_config.CASCADE_WIRING.get(key)
        if members and provider_id in members:
            return True
    return False


def provider_label(entry: dict) -> dict:
    """proven / wired / evidence for one registry provider entry."""
    pid = entry["id"]
    role = entry.get("role") or ""

    if role == "realtime":
        if pid == PROVEN_REALTIME_PROVIDER:
            return {
                "proven": True,
                "wired": True,
                "evidence": (
                    "The only realtime provider the bridges implement "
                    "(voicecore.profiles.IMPLEMENTED_REALTIME_PROVIDER); the "
                    "live phone line runs it."),
            }
        return {
            "proven": False,
            "wired": False,
            "evidence": (
                "Not wired in the realtime lane - the bridges refuse any "
                "realtime provider other than "
                f"{PROVEN_REALTIME_PROVIDER}."),
        }

    if pid == profiles.HERMES_LLM_PROVIDER:
        return {
            "proven": True,
            "wired": True,
            "evidence": (
                "The Agent's own Hermes profile is the llm stage of the cascade "
                "pipeline (VC24): it holds the conversation, the tools and the memory. "
                + DIRECT_LANE_EVIDENCE),
        }
    if pid in _DIRECT_LANE_EARS_AND_VOICE:
        return {
            "proven": True,
            "wired": True,
            "evidence": (
                "The default " + role + " of the Hermes directly agent type. It "
                "carried the inbound phone call on 2026-09-24 (540 s) that proved "
                "that lane by ear."),
        }

    wired = _cascade_wired(role, pid)
    if wired:
        return {
            "proven": False,
            "wired": True,
            "evidence": (
                "A cascade client is wired for this provider, but it has "
                "never carried a real call. Wired is not proven."),
        }
    return {
        "proven": False,
        "wired": False,
        "evidence": "No cascade client is wired for this provider.",
    }


def voice_label(provider_id: str, voice_id: str) -> dict:
    """proven / evidence for one voice on one provider."""
    if provider_id == PROVEN_REALTIME_PROVIDER and voice_id == PROVEN_REALTIME_VOICE:
        return {
            "proven": True,
            "evidence": (
                "The live phone line speaks this voice (OPENAI_VOICE=ash in the "
                "deployed compose file); the only voice a real call has used."),
        }
    return {
        "proven": False,
        "evidence": "Never exercised by a real call.",
    }


def realtime_voice_options() -> list:
    """The realtime-lane voice picker: the proven voice first, then the
    suggestions, each labelled honestly."""
    return [
        {
            "id": vid,
            "proven": vid == PROVEN_REALTIME_VOICE,
            "evidence": voice_label(PROVEN_REALTIME_PROVIDER, vid)["evidence"],
        }
        for vid in REALTIME_VOICE_OPTIONS
    ]


def proven_defaults() -> dict:
    """The one combination a working Agent needs no decision about."""
    return {
        "pipeline": PROVEN_PIPELINE,
        "realtime_provider": PROVEN_REALTIME_PROVIDER,
        "voice": PROVEN_REALTIME_VOICE,
    }
