"""s14b: the Talk-cascade start crash — `overlay_profile` on a REAL cascade profile.

Session 1 of the s14b live campaign found Talk cascade dead on arrival: every
`session.start()` for a cascade profile raised `KeyError: 'realtime'` from deep inside
`config.overlay_profile`, was caught, and surfaced as a bare HTTP 409.

Root cause: the overlay unconditionally resolves three realtime-only knobs
(voice / model / transcription_model); each walks
`resolve -> _registry_default_knobs -> realtime_provider -> doc["providers"]["realtime"]`.
A cascade profile's providers block is `{stt, llm, tts}` — that key does not exist.

Why 118 unit tests missed it: every existing cascade test builds its doc with
`profile_doc(pipeline="cascade")`, which keeps the MINIMAL_PROFILE `providers.realtime`
key. The fixtures were cascade in name and realtime in shape. These tests use the real
`{stt, llm, tts}` block instead.
"""
import pytest

import config
from voicecore import profiles
from profile_helpers import profile_doc


CASCADE_PROVIDERS = {"stt": "deepgram", "llm": "gpt-4.1", "tts": "elevenlabs"}


def cascade_doc(**overrides) -> dict:
    """A cascade profile shaped like the supplier-caller worked example the repo used
    to ship (VC18 deleted it in ticket 15) - no realtime key."""
    doc = profile_doc(pipeline="cascade", **overrides)
    doc["providers"] = dict(CASCADE_PROVIDERS)
    return doc


def _profile(doc) -> "profiles.ActiveProfile":
    return profiles.ActiveProfile(agent_id=doc["id"], source="s14b-test",
                                  doc=doc, registry={})


# -- the crash itself ---------------------------------------------------------

def test_overlay_profile_survives_a_real_cascade_profile():
    """RED before the fix: KeyError: 'realtime'."""
    base = config.load_base()
    cfg = config.overlay_profile(base, _profile(cascade_doc()))
    assert cfg is not None


def test_cascade_overlay_leaves_realtime_only_knobs_at_base():
    """Cascade never uses voice/model/transcription_model — they must not be invented
    from a realtime registry the profile does not reference."""
    base = config.load_base()
    cfg = config.overlay_profile(base, _profile(cascade_doc()))
    assert cfg.openai_voice == base.openai_voice
    assert cfg.openai_model == base.openai_model
    assert cfg.transcription_model == base.transcription_model


def test_cascade_overlay_still_applies_the_pipeline_agnostic_knobs():
    """vad_silence_ms + retain_enabled are real for cascade and must survive the guard —
    a fix that skipped the whole overlay would silently drop the D8 retain opt-out."""
    base = config.load_base()
    doc = cascade_doc(knobs={"vad": {"silence_ms": 1234}}, memory={"retain": False})
    cfg = config.overlay_profile(base, _profile(doc))
    assert cfg.vad_silence_ms == 1234
    assert cfg.retain_enabled is False


def test_resolve_falls_through_instead_of_raising_without_a_realtime_provider():
    """The deeper guard: ANY resolve() on a providers-block with no realtime key must
    fall through to the caller's fallback, not raise. overlay_profile is only today's
    caller; server/session paths must not re-arm this landmine."""
    prof = _profile(cascade_doc())
    assert prof.resolve(("voice",), None, "sentinel-fallback") == "sentinel-fallback"


# -- the realtime path must be byte-identical ---------------------------------

def test_realtime_overlay_is_unchanged_by_the_guard():
    base = config.load_base()
    doc = profile_doc(knobs={"voice": "marin", "vad": {"silence_ms": 900}})
    cfg = config.overlay_profile(base, _profile(doc))
    assert cfg.openai_voice == "marin"
    assert cfg.vad_silence_ms == 900
    assert cfg.openai_model == base.openai_model


def test_realtime_provider_property_still_raises_for_a_realtime_profile_missing_it():
    """The guard keys off the PROVIDERS BLOCK, not off pipeline=='cascade' — but a
    realtime profile with no realtime provider is genuinely malformed and the schema
    validator owns that error. Pin that we did not turn it into a silent default."""
    doc = profile_doc()
    doc["providers"] = {}
    with pytest.raises(KeyError):
        _profile(doc).realtime_provider
