"""s1 profile-overlay behavior tests, Mode C (c08–c14, c23–c29).

Mode C's config is module-level constants, so every scenario runs through
parity_env.env_sandbox (reload server.py under a controlled env, restore after).
Byte-level "nothing else changed" claims go through profile_helpers.mutated_golden."""
import json
import shutil
import socket
import subprocess
import sys

import pytest
import yaml

import parity_env as pe
from voicecore import profiles
from profile_helpers import (
    CANONICAL, REJECTION_CASES, SAMPLE, SERVICE_DIR,
    build_payload_and_url, effective_config, fixture_registry,
    mutated_golden, profile_doc, write_config_dir,
)

MC_DEFAULT_URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime"


def activation(config_dir, agent_id="t-agent", **extra):
    env = {"VOICE_CONFIG_DIR": str(config_dir), "VOICE_AGENT": agent_id}
    env.update(extra)
    return env


# -- c08: validate-only fields are runtime-inert ------------------------------

VALIDATE_ONLY = [
    ("hermes_profile", "profile-a", "profile-b"),
    ("direction", "inbound", "outbound"),
    ("number_policy", {"allowed": ["+15550001111"]}, {"allowed": ["+15550002222"]}),
    ("guardrails", {"on_call_tools": True}, {"on_call_tools": False}),
    ("memory", {"retain": True}, {"retain": False}),
]


@pytest.mark.parametrize("field,value_a,value_b",
                         VALIDATE_ONLY, ids=[c[0] for c in VALIDATE_ONLY])
def test_validate_only_fields_inert(monkeypatch, tmp_path, field, value_a, value_b):
    """Two profiles differing ONLY in a validate-only field: byte-identical payloads,
    identical URLs, and no network call of any kind during load/build."""
    def no_network(*a, **kw):
        raise AssertionError("network access during profile load/build")
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)

    captured = []
    for value in (value_a, value_b):
        d = write_config_dir(tmp_path, [profile_doc(**{field: value})],
                             name=f"vcfg-{len(captured)}")
        captured.append(build_payload_and_url(activation(d)))
    (raw_a, url_a), (raw_b, url_b) = captured
    assert raw_a == raw_b, f"payload changed when only {field} differed"
    assert url_a == url_b


# -- c09: disabled profile fails loud -----------------------------------------

def test_disabled_profile_rejected(tmp_path):
    d = write_config_dir(tmp_path, [profile_doc(enabled=False)])
    with pytest.raises(profiles.ProfileError) as exc:
        effective_config(activation(d))
    msg = str(exc.value)
    assert "enabled" in msg and "t-agent" in msg


# -- c10: CLI validation via subprocess ---------------------------------------

def test_cli_validate_exit_codes(tmp_path):
    good = tmp_path / "good"
    (good / "agents").mkdir(parents=True)
    shutil.copy(SAMPLE, good / "agents" / "sample.yaml")
    r = subprocess.run([sys.executable, "-m", "voicecore.profiles", "validate", str(good)],
                       cwd=SERVICE_DIR, capture_output=True, text=True)
    assert r.returncode == 0, f"good dir must validate: {r.stderr}{r.stdout}"

    bad = tmp_path / "bad"
    (bad / "agents").mkdir(parents=True)
    (bad / "agents" / "broken.yaml").write_text(
        yaml.safe_dump(profile_doc(direction="sideways", surprise_key=1)))
    r = subprocess.run([sys.executable, "-m", "voicecore.profiles", "validate", str(bad)],
                       cwd=SERVICE_DIR, capture_output=True, text=True)
    assert r.returncode != 0
    out = r.stderr + r.stdout
    assert "direction" in out and "surprise_key" in out, \
        f"CLI errors must carry c06-style field paths: {out}"


# -- c11: the four precedence levels, on the voice knob -----------------------

PRECEDENCE = [
    ("profile-beats-env", "marin", "echo", "verse", "marin"),
    ("env-beats-registry", None, "echo", "verse", "echo"),
    ("registry-fills-absent-knob", None, None, "verse", "verse"),
    ("coded-default-last", None, None, None, "cedar"),
]


@pytest.mark.parametrize("level,profile_voice,env_voice,registry_voice,expected",
                         PRECEDENCE, ids=[c[0] for c in PRECEDENCE])
def test_precedence_chain(tmp_path, level, profile_voice, env_voice,
                          registry_voice, expected):
    doc = profile_doc()
    if profile_voice is not None:
        doc["knobs"] = {"voice": profile_voice}
    registry = fixture_registry({"voice": registry_voice} if registry_voice else {})
    d = write_config_dir(tmp_path, [doc], registry_text=registry)
    env = activation(d)
    if env_voice is not None:
        env["OPENAI_VOICE"] = env_voice
    assert effective_config(env)["voice"] == expected
    raw, _ = build_payload_and_url(env)
    assert json.loads(raw)["session"]["audio"]["output"]["voice"] == expected


# -- c12: registry defaults never apply without a profile ---------------------

def test_registry_defaults_inert_without_profile(tmp_path):
    d = write_config_dir(tmp_path, [profile_doc()],
                         registry_text=fixture_registry({"voice": "verse"}))
    env = {"VOICE_CONFIG_DIR": str(d)}  # registry present, NO VOICE_AGENT
    with pe.env_sandbox(env) as srv:
        assert pe.config_snapshot(srv) == pe.load_golden_json("golden_mc_config_defaults.json")
        assert pe.capture_session_raw(srv, outbound=False) == \
            pe.load_golden("golden_mc_session_inbound.json")
        assert srv._realtime_url() == MC_DEFAULT_URL


# -- c13: partial overlay leaves every other effective-config key untouched ---

PARTIAL = [
    ("voice-only", {"voice": "marin"}, {"voice": "marin"}),
    ("vad-only", {"vad": {"silence_ms": 400, "threshold": 0.6, "prefix_padding_ms": 200}},
     {"vad_silence_ms": 400, "vad_threshold": 0.6, "vad_prefix_padding_ms": 200}),
    ("model-only", {"model": "gpt-realtime-2026"}, {"model": "gpt-realtime-2026"}),
]


@pytest.mark.parametrize("name,knobs,changed", PARTIAL, ids=[c[0] for c in PARTIAL])
def test_partial_overlay_leaves_rest_untouched(tmp_path, name, knobs, changed):
    baseline = effective_config()  # env-derived, no profile selected
    d = write_config_dir(tmp_path, [profile_doc(knobs=knobs)])
    overlaid = effective_config(activation(d))
    for key, expected in changed.items():
        assert overlaid[key] == expected
    # Equality on EVERY other key of the effective-config dict.
    rest = {k: v for k, v in overlaid.items() if k not in changed}
    assert rest == {k: v for k, v in baseline.items() if k not in changed}


# -- c23: the full override fixture changes exactly model/voice/vad -----------

def test_yaml_overrides_land_in_payload(tmp_path):
    d = tmp_path / "vcfg"
    (d / "agents").mkdir(parents=True)
    shutil.copy(SAMPLE, d / "agents" / "sample.yaml")
    raw, url = build_payload_and_url(activation(d, agent_id="sample-realtime"))
    assert url == "wss://api.openai.com/v1/realtime?model=gpt-realtime-2026"
    golden = pe.load_golden("golden_mc_session_inbound.json")
    assert raw == mutated_golden(golden, voice="marin", vad=(0.6, 200, 400))


# -- c24: single-field overlays change only their mapped element --------------

SINGLE_FIELD = [
    ("persona-only", {"persona": "Speak like a pirate."},
     {"persona": "Speak like a pirate."}, MC_DEFAULT_URL),
    ("vad-only", {"knobs": {"vad": {"silence_ms": 400, "threshold": 0.6,
                                    "prefix_padding_ms": 200}}},
     {"vad": (0.6, 200, 400)}, MC_DEFAULT_URL),
    ("voice-only", {"knobs": {"voice": "marin"}}, {"voice": "marin"}, MC_DEFAULT_URL),
    ("model-only", {"knobs": {"model": "gpt-realtime-2026"}}, {},
     "wss://api.openai.com/v1/realtime?model=gpt-realtime-2026"),
]


@pytest.mark.parametrize("name,overrides,mutation,expected_url",
                         SINGLE_FIELD, ids=[c[0] for c in SINGLE_FIELD])
def test_single_field_overlay_payload(tmp_path, name, overrides, mutation, expected_url):
    d = write_config_dir(tmp_path, [profile_doc(**overrides)])
    raw, url = build_payload_and_url(activation(d))
    assert url == expected_url
    golden = pe.load_golden("golden_mc_session_inbound.json")
    assert raw == mutated_golden(golden, **mutation)


# -- c25: persona composition, exact bytes ------------------------------------

def test_persona_composition_exact(tmp_path):
    persona = "Answer in one short sentence.\nAlways stay dry and factual."
    d = write_config_dir(tmp_path, [profile_doc(persona=persona)])
    raw, _ = build_payload_and_url(activation(d))
    assert json.loads(raw)["session"]["instructions"] == \
        pe.BASE_PROMPT + "\n\n" + persona

    d2 = write_config_dir(tmp_path, [profile_doc()], name="vcfg-nopersona")
    raw2, _ = build_payload_and_url(activation(d2))
    assert json.loads(raw2)["session"]["instructions"] == pe.BASE_PROMPT


# -- c26: transcription-model knob --------------------------------------------

def test_transcription_model_override(tmp_path):
    d = write_config_dir(tmp_path, [profile_doc(knobs={"transcription_model": "whisper-1"})])
    raw, _ = build_payload_and_url(activation(d))
    golden = pe.load_golden("golden_mc_session_inbound.json")
    assert raw == mutated_golden(golden, transcription="whisper-1")

    # No profile: env then coded default (the golden pins gpt-4o-transcribe).
    raw_env, _ = build_payload_and_url({"VOICE_TRANSCRIPTION_MODEL": "custom-stt"})
    assert json.loads(raw_env)["session"]["audio"]["input"]["transcription"]["model"] \
        == "custom-stt"
    assert json.loads(golden)["session"]["audio"]["input"]["transcription"]["model"] \
        == "gpt-4o-transcribe"


# -- c27/c28: unimplemented lanes fail loud -----------------------------------

def test_non_openai_realtime_rejected_in_s1(tmp_path):
    d = write_config_dir(
        tmp_path, [profile_doc(providers={"realtime": "google-gemini-live"})])
    with pytest.raises(profiles.ProfileError) as exc:
        effective_config(activation(d))
    msg = str(exc.value)
    assert "google-gemini-live" in msg and "not implemented in s1" in msg


def test_cascade_rejected(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [profile_doc(pipeline="cascade")])
    # Not a cascade host (the talk bridge posture — mode-c's server import flips the
    # flag, so this arm goes straight through profiles): refused wholesale.
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {})
    with pytest.raises(profiles.ProfileError) as exc:
        profiles.load_effective_profile(
            "inbound", env={"VOICE_CONFIG_DIR": str(d), "VOICE_AGENT": "t-agent"})
    assert "cascade is not implemented in this bridge" in str(exc.value)
    # s7 host (server.py flips the flag at import — effective_config reloads it):
    # INBOUND still refuses — the live DID stays realtime (D3). The realtime lane can
    # never be handed a cascade profile.
    with pytest.raises(profiles.ProfileError) as exc2:
        effective_config(activation(d))
    assert "outbound-only" in str(exc2.value)


def test_cascade_outbound_activates_on_the_host(tmp_path, monkeypatch):
    d = write_config_dir(tmp_path, [profile_doc(pipeline="cascade")])
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {"phone": frozenset({"outbound", "inbound"})})
    prof = profiles.load_effective_profile(
        "outbound", env={"VOICE_CONFIG_DIR": str(d), "VOICE_AGENT": "t-agent"})
    assert prof is not None and prof.pipeline == "cascade"
    # s15b-A a5: cascade-SPECIFIC, not merely "it loaded". The doc that activates must be
    # cascade-SHAPED -- resolving {stt,llm,tts} and carrying no realtime provider. Before
    # s15b-A this fixture was cascade in name and realtime in shape, so this activation
    # was proving nothing about the cascade lane at all.
    assert set(prof.doc["providers"]) == {"stt", "llm", "tts"}
    assert "realtime" not in prof.doc["providers"]


# -- c29: loud-fail matrix with VOICE_AGENT set -------------------------------

def _dir_missing(tmp_path):
    return str(tmp_path / "no-such-dir"), "t-agent"


def _dir_unset(tmp_path):
    return None, "ghost-agent"


def _empty_agents(tmp_path):
    d = tmp_path / "vcfg"
    (d / "agents").mkdir(parents=True)
    return str(d), "t-agent"


def _id_absent(tmp_path):
    d = write_config_dir(tmp_path, [profile_doc(id="someone-else")])
    return str(d), "t-agent"


def _invalid_yaml(tmp_path):
    d = write_config_dir(tmp_path, [("broken.yaml", "id: [unclosed\n  ]]]junk: {")])
    return str(d), "t-agent"


def _traversal_id(tmp_path):
    d = write_config_dir(tmp_path, [profile_doc()])
    return str(d), "../x"


def _duplicate_ids(tmp_path):
    d = write_config_dir(tmp_path, [profile_doc(), profile_doc(description="twin")])
    return str(d), "t-agent"


LOUD_FAIL = [
    ("config-dir-unset", _dir_unset),
    ("config-dir-missing", _dir_missing),
    ("empty-agents-dir", _empty_agents),
    ("agent-id-absent", _id_absent),
    ("invalid-yaml-selected", _invalid_yaml),
    ("path-traversal-id", _traversal_id),
    ("duplicate-agent-ids", _duplicate_ids),
]


@pytest.mark.parametrize("name,setup", LOUD_FAIL, ids=[c[0] for c in LOUD_FAIL])
def test_loud_fail_matrix(tmp_path, name, setup):
    cfg_dir, agent_id = setup(tmp_path)
    env = {"VOICE_AGENT": agent_id}
    if cfg_dir is not None:
        env["VOICE_CONFIG_DIR"] = cfg_dir
    with pytest.raises(profiles.ProfileError) as exc:
        effective_config(env)
    msg = str(exc.value)
    assert agent_id in msg, f"error must name the agent id: {msg}"
    searched = cfg_dir if cfg_dir is not None else str(profiles.CANONICAL_CONFIG_DIR)
    assert searched in msg, f"error must name the searched dir: {msg}"
