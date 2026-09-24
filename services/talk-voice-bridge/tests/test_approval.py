import asyncio, pytest
from approval import ApprovalStore


def test_create_rejects_second_pending():
    s = ApprovalStore()
    aid = s.create(token="r1", caller="alice", summary="send email")
    assert aid and s.get_pending()["approval_id"] == aid
    with pytest.raises(RuntimeError):
        s.create(token="r1", caller="alice", summary="another")


@pytest.mark.asyncio
async def test_resolve_unblocks_await():
    s = ApprovalStore()
    aid = s.create(token="r1", caller="alice", summary="x")

    async def approve_soon():
        await asyncio.sleep(0.05)
        assert s.resolve(aid, "approve") is True
    asyncio.create_task(approve_soon())
    verdict = await s.await_verdict(aid, timeout=2)
    assert verdict == "approved"
    assert s.get_pending() is None            # cleared after resolution


@pytest.mark.asyncio
async def test_await_times_out():
    s = ApprovalStore()
    aid = s.create(token="r1", caller="alice", summary="x")
    verdict = await s.await_verdict(aid, timeout=0.1)
    assert verdict == "timeout"
    assert s.get_pending() is None
