"""s9 — OpenCode Zen (opencodego) provider pins (c1/c2/c3).

c1: registry entry (minimax-m3 default, NO json-mode capability) + the config builder
    NEVER attaches response_format for opencodego (Zen's response_format is decorative,
    ~35% non-compliant — trusting it would silently corrupt).
c2: opencodego resolves to its OWN Zen endpoint with the registry-default model,
    verbatim — no host/model remap.
c3 (config half): the CF-1010 browser UA is attached to opencodego's config from the
    ONE shared table (cascade_config.LLM_EXTRA_HEADERS) and to no other provider.
    c3 (probe half): the opencodego probe carries the browser UA while the glm probe
    keeps the `hermes-voice-control-probe/*` tag.

Ticket 15 deleted the bench, and the three nodes here that proved c2/c3 by DISPATCHING a
bench session with it. The wire half of c3 was never the bench's to prove anyway: the
LIVE lane asserts it in
``services/voice/tests/test_cascade_live.py::test_opencodego_live_llm_request_carries_browser_ua``,
which drives the code a real call runs.
"""
import httpx
import pytest

from voicecore import cascade_config
from voicecore import probes
from voicecore import profiles
from conftest import CANONICAL_DIR, RecordingTransport

REGISTRY = profiles.load_registry(CANONICAL_DIR)

ENV = {"OPENCODE_GO_API_KEY": "zen-key", "OPENROUTER_API_KEY": "or-key",
       "OPENAI_API_KEY": "sk-openai", "ELEVENLABS_API_KEY": "el-key",
       "DEEPGRAM_API_KEY": "dg-key"}

def cascade_doc(llm="opencodego"):
    return {"id": "s9-agent", "pipeline": "cascade",
            "providers": {"stt": "openai-gpt-4o-transcribe", "llm": llm,
                          "tts": "elevenlabs"}}


# ---------------------------------------------------------------- c1 --------

def test_registry_entry_defaults_and_no_json_mode():
    e = REGISTRY["opencodego"]
    assert e["role"] == "llm"
    assert e["secret_env"] == "OPENCODE_GO_API_KEY"
    assert e["default_knobs"]["model"] == "minimax-m3"
    # Zen's response_format is decorative — the entry must NOT advertise json-mode.
    assert "json-mode" not in e["capabilities"]
    assert "chat" in e["capabilities"]


def test_config_builder_attaches_no_response_format():
    """The shared builder puts NO response_format in opencodego's extra_body (the only
    channel the cascade body would pick one up from)."""
    cfg = cascade_config.build_cascade_config(cascade_doc(), REGISTRY, ENV)
    assert "response_format" not in (cfg["llm"].get("extra_body") or {})


# ---------------------------------------------------------------- c2 --------

def test_opencodego_resolves_verbatim_with_no_response_format():
    """Endpoint and model as literals, and nothing that could become a
    response_format on the wire. The literals are written out rather than read
    back from ``LLM_ENDPOINTS`` so this is not the code agreeing with itself."""
    llm = cascade_config.build_cascade_config(cascade_doc(), REGISTRY, ENV)["llm"]
    assert llm["endpoint"] == "https://opencode.ai/zen/go/v1/chat/completions"
    assert llm["model"] == "minimax-m3"                  # registry default verbatim
    assert "response_format" not in (llm.get("extra_body") or {})


# ---------------------------------------------------------------- c3 --------

def test_browser_ua_is_attached_to_opencodego_only():
    zen = cascade_config.build_cascade_config(
        cascade_doc(llm="opencodego"), REGISTRY, ENV)["llm"]
    assert zen["extra_headers"] == {"User-Agent": cascade_config.BROWSER_UA}
    # Control: openrouter gets NO injected browser UA.
    other = cascade_config.build_cascade_config(
        cascade_doc(llm="openrouter"), REGISTRY, ENV)["llm"]
    assert other["extra_headers"] == {}


# ---------------------------------------------------- reasoning-strip -------

@pytest.mark.parametrize("raw,expected", [
    ("<think>2+2 is 4</think>\nFour.", "Four."),
    ("<think>reasoning\nmulti-line</think>Hello, my friend!", "Hello, my friend!"),
    ("no tags here", "no tags here"),
    ("<think>cut off mid-thought with no close", ""),   # truncated tail dropped
    ("Answer.<think>late thought</think>", "Answer."),
    (None, None),
])
def test_strip_reasoning_removes_think_blocks(raw, expected):
    assert cascade_config.strip_reasoning(raw) == expected


async def test_probe_opencodego_browser_ua_glm_keeps_probe_tag(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "zen-key")
    monkeypatch.setenv("ZHIPU_API_KEY", "zh-key")

    async def handler(request):
        return httpx.Response(200, json={"ok": True})

    transport = RecordingTransport(handler)
    results = await probes.probe_batch(
        [REGISTRY["opencodego"], REGISTRY["glm"]], transport=transport)

    assert results["opencodego"].status == "ready"       # POST /chat/completions 200
    assert results["glm"].status == "ready"
    zen = [c for c in transport.calls if c.url.host == "opencode.ai"]
    glm = [c for c in transport.calls if c.url.host == "open.bigmodel.cn"]
    assert zen and zen[0].method == "POST"               # credentialed POST, not GET /models
    assert zen[0].headers["user-agent"] == cascade_config.BROWSER_UA
    assert glm and glm[0].headers["user-agent"].startswith("hermes-voice-control-probe/")
