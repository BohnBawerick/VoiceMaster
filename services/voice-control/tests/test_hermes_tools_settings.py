"""The live missing-field path, from Settings persistence to the Hermes request.

All outbound traffic uses inert transports. No call or Hermes tool runs here.
"""
import json
import os

import httpx
import pytest

from conftest import RecordingTransport
from test_direct_lane_api import DIRECT, TO_DIRECT, client, config_dir, hermes  # noqa: F401
from voicecore import cascade_config, cascade_live, profiles


@pytest.mark.parametrize("stored", [None, True, False], ids=["missing", "on", "off"])
async def test_create_edit_reload_and_place_preserve_the_agents_tools(
        make_client, hermes, monkeypatch, stored):
    monkeypatch.setenv("HERMES_GATEWAY_TOKEN", "inert-token")
    dials = []

    async def dial(request):
        assert request.url.path == "/voice/outbound"
        dials.append(json.loads(request.content))
        return httpx.Response(200, json={"placed": True, "call_id": "inert-call"})

    async with make_client(RecordingTransport(dial)) as c:
        doc = {"id": "robot", "pipeline": "cascade", "providers": DIRECT,
               "hermes_profile": "default"}
        if stored is not None:
            doc["guardrails"] = {"on_call_tools": stored, "other_policy": "keep"}
        res = await c.post("/api/agents", json=doc)
        assert res.status_code == 201, res.text
        hermes.write_agent("untouched", pipeline="cascade", providers=DIRECT)
        before = hermes.agent_doc("untouched")
        for tools in (stored, False, True):
            if tools is not None:
                res = await c.put("/api/agents/robot/voice", json={
                    "guardrails": {"on_call_tools": tools}})
                assert res.status_code == 200, res.text
            loaded = (await c.get("/api/agents/robot")).json()
            row = next(r for r in (await c.get("/api/agents")).json() if r["id"] == "robot")
            assert row["guardrails"] == loaded.get("guardrails", {})
            assert hermes.agent_doc("robot") == loaded
            if stored is not None:
                assert loaded["guardrails"]["other_policy"] == "keep"
            placed = await c.post("/api/calls/place", json={
                "agent": "robot", "to": "+15551234567", "mission": "Say hello."})
            assert placed.status_code == 200, placed.text
            assert dials[-1]["agent"] == "robot"
            snapshot = profiles.load_named_profile(dials[-1]["agent"], "outbound")
            config = cascade_config.build_cascade_config(
                snapshot.doc, snapshot.registry, dict(os.environ),
                base_prompt=cascade_live.base_prompt_for(snapshot.doc))
            bodies = []

            def gateway(request, bodies=bodies):
                assert str(request.url) == "http://hermes:18789/v1/chat/completions"
                bodies.append(json.loads(request.content))
                return httpx.Response(200, text=(
                    'data: {"choices":[{"delta":{"content":"Hello."},"finish_reason":null}]}\n\n'
                    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                    'data: [DONE]\n\n'), headers={"Content-Type": "text/event-stream"})

            conversation = cascade_live.hermes_conversation_for(
                config, call_id="inert-call", token="inert-token",
                transport=httpx.MockTransport(gateway))
            assert [part async for part in conversation.stream_turn("hello")]
            assert bodies[0]["tool_choice"] == ("none" if tools is False else "auto")
            assert "tools" not in bodies[0]
        assert hermes.agent_doc("untouched") == before


@pytest.mark.parametrize("value", [None, "false", 0, 1, [], {}, {"on_call_tools": None},
                                   {"on_call_tools": "true"}, {"on_call_tools": 1},
                                   {"on_call_tools": True, "extra": False}])
async def test_malformed_tools_writes_fail_without_changing_disk(client, hermes, value):
    hermes.write_agent("robot", pipeline="cascade", providers=DIRECT)
    path = hermes.agents / "robot.yaml"
    before = path.read_bytes()
    async with client as c:
        res = await c.put("/api/agents/robot/voice", json={"guardrails": value})
    assert res.status_code == 422, res.text
    assert path.read_bytes() == before


async def test_other_runtimes_do_not_take_the_direct_tools_control(client, hermes):
    hermes.write_agent("robot")
    async with client as c:
        assert (await c.put("/api/agents/robot/voice", json={
            "guardrails": {"on_call_tools": True}})).status_code == 422
        assert profiles.on_call_tools_of(hermes.agent_doc("robot")) is False
        res = await c.put("/api/agents/robot/voice", json=TO_DIRECT)
        assert res.status_code == 200, res.text
        assert profiles.on_call_tools_of(hermes.agent_doc("robot")) is True


async def test_default_is_a_profile_not_an_agent_alias(client, hermes):
    async with client as c:
        assert (await c.get("/api/agents")).json() == []
        for agent in (None, "", "default", "Hermes (default profile)"):
            res = await c.post("/api/calls/place", json={
                "agent": agent, "to": "+15551234567", "mission": "Say hello."})
            assert res.status_code in (409, 422)
        for outlet in profiles.OUTLETS:
            for direction in profiles.ACTIVE_DIRECTIONS:
                res = await c.put("/api/active", json={
                    "outlets": {outlet: {direction: "default"}}})
                assert res.status_code == 422
