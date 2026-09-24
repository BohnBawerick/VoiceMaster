"""s7p — provider expansion pins (c1/c2/c3/c5/c7).

c2: the four new OpenAI-compat LLM providers resolve to their OWN endpoints with the
REGISTRY default model - asserted with LITERAL expected values, never against the same
dict the code reads (no tautology).
c3: per-provider TTS config defaults (Aura vs ElevenLabs vocabularies never cross).
c1: registry completeness — every live-wired provider has working defaults.
c5: the voices endpoint (account-fetched / curated / honest unavailable).
c7: PUT /api/active refuses an inbound cascade activation.

**Ticket 15 deleted the bench**, and with it the four nodes here that proved these
properties by DISPATCHING a bench session and reading the recorded request. What
survives the bench is ``voicecore.cascade_config.build_cascade_config`` - the ONE
builder the live lane consumes - so c2/c3 are asserted against the config it produces.
The one property that was only ever proven on the wire, that ``extra_body`` reaches the
outgoing chat request, moved to the LIVE lane where it belongs:
``services/voice/tests/test_cascade_live.py::test_live_llm_request_carries_the_extra_body``.
"""
import json

import httpx
import pytest
import yaml
from starlette.testclient import TestClient

from voicecore import cascade_config
from voicecore import profiles
from conftest import CANONICAL_DIR, RecordingTransport, SentinelTransport

REGISTRY = profiles.load_registry(CANONICAL_DIR)

ENV = {
    "OPENAI_API_KEY": "sk-openai", "NVIDIA_API_KEY": "nvapi-key",
    "OPENROUTER_API_KEY": "sk-or-key", "ELEVENLABS_API_KEY": "el-key",
    "DEEPGRAM_API_KEY": "dg-key", "XAI_API_KEY": "xai-key",
    "GOOGLE_API_KEY": "goog-key", "ZHIPU_API_KEY": "zh-key",
}

def cascade_doc(*, stt="openai-gpt-4o-transcribe", llm="openrouter",
                tts="elevenlabs", knobs=None, **over):
    doc = {"id": "s7p-agent", "pipeline": "cascade",
           "providers": {"stt": stt, "llm": llm, "tts": tts}}
    if knobs is not None:
        doc["knobs"] = knobs
    doc.update(over)
    return doc


# ---------------------------------------------------------------------------
# c2 - new LLM providers resolve verbatim (URL + registry-default model literals)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pid,url,model", [
    ("grok", "https://api.x.ai/v1/chat/completions",
     "grok-4-fast-non-reasoning"),
    ("gpt-4.1", "https://api.openai.com/v1/chat/completions", "gpt-4.1"),
    ("gemini-2.5-flash",
     "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
     "gemini-flash-latest"),
    ("glm", "https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-4.6"),
])
def test_new_llm_provider_resolves_verbatim(pid, url, model):
    """The endpoint and model the live lane will dial, from the ONE builder.

    The expected values are literals on purpose: reading them back out of
    ``cascade_config.LLM_ENDPOINTS`` would assert the code against itself.
    """
    llm = cascade_config.build_cascade_config(
        cascade_doc(llm=pid), REGISTRY, ENV)["llm"]
    assert llm["endpoint"] == url                             # literal, no remap
    assert llm["model"] == model                              # registry default verbatim


def test_gemini_gets_reasoning_effort_low():
    """gemini-flash-latest thinks by default and burns the token budget on hidden
    reasoning - the builder must pin reasoning_effort: low for it and nothing else.

    ``cascade_live._llm_round`` merges ``extra_body`` into the outgoing chat request;
    that half is asserted on the wire in
    ``services/voice/tests/test_cascade_live.py::test_live_llm_request_carries_the_extra_body``.
    """
    gemini = cascade_config.build_cascade_config(
        cascade_doc(llm="gemini-2.5-flash"), REGISTRY, ENV)["llm"]
    assert gemini["extra_body"] == {"reasoning_effort": "low"}
    # ...and ONLY gemini gets it.
    other = cascade_config.build_cascade_config(
        cascade_doc(llm="gpt-4.1"), REGISTRY, ENV)["llm"]
    assert other["extra_body"] == {}


# ---------------------------------------------------------------------------
# c3 - per-provider TTS config defaults
# ---------------------------------------------------------------------------

def test_aura_config_defaults_and_greyed_knobs():
    cfg = cascade_config.build_cascade_config(
        cascade_doc(tts="deepgram-aura"), REGISTRY, ENV)
    assert cfg["tts"]["voice"] == "aura-2-thalia-en"
    assert cfg["tts"]["voice_source"] == "registry"
    assert cfg["tts"]["wired_live"] is True
    assert cfg["tts"]["speed"] is None                        # Aura has no speed control
    assert cfg["tts"]["model"] is None                        # model_id is ElevenLabs-only
    assert cfg["tts"]["format"] == "mp3"


def test_elevenlabs_env_voice_never_fills_aura():
    """ELEVENLABS_VOICE_ID is ElevenLabs-shaped — it must not leak into an Aura config."""
    env = dict(ENV, ELEVENLABS_VOICE_ID="elevenVoiceXYZ")
    cfg = cascade_config.build_cascade_config(cascade_doc(tts="deepgram-aura"), REGISTRY, env)
    assert cfg["tts"]["voice"] == "aura-2-thalia-en"
    cfg2 = cascade_config.build_cascade_config(cascade_doc(tts="elevenlabs"), REGISTRY, env)
    assert cfg2["tts"]["voice"] == "elevenVoiceXYZ"           # env still wins for elevenlabs


def test_elevenlabs_registry_voice_fills_when_env_unset():
    """c1: a fresh env (no ELEVENLABS_VOICE_ID) still resolves a voice — the registry
    default — so a zero-edit prefilled agent can actually speak."""
    cfg = cascade_config.build_cascade_config(cascade_doc(tts="elevenlabs"), REGISTRY, ENV)
    assert cfg["tts"]["voice"] == "21m00Tcm4TlvDq8ikWAM"
    assert cfg["tts"]["voice_source"] == "registry"


# ---------------------------------------------------------------------------
# c1 — registry completeness for everything wired
# ---------------------------------------------------------------------------

def test_every_wired_llm_has_a_default_model():
    # VC24: the Hermes stage is the one wired llm with neither. Its endpoint is the
    # Agent's own hermes_profile gateway and its model is the profile's own; a default
    # here would override the profile's model chain for every Agent that never chose one.
    vendors = cascade_config.CASCADE_WIRING["llm"] - {profiles.HERMES_LLM_PROVIDER}
    assert vendors != cascade_config.CASCADE_WIRING["llm"]
    assert not (REGISTRY[profiles.HERMES_LLM_PROVIDER].get("default_knobs") or {}).get("model")
    for pid in vendors:
        dk = REGISTRY[pid].get("default_knobs") or {}
        assert dk.get("model"), f"llm '{pid}' is wired but has no registry default model"
        assert pid in cascade_config.LLM_ENDPOINTS, f"llm '{pid}' wired without endpoint"


def test_every_live_tts_has_a_default_voice():
    for pid in cascade_config.CASCADE_WIRING["tts_live"]:
        dk = REGISTRY[pid].get("default_knobs") or {}
        assert dk.get("voice"), f"tts '{pid}' is live-wired but has no default voice"


def test_live_stt_has_model_and_language_defaults():
    # Ticket 21: English, not `multi`, which heard "fourteen six" as "Diez dos".
    dk = REGISTRY["deepgram"].get("default_knobs") or {}
    assert dk.get("model") == "nova-3"
    assert dk.get("language") == "en"
    dk = REGISTRY["elevenlabs-scribe"].get("default_knobs") or {}
    assert dk.get("model") == "scribe_v2_realtime"
    assert dk.get("language") == "en"


# ---------------------------------------------------------------------------
# c5 — voices endpoint
# ---------------------------------------------------------------------------

def _client(make_app, transport):
    return TestClient(make_app(transport))


def test_aura_voices_curated_with_default(make_app, monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(CANONICAL_DIR.parent / "voice-config"))
    client = _client(make_app, SentinelTransport())     # curated list: zero network
    r = client.get("/api/providers/deepgram-aura/voices")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "curated"
    assert body["default"] == "aura-2-thalia-en"
    ids = [v["id"] for v in body["voices"]]
    assert "aura-2-thalia-en" in ids and len(ids) >= 10


def test_elevenlabs_voices_fetched_from_account(make_app, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")

    async def handler(request):
        assert request.url.host == "api.elevenlabs.io"
        assert request.headers["xi-api-key"] == "el-key"
        return httpx.Response(200, json={"voices": [
            {"voice_id": "abc123", "name": "Rachel"},
            {"voice_id": "def456", "name": "Josh"}]})

    client = _client(make_app, RecordingTransport(handler))
    body = client.get("/api/providers/elevenlabs/voices").json()
    assert body["source"] == "account"
    assert body["voices"] == [{"id": "abc123", "name": "Rachel"},
                              {"id": "def456", "name": "Josh"}]


def test_elevenlabs_voices_keyless_is_honest_unavailable(make_app, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    client = _client(make_app, SentinelTransport())     # keyless: zero network
    body = client.get("/api/providers/elevenlabs/voices").json()
    assert body["source"] == "unavailable"
    assert body["voices"] is None
    assert "ELEVENLABS_API_KEY" in body["detail"]


def test_elevenlabs_voices_fetch_failure_is_honest_unavailable(make_app, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")

    async def handler(request):
        return httpx.Response(500, json={})

    client = _client(make_app, RecordingTransport(handler))
    body = client.get("/api/providers/elevenlabs/voices").json()
    assert body["source"] == "unavailable" and body["voices"] is None
    assert "HTTP 500" in body["detail"]


def test_voices_unknown_or_non_tts_provider_404s(make_app):
    client = _client(make_app, SentinelTransport())
    assert client.get("/api/providers/nope/voices").status_code == 404
    assert client.get("/api/providers/openrouter/voices").status_code == 404


# ---------------------------------------------------------------------------
# c7 — dry-run live_wiring + inbound-cascade pointer + PUT /api/active refusal
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "agents").mkdir()
    return tmp_path


def test_the_dashboard_grades_cascade_as_the_phone_bridge_does(make_app, cfg_dir):
    """The positive control for the refusal below, and for `app.py`'s own
    ``CASCADE_OUTBOUND_HOST = True``.

    Ticket 15: that flag used to be set by importing ``dryrun``, which only the test
    suite ever did, so this arm passed in pytest while the SHIPPED dashboard refused
    to store a cascade outbound activation the mode-c bridge dials happily. Without
    this node the refusal test below stays green against a dashboard that refuses
    everything.
    """
    doc = cascade_doc(stt="deepgram")
    (cfg_dir / "agents" / "s7p-agent.yaml").write_text(yaml.safe_dump(doc))
    client = _client(make_app, SentinelTransport())
    r = client.put("/api/active", json={"outlets": {
        "phone": {"outbound": "s7p-agent"}}})
    assert r.status_code == 200, r.text
    assert r.json()["outlets"]["phone"]["outbound"] == "s7p-agent"


def test_put_active_refuses_inbound_cascade(make_app, cfg_dir):
    """The dashboard must refuse to STORE an activation every live call would refuse —
    cascade agents cannot be the inbound agent."""
    doc = cascade_doc(stt="deepgram")
    (cfg_dir / "agents" / "s7p-agent.yaml").write_text(yaml.safe_dump(doc))
    client = _client(make_app, SentinelTransport())
    r = client.put("/api/active", json={"outlets": {
        "phone": {"inbound": "s7p-agent"}}})
    assert r.status_code == 422
    assert "outbound-only" in json.dumps(r.json())
    r2 = client.put("/api/active", json={"outlets": {
        "phone": {"outbound": "s7p-agent"}}})
    assert r2.status_code == 200, r2.json()
