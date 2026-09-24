"""Shared builders for the s1 voice-profile tests (Mode V).

Keeps three things in ONE place: tmp config-dir assembly, minimal/valid profile docs,
the fixture registry used by the precedence tests, and the golden-mutation helper that
proves "only these fields changed" BYTE-level (load golden -> mutate the expected
fields -> re-dumps with the same default serialization -> compare full strings).
"""
import asyncio
import copy
import json
from pathlib import Path

import yaml

TESTS = Path(__file__).resolve().parent
SERVICE_DIR = TESTS.parent
SERVICES = SERVICE_DIR.parent
CANONICAL = SERVICES / "voice-config"
SAMPLE = TESTS / "fixtures" / "agent_realtime_sample.yaml"

MINIMAL_PROFILE = {
    "id": "t-agent",
    "pipeline": "realtime",
    "providers": {"realtime": "openai-gpt-realtime"},
}

# s15b-A: the provider roles each pipeline OWNS. A doc that claims one pipeline while
# carrying the other's roles is the defect class that shipped the s14b crash -- cascade
# in NAME, realtime in SHAPE -- so the helper refuses to build one.
PIPELINE_PROVIDER_ROLES = {
    "realtime": ("realtime",),
    "cascade": ("stt", "llm", "tts"),
}
CASCADE_PROVIDERS = {"stt": "deepgram", "llm": "nvidia-nemotron", "tts": "elevenlabs"}


def profile_doc(**overrides) -> dict:
    """Build a minimal valid profile doc, PIPELINE-AWARE.

    s15b-A: this used to be ``doc.update(overrides)``, a shallow merge -- so
    ``profile_doc(pipeline="cascade")`` renamed the pipeline and left
    ``providers.realtime`` in place. Every cascade fixture in the suite was therefore
    realtime-shaped, and the cascade branch of the product was unreachable in test.
    Three separate defects shipped through that hole.

    Now: switching the pipeline switches the providers block with it, and an explicit
    ``providers=`` that does not match the pipeline's roles is a LOUD error here rather
    than a misleading doc handed to the product.

    ``allow_invalid_providers=True`` is the DELIBERATE-poison escape hatch, for the
    negative controls that must build a malformed doc to prove the product rejects it
    (REJECTION_CASES, INLINE_SECRET_CASES). It is spelled out at the call site on
    purpose: accidental poison is now impossible, deliberate poison is self-declaring.
    """
    allow_invalid = overrides.pop("allow_invalid_providers", False)
    doc = copy.deepcopy(MINIMAL_PROFILE)
    pipeline = overrides.get("pipeline", doc["pipeline"])
    roles = PIPELINE_PROVIDER_ROLES.get(pipeline)

    if "providers" in overrides and not allow_invalid:
        given = overrides["providers"]
        if roles is not None and isinstance(given, dict) and set(given) != set(roles):
            raise ValueError(
                f"profile_doc: pipeline {pipeline!r} owns providers {sorted(roles)}, "
                f"got {sorted(given)} — a doc that claims one pipeline while carrying "
                f"another's roles is the s14b defect class. Pass a complete providers "
                f"block for {pipeline!r}, or omit providers= to take the default.")
    elif pipeline == "cascade":
        doc["providers"] = dict(CASCADE_PROVIDERS)

    doc.update(overrides)
    return doc


def write_config_dir(tmp_path, docs, registry_text=None, name="vcfg") -> Path:
    """Assemble a config dir: agents/*.yaml from dicts or (filename, raw-text) pairs,
    plus an optional providers.yaml (else the canonical registry is the fallback)."""
    d = tmp_path / name
    (d / "agents").mkdir(parents=True)
    for i, item in enumerate(docs):
        if isinstance(item, tuple):
            fname, text = item
            (d / "agents" / fname).write_text(text)
        else:
            (d / "agents" / f"agent{i}.yaml").write_text(yaml.safe_dump(item))
    if registry_text is not None:
        (d / "providers.yaml").write_text(registry_text)
    return d


def fixture_registry(realtime_default_knobs: dict) -> str:
    """Minimal schema-valid registry whose openai-gpt-realtime default_knobs the
    precedence tests control (c11 fill level / c12 inertness)."""
    return yaml.safe_dump({"providers": [
        {"id": "openai-gpt-realtime", "role": "realtime",
         "display_name": "OpenAI GPT Realtime", "secret_env": "OPENAI_API_KEY",
         "capabilities": ["speech-to-speech"], "default_knobs": realtime_default_knobs},
        {"id": "google-gemini-live", "role": "realtime",
         "display_name": "Google Gemini Live", "secret_env": "GOOGLE_API_KEY",
         "capabilities": ["speech-to-speech"], "default_knobs": {}},
        {"id": "nvidia-nemotron", "role": "llm", "display_name": "NVIDIA Nemotron",
         "secret_env": "NVIDIA_API_KEY", "capabilities": ["chat"], "default_knobs": {}},
    ]})


# The c06 rejection matrix: (case-name, profile-doc overrides, expected field path).
# Shared by test_profile_schema (actionable errors) and test_loader_no_drift (both
# services' loaders must produce identical outcomes on the same fixture set).
REJECTION_CASES = [
    # s15b-A: the providers-bearing cases declare allow_invalid_providers -- they are
    # DELIBERATE poison whose whole point is that the product rejects them. The helper
    # would otherwise refuse to build them, which is correct for accidental poison.
    ("unknown-llm-id",
     {"providers": {"realtime": "openai-gpt-realtime", "llm": "no-such-provider"},
      "allow_invalid_providers": True},
     "providers.llm"),
    ("role-mismatch-tts",
     {"providers": {"realtime": "openai-gpt-realtime", "tts": "nvidia-nemotron"},
      "allow_invalid_providers": True},
     "providers.tts"),
    ("bad-direction-enum", {"direction": "sideways"}, "direction"),
    ("vad-silence-wrong-type", {"knobs": {"vad": {"silence_ms": "fast"}}},
     "knobs.vad.silence_ms"),
    ("unknown-top-level-key", {"surprise_key": 1}, "surprise_key"),
    ("realtime-provider-missing",
     {"providers": {}, "allow_invalid_providers": True}, "providers.realtime"),
    ("realtime-provider-null", {"providers": {"realtime": None}}, "providers.realtime"),
]


def build_payload_and_url(*, outbound=False):
    """One session.update + dial URL through the CURRENT product code (fake ws)."""
    import config
    import outbound as outbound_mod
    import realtime_bridge
    from approval import ApprovalStore
    from parity_env import BASE_PROMPT, RecordingWS

    cfg = config.load()
    mission = outbound_mod.OutboundMission(brief="parity mission") if outbound else None
    bridge = realtime_bridge.RealtimeBridge(
        cfg, BASE_PROMPT, ApprovalStore(),
        token_ctx={"token": "t", "caller": "c"}, mission=mission)
    ws = RecordingWS()
    asyncio.run(bridge._send_session_update(ws))
    return ws.raw[0], realtime_bridge.realtime_url(cfg)


def mutated_golden(golden_raw: str, *, voice=None, vad=None, persona=None,
                   transcription=None) -> str:
    """Expected payload bytes: the golden with ONLY the named fields changed.

    Asserts the golden round-trips byte-identically first, so a json.loads/dumps cycle
    provably cannot mask drift anywhere else in the payload (D5 guard). ``vad`` is
    (threshold, prefix_padding_ms, silence_duration_ms).
    """
    assert json.dumps(json.loads(golden_raw)) == golden_raw, \
        "golden does not round-trip — mutation technique would be unsound"
    doc = json.loads(golden_raw)
    session = doc["session"]
    if voice is not None:
        session["audio"]["output"]["voice"] = voice
    if vad is not None:
        td = session["audio"]["input"]["turn_detection"]
        td["threshold"], td["prefix_padding_ms"], td["silence_duration_ms"] = vad
    if persona is not None:
        session["instructions"] = f"{session['instructions']}\n\n{persona}"
    if transcription is not None:
        session["audio"]["input"]["transcription"]["model"] = transcription
    return json.dumps(doc)
