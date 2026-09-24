"""Registry resolution + loud-failure tests (c15-c17 API side)."""
import textwrap

import httpx
import pytest

from voicecore import probes
from voicecore import profiles
from conftest import APP_DIR, CANONICAL_PROVIDERS, RecordingTransport, SentinelTransport

TWO_ENTRY_REGISTRY = textwrap.dedent("""\
    providers:
      - id: custom-openai-llm
        role: llm
        display_name: Custom OpenAI-keyed LLM
        secret_env: OPENAI_API_KEY
        capabilities: [chat]
        default_knobs: {}
      - id: custom-cartesia-tts
        role: tts
        display_name: Custom Cartesia TTS
        secret_env: CARTESIA_API_KEY
        capabilities: [streaming]
        default_knobs: {}
    """)


# ---------------------------------------------------------------------------
# c15 — resolution order both ways (loader-level AND through the API)
# ---------------------------------------------------------------------------

async def test_registry_resolution_order(tmp_path, make_client, monkeypatch):
    # (a) dir-local providers.yaml WINS when present
    local_dir = tmp_path / "with-registry"
    local_dir.mkdir()
    (local_dir / "providers.yaml").write_text(TWO_ENTRY_REGISTRY)
    assert profiles.registry_path(local_dir) == local_dir / "providers.yaml"

    monkeypatch.setenv("VOICE_CONFIG_DIR", str(local_dir))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")

    async def handler(request):
        return httpx.Response(200, json={"ok": True})

    transport = RecordingTransport(handler)
    async with make_client(transport) as client:
        rows = (await client.get("/api/providers")).json()

    assert [row["id"] for row in rows] == ["custom-openai-llm", "custom-cartesia-tts"]
    # ... and the NEW id was probed by the OPENAI_API_KEY family prober (c31 app path)
    assert [req.url.host for req in transport.calls] == ["api.openai.com"]
    assert rows[0]["status"] == "ready"
    assert rows[1]["status"] == "needs_key"

    # (b) canonical fallback when the dir has NO providers.yaml
    empty_dir = tmp_path / "no-registry"
    empty_dir.mkdir()
    assert profiles.registry_path(empty_dir) == \
        profiles.CANONICAL_CONFIG_DIR / "providers.yaml"

    monkeypatch.setenv("VOICE_CONFIG_DIR", str(empty_dir))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)  # branch (b) is keyless
    sentinel = SentinelTransport()
    async with make_client(sentinel) as client:
        rows = (await client.get("/api/providers")).json()

    assert [row["id"] for row in rows] == CANONICAL_PROVIDERS
    assert sentinel.calls == []  # no keys set in this branch


# ---------------------------------------------------------------------------
# c16 — registry failure is a loud 5xx carrying the loader's message,
#        with NO silent fallback to canonical, while healthz stays 200
# ---------------------------------------------------------------------------

BROKEN_CASES = {
    "invalid_yaml": (
        "providers:\n  - id: broken\n    role: [unclosed\n", "invalid YAML"),
    "duplicate_id": (textwrap.dedent("""\
        providers:
          - {id: dup, role: llm, display_name: A, secret_env: OPENAI_API_KEY,
             capabilities: [], default_knobs: {}}
          - {id: dup, role: llm, display_name: B, secret_env: OPENAI_API_KEY,
             capabilities: [], default_knobs: {}}
        """), "duplicate provider id"),
    "missing_secret_env": (textwrap.dedent("""\
        providers:
          - {id: nosecret, role: llm, display_name: A,
             capabilities: [], default_knobs: {}}
        """), "secret_env: required key missing"),
    "bad_role": (textwrap.dedent("""\
        providers:
          - {id: badrole, role: banana, display_name: A, secret_env: OPENAI_API_KEY,
             capabilities: [], default_knobs: {}}
        """), "'banana' not one of"),
}


@pytest.mark.parametrize("case", list(BROKEN_CASES), ids=list(BROKEN_CASES))
async def test_registry_error_is_loud_at_api(case, tmp_path, make_client, monkeypatch):
    yaml_text, expected_fragment = BROKEN_CASES[case]
    (tmp_path / "providers.yaml").write_text(yaml_text)
    monkeypatch.setenv("VOICE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")  # keys present changes nothing

    sentinel = SentinelTransport()
    async with make_client(sentinel) as client:
        response = await client.get("/api/providers")
        assert response.status_code == 500
        body = response.json()
        assert body["error"] == "registry_error"
        assert expected_fragment in body["detail"]  # the loader's own message

        refresh = await client.post("/api/providers/refresh")
        assert refresh.status_code == 500  # refresh is equally loud

        health = await client.get("/healthz")
        assert health.status_code == 200  # health is independent of the registry

    assert sentinel.calls == []  # no probes fired against a broken registry


def test_probe_module_has_no_per_id_dispatch():
    """c31 code-review guard: PROBERS keys are env-var NAMES, not provider ids."""
    for key in probes.PROBERS:
        assert profiles._ENV_NAME_RE.match(key), (
            f"PROBERS key {key!r} is not an env-var name — per-id dispatch forbidden")
    canonical_ids = set(
        profiles.load_registry(APP_DIR.parent / "voice-config").keys())
    assert not (set(probes.PROBERS) & canonical_ids)
