"""s1 profile-overlay behavior tests, Mode V (c08–c14, c23–c29).

Everything runs offline: payloads are captured through a fake ws recorder; a
socket-level guard proves profile LOADING never dials out. Byte-level "nothing else
changed" claims go through profile_helpers.mutated_golden (golden -> mutate exactly
the expected fields -> full-string compare)."""
import json
import shutil
import socket
import subprocess
import sys
from dataclasses import replace

import pytest
import yaml

import config
import parity_env as pe
from voicecore import profiles
from profile_helpers import (
    CANONICAL, REJECTION_CASES, SAMPLE, SERVICE_DIR,
    build_payload_and_url, fixture_registry,
    mutated_golden, profile_doc, write_config_dir,
)

MV_DEFAULT_URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime-2"


@pytest.fixture
def clean_env(monkeypatch):
    """Scrub every config env var so tests control the whole precedence chain."""
    for var in pe.CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def activate(monkeypatch, config_dir, agent_id="t-agent"):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("VOICE_AGENT", agent_id)


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
def test_validate_only_fields_inert(clean_env, monkeypatch, tmp_path, field, value_a, value_b):
    """Two profiles differing ONLY in a validate-only field: byte-identical payloads,
    identical URLs, and no network call of any kind during load."""
    def no_network(*a, **kw):
        raise AssertionError("network access during profile load/build")
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)

    captured = []
    for value in (value_a, value_b):
        d = write_config_dir(tmp_path, [profile_doc(**{field: value})],
                             name=f"vcfg-{len(captured)}")
        activate(monkeypatch, d)
        captured.append(build_payload_and_url())
    (raw_a, url_a), (raw_b, url_b) = captured
    assert raw_a == raw_b, f"payload changed when only {field} differed"
    assert url_a == url_b


# -- c09: disabled profile fails loud -----------------------------------------

def test_disabled_profile_rejected(clean_env, monkeypatch, tmp_path):
    d = write_config_dir(tmp_path, [profile_doc(enabled=False)])
    activate(monkeypatch, d)
    with pytest.raises(profiles.ProfileError) as exc:
        config.load()
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
def test_precedence_chain(clean_env, monkeypatch, tmp_path, level,
                          profile_voice, env_voice, registry_voice, expected):
    doc = profile_doc()
    if profile_voice is not None:
        doc["knobs"] = {"voice": profile_voice}
    registry = fixture_registry({"voice": registry_voice} if registry_voice else {})
    d = write_config_dir(tmp_path, [doc], registry_text=registry)
    if env_voice is not None:
        monkeypatch.setenv("OPENAI_VOICE", env_voice)
    activate(monkeypatch, d)
    cfg = config.load()
    assert cfg.openai_voice == expected
    raw, _ = build_payload_and_url()
    assert json.loads(raw)["session"]["audio"]["output"]["voice"] == expected


# -- c12: registry defaults never apply without a profile ---------------------

def test_registry_defaults_inert_without_profile(clean_env, monkeypatch, tmp_path):
    d = write_config_dir(tmp_path, [profile_doc()],
                         registry_text=fixture_registry({"voice": "verse"}))
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(d))  # registry present, NO VOICE_AGENT
    assert pe.config_snapshot() == pe.load_golden_json("golden_mv_config_defaults.json")
    raw, url = build_payload_and_url()
    assert raw == pe.load_golden("golden_mv_session_inbound.json")
    assert url == MV_DEFAULT_URL


# -- c13: partial overlay leaves every other Config field untouched -----------

PARTIAL = [
    ("voice-only", {"voice": "marin"}, {"openai_voice": "marin"}),
    ("vad-only", {"vad": {"silence_ms": 400, "threshold": 0.6, "prefix_padding_ms": 200}},
     {"vad_silence_ms": 400}),
    ("model-only", {"model": "gpt-realtime-2026"}, {"openai_model": "gpt-realtime-2026"}),
]


@pytest.mark.parametrize("name,knobs,changed", PARTIAL, ids=[c[0] for c in PARTIAL])
def test_partial_overlay_leaves_rest_untouched(clean_env, monkeypatch, tmp_path,
                                               name, knobs, changed):
    baseline = config.load()  # env-derived, no profile selected
    d = write_config_dir(tmp_path, [profile_doc(knobs=knobs)])
    activate(monkeypatch, d)
    cfg = config.load()
    for field, expected in changed.items():
        assert getattr(cfg, field) == expected
    # Dataclass equality on EVERY other field: revert only the changed ones.
    assert replace(cfg, **{f: getattr(baseline, f) for f in changed}) == baseline


# -- c23: the full override fixture changes exactly model/voice/vad -----------

def test_yaml_overrides_land_in_payload(clean_env, monkeypatch, tmp_path):
    d = tmp_path / "vcfg"
    (d / "agents").mkdir(parents=True)
    shutil.copy(SAMPLE, d / "agents" / "sample.yaml")
    activate(monkeypatch, d, agent_id="sample-realtime")
    raw, url = build_payload_and_url()
    assert url == "wss://api.openai.com/v1/realtime?model=gpt-realtime-2026"
    golden = pe.load_golden("golden_mv_session_inbound.json")
    assert raw == mutated_golden(golden, voice="marin", vad=(0.6, 200, 400))


# -- c24: single-field overlays change only their mapped element --------------

SINGLE_FIELD = [
    ("persona-only", {"persona": "Speak like a pirate."},
     {"persona": "Speak like a pirate."}, MV_DEFAULT_URL),
    ("vad-only", {"knobs": {"vad": {"silence_ms": 400, "threshold": 0.6,
                                    "prefix_padding_ms": 200}}},
     {"vad": (0.6, 200, 400)}, MV_DEFAULT_URL),
    ("voice-only", {"knobs": {"voice": "marin"}}, {"voice": "marin"}, MV_DEFAULT_URL),
    ("model-only", {"knobs": {"model": "gpt-realtime-2026"}}, {},
     "wss://api.openai.com/v1/realtime?model=gpt-realtime-2026"),
]


@pytest.mark.parametrize("name,overrides,mutation,expected_url",
                         SINGLE_FIELD, ids=[c[0] for c in SINGLE_FIELD])
def test_single_field_overlay_payload(clean_env, monkeypatch, tmp_path,
                                      name, overrides, mutation, expected_url):
    d = write_config_dir(tmp_path, [profile_doc(**overrides)])
    activate(monkeypatch, d)
    raw, url = build_payload_and_url()
    assert url == expected_url
    golden = pe.load_golden("golden_mv_session_inbound.json")
    assert raw == mutated_golden(golden, **mutation)


# -- c25: persona composition, exact bytes ------------------------------------

def test_persona_composition_exact(clean_env, monkeypatch, tmp_path):
    persona = "Answer in one short sentence.\nAlways stay dry and factual."
    d = write_config_dir(tmp_path, [profile_doc(persona=persona)])
    activate(monkeypatch, d)
    raw, _ = build_payload_and_url()
    assert json.loads(raw)["session"]["instructions"] == \
        pe.BASE_PROMPT + "\n\n" + persona

    d2 = write_config_dir(tmp_path, [profile_doc()], name="vcfg-nopersona")
    activate(monkeypatch, d2)
    raw2, _ = build_payload_and_url()
    assert json.loads(raw2)["session"]["instructions"] == pe.BASE_PROMPT


# -- c26: transcription-model knob --------------------------------------------

def test_transcription_model_override(clean_env, monkeypatch, tmp_path):
    d = write_config_dir(tmp_path, [profile_doc(knobs={"transcription_model": "whisper-1"})])
    activate(monkeypatch, d)
    raw, _ = build_payload_and_url()
    golden = pe.load_golden("golden_mv_session_inbound.json")
    assert raw == mutated_golden(golden, transcription="whisper-1")

    # No profile: env then coded default (the golden pins gpt-4o-transcribe).
    monkeypatch.delenv("VOICE_AGENT", raising=False)
    monkeypatch.setenv("VOICE_TRANSCRIPTION_MODEL", "custom-stt")
    raw_env, _ = build_payload_and_url()
    assert json.loads(raw_env)["session"]["audio"]["input"]["transcription"]["model"] \
        == "custom-stt"
    assert json.loads(golden)["session"]["audio"]["input"]["transcription"]["model"] \
        == "gpt-4o-transcribe"


# -- c27/c28: unimplemented lanes fail loud -----------------------------------

def test_non_openai_realtime_rejected_in_s1(clean_env, monkeypatch, tmp_path):
    d = write_config_dir(
        tmp_path, [profile_doc(providers={"realtime": "google-gemini-live"})])
    activate(monkeypatch, d)
    with pytest.raises(profiles.ProfileError) as exc:
        config.load()
    msg = str(exc.value)
    assert "google-gemini-live" in msg and "not implemented in s1" in msg


def test_cascade_rejected(clean_env, monkeypatch, tmp_path):
    """Legacy posture: a bridge that does NOT flip CASCADE_OUTBOUND_HOST refuses cascade
    wholesale. Flag pinned False explicitly — server.py's import (s12b) flips the process
    global True, so this arm must not depend on collection-time import order."""
    d = write_config_dir(tmp_path, [profile_doc(pipeline="cascade")])
    activate(monkeypatch, d)
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {})
    with pytest.raises(profiles.ProfileError) as exc:
        config.load()
    assert "cascade is not implemented in this bridge" in str(exc.value)


def test_cascade_inbound_refused_on_host(clean_env, monkeypatch, tmp_path):
    """s12b: the Talk bridge now HOSTS cascade (server flips the flag True at import), but
    INBOUND cascade is still refused — the inbound path stays realtime (D3)."""
    d = write_config_dir(tmp_path, [profile_doc(pipeline="cascade")])
    activate(monkeypatch, d)
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {"talk": frozenset({"outbound", "inbound"})})
    with pytest.raises(profiles.ProfileError) as exc:
        config.load()                       # config.load resolves INBOUND
    assert "outbound-only" in str(exc.value)


def test_cascade_outbound_activates_on_host(clean_env, monkeypatch, tmp_path):
    """s12b: an OUTBOUND cascade profile ACTIVATES on the Talk bridge (flag True)."""
    d = write_config_dir(tmp_path, [profile_doc(pipeline="cascade")])
    monkeypatch.setattr(profiles, "CASCADE_CAPABILITY", {"talk": frozenset({"outbound", "inbound"})})
    prof = profiles.load_effective_profile(
        "outbound", env={"VOICE_CONFIG_DIR": str(d), "VOICE_AGENT": "t-agent"})
    assert prof is not None and prof.pipeline == "cascade"


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
def test_loud_fail_matrix(clean_env, monkeypatch, tmp_path, name, setup):
    cfg_dir, agent_id = setup(tmp_path)
    if cfg_dir is not None:
        monkeypatch.setenv("VOICE_CONFIG_DIR", cfg_dir)
    monkeypatch.setenv("VOICE_AGENT", agent_id)
    with pytest.raises(profiles.ProfileError) as exc:
        config.load()
    msg = str(exc.value)
    assert agent_id in msg, f"error must name the agent id: {msg}"
    searched = cfg_dir if cfg_dir is not None else str(profiles.CANONICAL_CONFIG_DIR)
    assert searched in msg, f"error must name the searched dir: {msg}"
