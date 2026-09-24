import httpx, respx, pytest
import hermes


def test_owner_prompt_grants_full_trust(tmp_path):
    (tmp_path / "SOUL.md").write_text("I am Robot, blunt and helpful.")
    p = hermes.build_system_prompt(str(tmp_path), trust="owner", caller_display="Alex")
    assert "I am Robot, blunt and helpful." in p
    assert "OWNER" in p and "full" in p.lower()


def test_guest_prompt_requires_approval(tmp_path):
    (tmp_path / "SOUL.md").write_text("soul")
    p = hermes.build_system_prompt(str(tmp_path), trust="guest", caller_display="Alice")
    assert "GUEST" in p
    assert "request_owner_approval" in p          # actions must be escalated


def test_tools_include_hermes_and_approval():
    names = {t["name"] for t in hermes.TOOLS}
    assert names == {"hermes_agent", "request_owner_approval"}
    approval = next(t for t in hermes.TOOLS if t["name"] == "request_owner_approval")
    assert "summary" in approval["parameters"]["properties"]


@pytest.mark.asyncio
@respx.mock
async def test_call_hermes_agent_parses_content():
    respx.post("http://hermes.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "Two containers are running."}}]}))
    out = await hermes.call_hermes_agent("check docker", gateway_url="http://hermes.test", token="", timeout=5)
    assert out == "Two containers are running."


@pytest.mark.asyncio
@respx.mock
async def test_call_hermes_agent_handles_http_error():
    respx.post("http://hermes.test/v1/chat/completions").mock(return_value=httpx.Response(500, text="boom"))
    out = await hermes.call_hermes_agent("x", gateway_url="http://hermes.test", token="", timeout=5)
    assert "error" in out.lower()


@pytest.mark.asyncio
@respx.mock
async def test_call_hermes_agent_prompt_nudges_skill_discovery():
    """The posted prompt must push the backend to discover + run skills (the fix for a bare
    one-shot answering 'I can't' from its native tool list), while keeping the instruction and
    the plain-spoken/read-aloud constraint."""
    import json
    route = respx.post("http://hermes.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    await hermes.call_hermes_agent("check my recent emails",
                                   gateway_url="http://hermes.test", token="", timeout=5)
    sent = json.loads(route.calls.last.request.content)["messages"][0]["content"]
    assert "skills_list" in sent                    # discover skills
    assert "actually execute" in sent.lower()       # and run them
    assert "check my recent emails" in sent          # the user's instruction is carried
    assert "read aloud" in sent.lower()              # voice constraint preserved
