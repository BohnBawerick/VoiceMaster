"""Hindsight mock + a real dashboard process, shared by the browser tests.

Extracted from ``test_calls_browser`` so ``test_calls_matrix`` drives the same
store against the same screen: two mocks would drift, and a matrix cell that
passes against its own private mock proves nothing.

The mock's bank spec language, used by every scenario:

    "voice": [doc, ...]                     a healthy bank holding those documents
    "voice": 503                            the bank answers that HTTP status
    (bank absent from the mapping)          the store answers 404: no such bank
    "voice": {"docs": [...],                a bank that honours `limit` and
              "ignore_offset": True}        IGNORES `offset` -- so paging through
                                            it returns page 1 forever and the
                                            bounded fetch stops with a prefix

``ignore_offset`` is what makes a read ``partial`` without any bank failing,
which is the "read completeness" axis of ``fixture_matrix``. Two further knobs
model stores this dashboard has to survive:

    "no_by_id": True        no fetch-by-id endpoint: every
                            GET /documents/<id> is a 404, so `get_call` has to
                            fall back to scanning the listing
    "recall_ghost_hits": n  recall returns `n` document ids that cannot then be
                            fetched -- a search hit the store could not resolve
    "report_total": False   the listing omits `total`

**The mock serves what the STORE serves, not what we POST.** Scenarios build
documents with the producer fixtures, whose metadata sits under ``metadata``
because that is the retainers' POST body; every document goes out of here
through ``as_store_returns``, which moves it to ``document_metadata`` - the only
key a real Hindsight document has. A mock that echoed the POST shape is what let
the dashboard read the wrong key and render every retained field as "not
retained" while the store held it. Do not "simplify" this away.
"""
import json
import os
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import app as voice_app
from hindsight_producer_fixtures import as_store_returns


class _StoreHandler(BaseHTTPRequestHandler):
    banks = {}

    def log_message(self, *args):  # keep pytest output clean
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _resolve(self, path):
        """(bank_name, spec) for a request path, or a ready-made error response."""
        match = re.match(r"/v1/default/banks/([^/]+)/", path)
        bank = match.group(1) if match else ""
        content = self.banks.get(bank)

        if content is None:
            # A bank that does not exist in this store.
            self._send(404, {"detail": f"bank '{bank}' not found"})
            return None
        if isinstance(content, int):
            self._send(content, {"detail": f"bank '{bank}' unavailable"})
            return None

        spec = content if isinstance(content, dict) else {"docs": content}
        return spec

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        spec = self._resolve(path)
        if spec is None:
            return
        docs = spec["docs"]

        doc_match = re.match(r"/v1/default/banks/[^/]+/documents/(.+)$", path)
        if doc_match:
            if spec.get("no_by_id"):
                # A store with no fetch-by-id endpoint at all.
                return self._send(404, {"detail": "no such route"})
            wanted = doc_match.group(1)
            for doc in docs:
                if doc["id"] == wanted:
                    return self._send(200, as_store_returns(doc))
            return self._send(404, {"detail": "not found"})

        if path.endswith("/documents"):
            query = dict(
                part.split("=", 1)
                for part in (self.path.split("?", 1)[1].split("&") if "?" in self.path else [])
            )
            limit = int(query.get("limit", 100))
            offset = 0 if spec.get("ignore_offset") else int(query.get("offset", 0))
            payload = {"items": [as_store_returns(doc)
                                 for doc in docs[offset : offset + limit]]}
            if spec.get("report_total", True):
                payload["total"] = len(docs)
            return self._send(200, payload)

        return self._send(404, {"detail": "no route"})

    def do_POST(self):  # noqa: N802
        """Hindsight's recall endpoint, as ``_recall_bank`` calls it."""
        path = self.path.split("?")[0]
        spec = self._resolve(path)
        if spec is None:
            return
        if not path.endswith("/memories/recall"):
            return self._send(404, {"detail": "no route"})

        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

        results = [{"document_id": doc["id"]} for doc in spec["docs"]]
        # Hits the store returns but cannot then hand over: a real possibility
        # (the recall index outliving the document), and the case where `total`
        # would otherwise silently under-report the matches.
        for i in range(int(spec.get("recall_ghost_hits") or 0)):
            results.append({"document_id": f"ghost-hit-{i:03d}"})
        return self._send(200, {"results": results})


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Stack:
    """A Hindsight mock plus a uvicorn serving the dashboard against it."""

    def __init__(self, banks, configured_bank):
        import uvicorn

        handler = type("_H", (_StoreHandler,), {"banks": banks})
        self.store = ThreadingHTTPServer(("127.0.0.1", free_port()), handler)
        self.store_thread = threading.Thread(target=self.store.serve_forever, daemon=True)
        self.store_thread.start()

        self.store_url = f"http://127.0.0.1:{self.store.server_address[1]}"
        self.configured_bank = configured_bank
        self.activate()

        self.port = free_port()
        config = uvicorn.Config(
            voice_app.create_app(), host="127.0.0.1", port=self.port, log_level="error"
        )
        self.server = uvicorn.Server(config)
        self.app_thread = threading.Thread(target=self.server.run, daemon=True)
        self.app_thread.start()
        for _ in range(200):
            if self.server.started:
                break
            threading.Event().wait(0.05)
        else:
            raise RuntimeError("dashboard did not start")

    def activate(self):
        """Point the dashboard at THIS store.

        Every Stack's app resolves ``HINDSIGHT_URL`` from the environment per
        request, so with two stacks alive both apps read whichever was created
        last. A test that stands two stores side by side -- a complete read
        against a bounded one, say -- would then compare one store with itself
        and pass while the screen was wrong. Addressing a stack re-points it.
        """
        os.environ["HINDSIGHT_URL"] = self.store_url
        os.environ["HINDSIGHT_BANK"] = self.configured_bank

    @property
    def base(self) -> str:
        self.activate()
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.should_exit = True
        self.app_thread.join(timeout=10)
        self.store.shutdown()
        self.store.server_close()
        self.store_thread.join(timeout=10)


# --------------------------------------------------------------------------
# DOM helpers shared by both browser test modules
# --------------------------------------------------------------------------


def body_text(page) -> str:
    return page.inner_text("body")


def visible_warn_banners(page):
    """Every warning banner currently on screen, wherever it was appended.

    ``.alert-banner`` is the screen's ONE banner class (``alert-unreachable``
    and ``alert-partial`` are its two variants), so "no banner of any kind" is
    one selector rather than a list of sentences the last defect happened to
    use. Ticket 15 repointed this from the deleted legacy screen's
    ``.banner-warn``; a helper that still matched only that class would make
    every "no banner" assertion vacuously true.
    """
    return [el.inner_text().strip() for el in page.query_selector_all(".alert-banner")
            if el.is_visible()]
