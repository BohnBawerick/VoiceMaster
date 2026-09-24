"""s6 (D7) — app-side HTTP Basic auth arms. Offline."""
import base64

import httpx

from conftest import SentinelTransport

CRED = base64.b64encode(b"sam:hunter2!x").decode()


def basic(value: str) -> dict:
    return {"Authorization": f"Basic {value}"}


async def test_auth_off_when_env_unset(make_client):
    async with make_client(SentinelTransport()) as client:
        response = await client.get("/api/agents")
    assert response.status_code == 200


async def test_missing_and_wrong_creds_get_401_with_challenge(monkeypatch, make_client):
    monkeypatch.setenv("VOICE_DASHBOARD_USER", "sam")
    monkeypatch.setenv("VOICE_DASHBOARD_PASSWORD", "hunter2!x")
    wrong = base64.b64encode(b"sam:nope").decode()
    async with make_client(SentinelTransport()) as client:
        for headers in ({}, basic(wrong), {"Authorization": "Bearer whatever"}):
            response = await client.get("/api/agents", headers=headers)
            assert response.status_code == 401
            assert response.headers["WWW-Authenticate"].startswith("Basic")


async def test_correct_creds_pass_and_healthz_stays_open(monkeypatch, make_client):
    monkeypatch.setenv("VOICE_DASHBOARD_USER", "sam")
    monkeypatch.setenv("VOICE_DASHBOARD_PASSWORD", "hunter2!x")
    async with make_client(SentinelTransport()) as client:
        assert (await client.get("/healthz")).status_code == 200
        response = await client.get("/api/agents", headers=basic(CRED))
        assert response.status_code == 200


async def test_password_never_in_any_response_body(monkeypatch, make_client):
    monkeypatch.setenv("VOICE_DASHBOARD_USER", "sam")
    monkeypatch.setenv("VOICE_DASHBOARD_PASSWORD", "hunter2!x")
    async with make_client(SentinelTransport()) as client:
        for path, headers in (("/api/agents", basic(CRED)), ("/api/agents", {}),
                              ("/healthz", {})):
            response = await client.get(path, headers=headers)
            assert "hunter2!x" not in response.text
