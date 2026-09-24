"""A fake ElevenLabs stream-input socket, played by a suite's httpx MockTransport.

The engine speaks over ONE websocket per reply (``elevenlabs_live.TTSStream``). The
suites were written against the old one-HTTP-request-per-line API, and their handlers
record what ElevenLabs was asked to say, stream audio with delays, or fail with a
status. This socket keeps all of that meaningful: every flushed sentence becomes one
POST through the same transport, to the same host, with the socket's own query string
(``output_format`` and the rest), and the response body comes back as ``audio``
messages, then ``isFinal`` after the closing empty text. Nothing here dials a network.
"""
import asyncio
import base64
import json
import urllib.parse

import httpx

_END = object()


class HttpBackedTTSSocket:
    def __init__(self, transport, url: str):
        parsed = urllib.parse.urlsplit(url)
        voice = parsed.path.split("/")[-2]
        self.http_url = (f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream?"
                         + parsed.query)
        self._transport = transport
        self._texts: asyncio.Queue = asyncio.Queue()
        self._out: asyncio.Queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._work())
        self.closed = False

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        text = message.get("text")
        if text == "":
            await self._texts.put(_END)
        elif message.get("flush"):
            await self._texts.put(text.strip())

    async def _work(self) -> None:
        try:
            await self._serve()
        except Exception as exc:  # noqa: BLE001 - a broken fake must fail the test, not hang it
            await self._out.put(json.dumps({"error": repr(exc), "message": "fake failed"}))

    async def _serve(self) -> None:
        async with httpx.AsyncClient(transport=self._transport, timeout=60) as client:
            while True:
                text = await self._texts.get()
                if text is _END:
                    await self._out.put(json.dumps({"isFinal": True}))
                    return
                async with client.stream("POST", self.http_url, json={"text": text}) as resp:
                    if resp.status_code != 200:
                        await self._out.put(json.dumps(
                            {"error": f"HTTP {resp.status_code}", "message": "failed"}))
                        return
                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            await self._out.put(json.dumps(
                                {"audio": base64.b64encode(chunk).decode()}))

    def __aiter__(self):
        return self._messages()

    async def _messages(self):
        while True:
            message = await self._out.get()
            if message is _END:
                return
            yield message

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._worker.cancel()
            await self._out.put(_END)


def http_tts_connect(transport, sockets: "list | None" = None):
    """A ``connect`` for ``CascadeLiveSession(tts_connect=...)``. ``sockets`` collects
    every socket opened, so a test can count replies or check one was closed."""
    async def connect(url, additional_headers=None):
        sock = HttpBackedTTSSocket(transport, url)
        if sockets is not None:
            sockets.append(sock)
        return sock
    return connect
