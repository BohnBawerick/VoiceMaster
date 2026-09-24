"""API-level tests through the ASGI app (c06, c08, c10b, c14, c27-c29) — offline."""
import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
import pytest

from conftest import (CANONICAL_PROVIDERS, OPENAI_IDS, RecordingTransport,
                      SentinelTransport)

REQUIRED_KEYS = {"id", "role", "display_name", "secret_env", "capabilities",
                 "cost_hint", "latency_hint", "status", "probe"}


def by_id(rows):
    return {row["id"]: row for row in rows}


# ---------------------------------------------------------------------------
# c06/c08 — shape + needs_key semantics with NO keys set (and zero network)
# ---------------------------------------------------------------------------

async def test_api_shape_canonical_18_all_needs_key_without_keys(make_client):
    sentinel = SentinelTransport()
    async with make_client(sentinel) as client:
        response = await client.get("/api/providers")

    assert response.status_code == 200
    rows = response.json()
    assert [row["id"] for row in rows] == CANONICAL_PROVIDERS
    assert sentinel.calls == []  # keyless => zero network for ALL 17

    for row in rows:
        assert REQUIRED_KEYS.issubset(row)
        assert row["role"] in ("realtime", "llm", "stt", "tts")
        assert row["status"] == "needs_key"
        assert isinstance(row["capabilities"], list)
        for hint in (row["cost_hint"], row["latency_hint"]):
            assert hint is None or isinstance(hint, str)
        # needs_key detail names the missing env var; no checked_at claimed
        assert row["secret_env"] in row["probe"]["detail"]
        assert "checked_at" not in row["probe"]


# ---------------------------------------------------------------------------
# c10b — MANDATORY integration: OPENAI_API_KEY=sk-invalid => all 3 OpenAI ids
#        error with 401-class detail
# ---------------------------------------------------------------------------

async def test_bad_openai_key_marks_all_openai_ids_error(make_client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-invalid")

    async def handler(request):
        assert request.url.host == "api.openai.com"  # only family with a key set
        return httpx.Response(401, json={"error": {"message": "Incorrect API key"}})

    async with make_client(RecordingTransport(handler)) as client:
        response = await client.get("/api/providers")

    assert response.status_code == 200
    rows = by_id(response.json())
    for pid in OPENAI_IDS:
        row = rows[pid]
        assert row["status"] == "error", f"{pid} must be error, got {row['status']}"
        assert "401" in row["probe"]["detail"]
        assert row["probe"]["http_status"] == 401
        assert "checked_at" in row["probe"]
    # everything else stays honest needs_key
    for pid, row in rows.items():
        if pid not in OPENAI_IDS:
            assert row["status"] == "needs_key"


# ---------------------------------------------------------------------------
# c27 — ids sharing a secret_env resolve independently (no per-key collapse)
# ---------------------------------------------------------------------------

async def test_shared_key_ids_resolve_independently(make_client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-something")

    async def handler(request):
        # One OpenAI-keyed probe fails auth, its siblings succeed.
        if "gpt-4.1" in request.headers["user-agent"]:
            return httpx.Response(401, json={"error": {"message": "no"}})
        return httpx.Response(200, json={"ok": True})

    async with make_client(RecordingTransport(handler)) as client:
        rows = by_id((await client.get("/api/providers")).json())

    assert rows["gpt-4.1"]["status"] == "error"
    assert "401" in rows["gpt-4.1"]["probe"]["detail"]
    assert rows["openai-gpt-realtime"]["status"] == "ready"
    assert rows["openai-gpt-4o-transcribe"]["status"] == "ready"


# ---------------------------------------------------------------------------
# c14 — cache-until-refresh; refresh re-probes ALL eligible entries
# ---------------------------------------------------------------------------

async def test_cache_until_refresh_and_full_reprobe(make_client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-x")
    # ElevenLabs Scribe (ticket 21) shares the ElevenLabs key, so it is probed too.
    probed_ids = OPENAI_IDS + ["elevenlabs-scribe", "elevenlabs"]

    async def handler(request):
        return httpx.Response(200, json={"ok": True})

    transport = RecordingTransport(handler)
    async with make_client(transport) as client:
        first = by_id((await client.get("/api/providers")).json())
        calls_after_first = len(transport.calls)
        assert calls_after_first == len(probed_ids)  # 3 OpenAI ids + both ElevenLabs ids

        second = by_id((await client.get("/api/providers")).json())
        assert len(transport.calls) == calls_after_first  # cache hit: NO new network
        for pid in probed_ids:
            assert first[pid]["probe"]["checked_at"] == second[pid]["probe"]["checked_at"]

        await asyncio.sleep(0.02)
        refresh = await client.post("/api/providers/refresh")
        assert refresh.status_code == 200
        # full re-probe: exactly one new request per eligible entry
        assert len(transport.calls) == 2 * calls_after_first

        third = by_id(refresh.json())
        for pid in probed_ids:  # EVERY probed entry strictly newer, ready ones included
            assert third[pid]["probe"]["checked_at"] > second[pid]["probe"]["checked_at"]


# ---------------------------------------------------------------------------
# c28 — one hung vendor: batch bounded, others resolve, healthz stays instant
# ---------------------------------------------------------------------------

async def test_hung_vendor_doesnt_block_batch_or_health(make_client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-x")

    async def handler(request):
        if request.url.host == "api.elevenlabs.io":
            await asyncio.sleep(60)  # hung vendor
        return httpx.Response(200, json={"ok": True})

    async with make_client(RecordingTransport(handler)) as client:
        start = time.monotonic()
        providers_task = asyncio.create_task(client.get("/api/providers"))

        await asyncio.sleep(0.3)  # ensure the probe cycle is in flight
        health_start = time.monotonic()
        health = await client.get("/healthz")
        health_elapsed = time.monotonic() - health_start
        assert health.status_code == 200
        assert health_elapsed < 1.0, "healthz must answer during a hung probe cycle"

        response = await providers_task
        elapsed = time.monotonic() - start

    assert elapsed < 6.5
    rows = by_id(response.json())
    hung = rows["elevenlabs"]
    assert hung["status"] == "error"
    assert "timeout" in hung["probe"]["detail"].lower()
    for pid in OPENAI_IDS:
        assert rows[pid]["status"] == "ready"


# ---------------------------------------------------------------------------
# c29 — two parallel refreshes: both succeed, coherent snapshot afterwards
# ---------------------------------------------------------------------------

async def test_concurrent_refreshes_are_safe(make_client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-x")

    async def handler(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"ok": True})

    async with make_client(RecordingTransport(handler)) as client:
        window_start = datetime.now(timezone.utc).isoformat()
        first, second = await asyncio.gather(
            client.post("/api/providers/refresh"),
            client.post("/api/providers/refresh"))
        assert first.status_code == 200
        assert second.status_code == 200

        rows = (await client.get("/api/providers")).json()

    window_end = datetime.now(timezone.utc).isoformat()
    for row in rows:
        assert row["status"] in ("ready", "needs_key", "error")
        if row["status"] != "needs_key":
            checked = row["probe"]["checked_at"]
            assert window_start <= checked <= window_end, (
                f"{row['id']} checked_at {checked} outside refresh window")


# ---------------------------------------------------------------------------
# c20 (API surface) — hostile upstream body never reaches the JSON response
# ---------------------------------------------------------------------------

async def test_upstream_body_never_leaks_into_api_response(make_client, monkeypatch):
    secret = "sk-proj-SUPERSECRETVALUE1234567890"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    async def handler(request):
        return httpx.Response(
            401, json={"error": {"message": f"invalid key: {secret}"}})

    async with make_client(RecordingTransport(handler)) as client:
        response = await client.get("/api/providers")

    dump = json.dumps(response.json())
    assert secret not in dump
    for i in range(len(secret) - 7):  # every 8-char fragment of the key
        assert secret[i:i + 8] not in dump
