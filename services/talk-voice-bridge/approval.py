"""Single-slot owner-approval store for guest escalation.

The single-call lock guarantees at most ONE pending approval, so no ids-to-juggle:
create() rejects a second. The RealtimeBridge awaits await_verdict(); the plugin
(via the sidecar control API) calls resolve()."""
import asyncio
from typing import Optional


class ApprovalStore:
    def __init__(self):
        self._pending: Optional[dict] = None
        self._event = asyncio.Event()
        self._verdict: Optional[str] = None
        self._counter = 0

    def create(self, *, token: str, caller: str, summary: str) -> str:
        if self._pending is not None:
            raise RuntimeError("an approval is already pending")
        self._counter += 1
        aid = f"appr-{self._counter}"
        self._pending = {"approval_id": aid, "token": token, "caller": caller, "summary": summary}
        self._verdict = None
        self._event.clear()
        return aid

    def get_pending(self) -> Optional[dict]:
        return dict(self._pending) if self._pending else None

    def resolve(self, approval_id: str, decision: str) -> bool:
        if not self._pending or self._pending["approval_id"] != approval_id:
            return False
        self._verdict = "approved" if decision.strip().lower().startswith("appr") else "denied"
        self._event.set()
        return True

    async def await_verdict(self, approval_id: str, timeout: float) -> str:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
            verdict = self._verdict or "denied"
        except asyncio.TimeoutError:
            verdict = "timeout"
        finally:
            if self._pending and self._pending["approval_id"] == approval_id:
                self._pending = None            # clear the slot either way
        return verdict
