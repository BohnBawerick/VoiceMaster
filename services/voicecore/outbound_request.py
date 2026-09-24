"""The ONE builder of the outbound Twilio ``calls.create`` request (s5).

Extracted verbatim from the ``/voice/outbound`` route so the dashboard's dry-run
test-call preview renders the EXACT request the live route places — same TwiML, same
kwargs — instead of a lookalike mapper. The route calls this builder; the dashboard
imports the same one and calls it with a placeholder call_id.
"""
from twilio.twiml.voice_response import VoiceResponse, Connect


class OutboundRequestError(Exception):
    """The request cannot be rendered honestly (e.g. no public host) — never a
    half-built ``wss://None/...`` URL."""


def build_outbound_request(*, to: str, from_number: str, public_host: str,
                           call_id: str) -> dict:
    """The exact keyword arguments for ``twilio.rest.Client().calls.create``.

    Inline TwiML (twiml=) rather than a url= callback: Twilio opens the Media Stream
    directly when the callee answers, so there is no second signed webhook fetch to
    get right. The mission rides across as the <Parameter> call_id, which surfaces in
    the WS `start` customParameters.

    ``call_id`` is injectable on purpose: the route mints a secret token; the dry-run
    preview passes a visible placeholder so its output is deterministic and never
    mints (or leaks) a real stream credential.
    """
    if not (public_host or "").strip():
        raise OutboundRequestError(
            "VOICE_PUBLIC_HOST not set — cannot render the <Stream> URL "
            "(refusing to emit wss://None/voice/stream)")
    resp = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=f"wss://{public_host}/voice/stream")
    stream.parameter(name="call_id", value=call_id)
    resp.append(connect)
    return {"to": to, "from_": from_number, "twiml": str(resp)}
