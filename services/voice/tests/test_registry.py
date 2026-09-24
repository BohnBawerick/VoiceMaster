"""s1 provider-registry tests (c01-c03): canonical file, full §2.1 menu, and pinned
secret_env NAMES."""
import re

import pytest
import yaml

from voicecore import profiles
from profile_helpers import CANONICAL

# The design-§2.1 menu: exact (id, role) pairs that MUST exist in the registry.
MENU = [
    ("openai-gpt-realtime", "realtime"),
    ("google-gemini-live", "realtime"),
    ("nvidia-nemotron", "llm"),
    ("openrouter", "llm"),
    ("grok", "llm"),
    ("gpt-4.1", "llm"),
    ("gemini-2.5-flash", "llm"),
    ("claude-haiku-4.5", "llm"),
    ("glm", "llm"),
    ("deepgram", "stt"),
    ("soniox", "stt"),
    ("assemblyai", "stt"),
    ("openai-gpt-4o-transcribe", "stt"),
    ("nvidia-stt", "stt"),
    ("elevenlabs", "tts"),
    ("cartesia", "tts"),
    ("deepgram-aura", "tts"),
    ("inworld", "tts"),
]

# c03: pinned secret_env NAME per menu entry.
PINNED_SECRET_ENV = {
    "openai-gpt-realtime": "OPENAI_API_KEY",
    "gpt-4.1": "OPENAI_API_KEY",
    "openai-gpt-4o-transcribe": "OPENAI_API_KEY",
    "google-gemini-live": "GOOGLE_API_KEY",
    "gemini-2.5-flash": "GOOGLE_API_KEY",
    "nvidia-nemotron": "NVIDIA_API_KEY",
    "nvidia-stt": "NVIDIA_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "grok": "XAI_API_KEY",
    "claude-haiku-4.5": "ANTHROPIC_API_KEY",
    "glm": "ZHIPU_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "deepgram-aura": "DEEPGRAM_API_KEY",
    "soniox": "SONIOX_API_KEY",
    "assemblyai": "ASSEMBLYAI_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "cartesia": "CARTESIA_API_KEY",
    "inworld": "INWORLD_API_KEY",
}

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


@pytest.fixture
def no_config_env(monkeypatch):
    monkeypatch.delenv("VOICE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("VOICE_AGENT", raising=False)


def test_registry_canonical_and_well_formed(no_config_env):
    """c01: single canonical file, valid schema, and BOTH services resolve to it."""
    path = profiles.registry_path()
    assert path == CANONICAL / "providers.yaml"
    raw = yaml.safe_load(path.read_text())
    assert isinstance(raw, dict) and isinstance(raw["providers"], list)

    reg = profiles.load_registry()
    assert reg, "registry is empty"
    for pid, entry in reg.items():
        assert isinstance(pid, str) and pid
        assert entry["role"] in ("realtime", "llm", "stt", "tts"), pid
        assert isinstance(entry["display_name"], str) and entry["display_name"], pid
        assert isinstance(entry["secret_env"], str), pid
        assert isinstance(entry["capabilities"], list), pid
        assert isinstance(entry["default_knobs"], dict), pid
        for opt in ("cost_hint", "latency_hint"):
            if opt in entry:
                assert isinstance(entry[opt], str), f"{pid}.{opt}"


@pytest.mark.parametrize("pid,role", MENU)
def test_all_menu_entries_present(no_config_env, pid, role):
    """c02: every §2.1 menu entry exists with its exact role."""
    reg = profiles.load_registry()
    assert pid in reg, f"menu entry '{pid}' missing from providers.yaml"
    assert reg[pid]["role"] == role


def test_secret_env_names_pinned(no_config_env):
    """c03: env-var NAMES only, matching ^[A-Z][A-Z0-9_]*$, pinned per menu entry."""
    reg = profiles.load_registry()
    for pid, env_name in PINNED_SECRET_ENV.items():
        assert reg[pid]["secret_env"] == env_name, pid
    for pid, entry in reg.items():
        assert _ENV_NAME_RE.match(entry["secret_env"]), \
            f"{pid}.secret_env '{entry['secret_env']}' is not an env-var NAME"
