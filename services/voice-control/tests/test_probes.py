"""Probe-layer unit tests (c09-c13, c20, c31) — all transports mocked, no network."""
import asyncio
import time

import httpx
import pytest

from voicecore import probes
from conftest import RecordingTransport, SentinelTransport, entry


# ---------------------------------------------------------------------------
# c09 — absent OR blank key => needs_key with ZERO network attempts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value", [None, "", " \t"], ids=["unset", "empty", "whitespace"])
async def test_absent_or_blank_key_makes_no_network_call(value, monkeypatch):
    if value is None:
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    else:
        monkeypatch.setenv("DEEPGRAM_API_KEY", value)
    sentinel = SentinelTransport()
    fake = entry(id="fake-stt", role="stt", secret_env="DEEPGRAM_API_KEY")

    results = await probes.probe_batch([fake], transport=sentinel)

    assert sentinel.calls == []          # zero network attempts
    result = results["fake-stt"]
    assert result.status == "needs_key"
    assert "DEEPGRAM_API_KEY" in result.detail
    assert result.checked_at is None


async def test_sentinel_trips_on_any_request_positive_control(monkeypatch):
    """Proves the sentinel used above WOULD fail the test on any request."""
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-not-blank")
    sentinel = SentinelTransport()
    fake = entry(id="fake-stt", role="stt", secret_env="DEEPGRAM_API_KEY")

    with pytest.raises(AssertionError, match="unexpected network attempt"):
        await probes.probe_batch([fake], transport=sentinel)
    assert len(sentinel.calls) == 1


# ---------------------------------------------------------------------------
# c10a — bad key => error with the code surfaced, never ready
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", [401, 403])
async def test_probe_401_is_error(code, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-invalid")

    async def handler(request):
        return httpx.Response(code, json={"error": {"message": "bad key"}})

    results = await probes.probe_batch(
        [entry(id="x", secret_env="OPENAI_API_KEY")],
        transport=RecordingTransport(handler))

    result = results["x"]
    assert result.status == "error"
    assert result.status not in ("ready", "needs_key")
    assert str(code) in result.detail
    assert result.http_status == code
    assert result.checked_at is not None


# ---------------------------------------------------------------------------
# c11 — mocked 500 and mocked timeout are honest, specific errors
# ---------------------------------------------------------------------------

async def test_probe_http_500_surfaces_status(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-x")

    async def handler(request):
        return httpx.Response(500, text="upstream exploded")

    results = await probes.probe_batch(
        [entry(id="n", secret_env="NVIDIA_API_KEY")],
        transport=RecordingTransport(handler))

    result = results["n"]
    assert result.status == "error"
    assert "500" in result.detail
    assert result.http_status == 500


async def test_probe_timeout_is_error(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-x")

    async def handler(request):
        raise httpx.ReadTimeout("simulated read timeout")

    results = await probes.probe_batch(
        [entry(id="e", secret_env="ELEVENLABS_API_KEY")],
        transport=RecordingTransport(handler))

    result = results["e"]
    assert result.status == "error"
    assert "timeout" in result.detail.lower()
    assert result.http_status is None


# ---------------------------------------------------------------------------
# c12 — hard per-probe wall bound: a never-responding mock finishes < 6 s
# ---------------------------------------------------------------------------

async def test_hung_probe_times_out_under_6s_wall(monkeypatch):
    assert probes.PROBE_TIMEOUT_S <= 5.0  # contract cap, visible constant
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")

    async def handler(request):
        await asyncio.sleep(60)  # never responds within any sane window
        return httpx.Response(200)

    start = time.monotonic()
    results = await probes.probe_batch(
        [entry(id="hung", secret_env="OPENAI_API_KEY")],
        transport=RecordingTransport(handler))
    elapsed = time.monotonic() - start

    assert elapsed < 6.0
    result = results["hung"]
    assert result.status == "error"
    assert "timeout" in result.detail.lower()


# ---------------------------------------------------------------------------
# c13 — concurrent, not serial: 8 probes sleeping ~1 s finish in < 2.5 s
# ---------------------------------------------------------------------------

async def test_probes_run_concurrently(monkeypatch):
    families = ["OPENAI_API_KEY", "GOOGLE_API_KEY", "NVIDIA_API_KEY",
                "ELEVENLABS_API_KEY", "DEEPGRAM_API_KEY", "XAI_API_KEY",
                "ANTHROPIC_API_KEY", "ZHIPU_API_KEY"]
    entries = []
    for i, fam in enumerate(families):
        monkeypatch.setenv(fam, f"key-{i}")
        entries.append(entry(id=f"p{i}", secret_env=fam))

    async def handler(request):
        await asyncio.sleep(1.0)
        return httpx.Response(200, json={"ok": True})

    start = time.monotonic()
    results = await probes.probe_batch(entries, transport=RecordingTransport(handler))
    elapsed = time.monotonic() - start

    assert elapsed < 2.5, f"batch took {elapsed:.2f}s — serial execution would be ~8s"
    assert len(results) == 8
    assert all(r.status == "ready" for r in results.values())


# ---------------------------------------------------------------------------
# c20 — upstream bodies never reach the detail (even when they echo the key)
# ---------------------------------------------------------------------------

async def test_upstream_body_never_leaks_into_detail(monkeypatch):
    secret = "sk-proj-SUPERSECRETVALUE1234567890"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    async def handler(request):
        # Hostile vendor: echoes the credential straight back in the error body.
        return httpx.Response(
            401, json={"error": {"message": f"invalid key provided: {secret}"}})

    results = await probes.probe_batch(
        [entry(id="x", secret_env="OPENAI_API_KEY")],
        transport=RecordingTransport(handler))

    result = results["x"]
    assert result.status == "error"
    assert "401" in result.detail
    assert secret not in result.detail
    assert secret[:8] not in result.detail


# ---------------------------------------------------------------------------
# c31 — dispatch keyed off secret_env, never provider id
# ---------------------------------------------------------------------------

async def test_new_id_with_known_secret_env_uses_same_prober(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    brand_new = entry(id="my-brand-new-openai-thing", secret_env="OPENAI_API_KEY")

    async def handler(request):
        return httpx.Response(200, json={"ok": True})

    transport = RecordingTransport(handler)
    results = await probes.probe_batch([brand_new], transport=transport)

    assert results["my-brand-new-openai-thing"].status == "ready"
    assert len(transport.calls) == 1
    spec = probes.PROBERS["OPENAI_API_KEY"]
    request = transport.calls[0]
    assert request.url.host == httpx.URL(spec.url).host
    assert request.method == spec.method
    # per-entry tagging: the NEW id rode the same family prober
    assert "my-brand-new-openai-thing" in request.headers["user-agent"]


async def test_unknown_secret_env_with_key_present_is_honest_error(monkeypatch):
    monkeypatch.setenv("SOME_FUTURE_VENDOR_KEY", "zzz")
    sentinel = SentinelTransport()

    results = await probes.probe_batch(
        [entry(id="future", secret_env="SOME_FUTURE_VENDOR_KEY")],
        transport=sentinel)

    assert sentinel.calls == []  # no guessing at endpoints
    result = results["future"]
    assert result.status == "error"
    assert "no prober" in result.detail


# ---------------------------------------------------------------------------
# c4 — OpenRouter probes the AUTH-GATED /api/v1/key, never the public /models
# ---------------------------------------------------------------------------

def test_openrouter_prober_targets_auth_gated_key_endpoint():
    spec = probes.PROBERS["OPENROUTER_API_KEY"]
    assert spec.method == "GET"
    assert httpx.URL(spec.url).path == "/api/v1/key", (
        "OpenRouter must probe /api/v1/key (auth-gated) — /api/v1/models is public "
        "and would fake a ready")
    # Bearer auth, no secret in the URL.
    assert spec.headers("sk-or-secret")["Authorization"] == "Bearer sk-or-secret"
    assert "secret" not in spec.url


@pytest.mark.parametrize("value", [None, "", "  "], ids=["unset", "empty", "whitespace"])
async def test_openrouter_keyless_is_needs_key_zero_network(value, monkeypatch):
    if value is None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENROUTER_API_KEY", value)
    sentinel = SentinelTransport()
    e = entry(id="openrouter", role="llm", secret_env="OPENROUTER_API_KEY")

    results = await probes.probe_batch([e], transport=sentinel)

    assert sentinel.calls == []
    assert results["openrouter"].status == "needs_key"
    assert "OPENROUTER_API_KEY" in results["openrouter"].detail


async def test_openrouter_valid_key_is_ready(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-valid")

    async def handler(request):
        assert request.url.path == "/api/v1/key"
        assert request.headers["authorization"] == "Bearer sk-or-valid"
        return httpx.Response(200, json={"data": {"limit": None}})

    transport = RecordingTransport(handler)
    results = await probes.probe_batch(
        [entry(id="openrouter", role="llm", secret_env="OPENROUTER_API_KEY")],
        transport=transport)

    assert results["openrouter"].status == "ready"
    assert results["openrouter"].http_status == 200


async def test_openrouter_invalid_key_is_error_no_body_leak(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-bad")

    async def handler(request):
        # A vendor error body that echoes the key must never reach the detail string.
        return httpx.Response(401, json={"error": "invalid key sk-or-bad"})

    results = await probes.probe_batch(
        [entry(id="openrouter", role="llm", secret_env="OPENROUTER_API_KEY")],
        transport=RecordingTransport(handler))

    result = results["openrouter"]
    assert result.status == "error"
    assert result.http_status == 401
    assert "sk-or-bad" not in result.detail
