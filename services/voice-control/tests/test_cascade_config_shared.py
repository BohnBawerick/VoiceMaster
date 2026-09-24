"""s7 c1 - the ONE cascade config builder is shared with the live lane.

Ticket 15 deleted ``cascade.py`` (the bench session and its re-export shim), so the
node that proved the bench imported the shared builder rather than a lookalike went
with it. The dashboard now imports ``voicecore.cascade_config`` directly -- pinned by
``test_the_dashboard_previews_through_the_shared_builder`` below, which is the same
near-miss guard aimed at the surviving caller.
"""
import os
from pathlib import Path

import app as voice_app
from voicecore import cascade_config
from voicecore import profiles

APP_DIR = Path(cascade_config.__file__).resolve().parent

REGISTRY_PATH = APP_DIR.parent / "voice-config" / "providers.yaml"


def _registry():
    return profiles.load_registry(APP_DIR.parent / "voice-config")


def test_the_dashboard_previews_through_the_shared_builder():
    # `POST /api/agents/preview` builds a cascade draft with the SAME builder the live
    # lane runs - not a lookalike (s7 c1 near-miss guard).
    assert voice_app.cascade_config is cascade_config
    assert voice_app.cascade_config.build_cascade_config is cascade_config.build_cascade_config
    assert voice_app.cascade_config.CascadeConfigError is cascade_config.CascadeConfigError


def test_openrouter_default_model_is_gpt4o_mini_class():
    """D4: a cascade profile that names openrouter but omits knobs.model resolves to the
    non-reasoning gpt-4o-mini-class registry default — in the shared builder every lane
    (bench, preview, LIVE) consumes."""
    doc = {"id": "d4-default", "pipeline": "cascade",
           "providers": {"stt": "deepgram", "llm": "openrouter", "tts": "elevenlabs"}}
    cfg = cascade_config.build_cascade_config(doc, _registry(), {})
    assert cfg["llm"]["provider"] == "openrouter"
    assert cfg["llm"]["model"] == "openai/gpt-4o-mini"
    assert cfg["llm"]["endpoint"] == "https://openrouter.ai/api/v1/chat/completions"


def test_deepgram_is_live_wired_but_not_bench_wired():
    doc = {"id": "dg", "pipeline": "cascade",
           "providers": {"stt": "deepgram", "llm": "openrouter", "tts": "elevenlabs"}}
    cfg = cascade_config.build_cascade_config(doc, _registry(), {})
    assert cfg["stt"]["wired_live"] is True     # s7 streaming lane
    assert cfg["stt"]["wired"] is False         # bench batch client stays honest
    batch = dict(doc, providers=dict(doc["providers"], stt="openai-gpt-4o-transcribe"))
    cfg2 = cascade_config.build_cascade_config(batch, _registry(), {})
    assert cfg2["stt"]["wired"] is True
    assert cfg2["stt"]["wired_live"] is False


def test_keyterms_knob_flows_through_the_builder_verbatim():
    """s8 c4: profile knobs.keyterms → stt.keyterms verbatim; absent → empty list."""
    doc = {"id": "kt", "pipeline": "cascade",
           "knobs": {"keyterms": ["Hermes", "Robot"]},
           "providers": {"stt": "deepgram", "llm": "openrouter", "tts": "elevenlabs"}}
    cfg = cascade_config.build_cascade_config(doc, _registry(), {})
    assert cfg["stt"]["keyterms"] == ["Hermes", "Robot"]
    bare = {"id": "bare", "pipeline": "cascade",
            "providers": {"stt": "deepgram", "llm": "openrouter", "tts": "elevenlabs"}}
    assert cascade_config.build_cascade_config(bare, _registry(), {})["stt"]["keyterms"] == []


def test_live_base_prompt_composes_via_the_same_helper():
    doc = {"id": "p", "pipeline": "cascade", "persona": "Speak like a pirate.",
           "providers": {"stt": "deepgram", "llm": "openrouter", "tts": "elevenlabs"}}
    cfg = cascade_config.build_cascade_config(doc, _registry(), {},
                                              base_prompt="LIVE BASE")
    assert cfg["llm"]["system_prompt"].startswith("LIVE BASE")
    assert "Speak like a pirate." in cfg["llm"]["system_prompt"]
