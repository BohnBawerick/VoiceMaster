"""Import/startup hygiene (c18): importing the app module makes ZERO probe
network calls, and /healthz answers without any probe having ever run."""
import importlib
import socket

import httpx
import pytest

import app as app_module
from conftest import SentinelTransport


def test_import_time_makes_no_probe_calls(monkeypatch):
    attempts = []

    async def tripwire_send(self, *args, **kwargs):
        attempts.append(args)
        raise AssertionError("httpx request during module import")

    def tripwire_connect(self, *args, **kwargs):
        attempts.append(args)
        raise AssertionError("socket connect during module import")

    monkeypatch.setattr(httpx.AsyncClient, "send", tripwire_send)
    monkeypatch.setattr(httpx.Client, "send", tripwire_send)
    monkeypatch.setattr(socket.socket, "connect", tripwire_connect)

    importlib.reload(app_module)  # re-executes module top level under the tripwires

    assert attempts == []


async def test_healthz_is_instant_and_probe_free(make_client, monkeypatch):
    # Keys present and probe-eligible — healthz still must not probe anything.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-x")

    sentinel = SentinelTransport()
    async with make_client(sentinel) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert sentinel.calls == []
