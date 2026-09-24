"""The ONE body of a dashboard → phone-bridge dial (ticket 09, ticket 11).

``POST /voice/outbound`` is the seam between the dashboard and mode-c, and the
presence of ``agent`` in this body is what makes a dial a **one-shot**: the
bridge loads that Agent, binds the snapshot to the call_id, skips the pointer,
skips the allow-list, and — the invariant ticket 09 fought for — does NOT record
that Agent as the phone Outlet's last-known-good at teardown.

It lives in the shared package for the same reason ``outbound_request.py`` does:
the bridge's own tests can then drive ``/voice/outbound`` with the EXACT body the
dashboard sends instead of a hand-copied lookalike that agrees with whichever
side was edited last. A Schedule fires through the dashboard's one placement
path, so it produces this body too — which is how "a scheduled Call is a
one-shot, not a configuration of the Outlet" is bound by a test rather than by a
comment.

``brief`` is the wire name for the Mission (the bridge has called it that since
before Missions were named); everything above this module says Mission.
"""

__all__ = ["DIAL_PATH", "build_dial_payload"]

DIAL_PATH = "/voice/outbound"


def build_dial_payload(*, to: str, mission: str, agent: str, disclose: bool,
                       target_display: str = "") -> dict:
    """The JSON body of one dial. ``agent`` present ⇒ the bridge's one-shot path."""
    return {
        "to": to,
        "brief": mission,
        "agent": agent,
        "disclose": bool(disclose),
        "target_display": target_display or "",
    }
