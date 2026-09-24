"""Live credential probes for the Voice Control dashboard (s2).

Status semantics (contract-frozen):
  ready     <=> a real AUTHENTICATED round-trip to the vendor succeeded (2xx),
                using the key resolved from process env via the registry entry's
                ``secret_env`` name. Key-present-in-env alone is NEVER ready, and
                an unauthenticated public 200 is NEVER a probe.
  needs_key <=> ``secret_env`` unset, empty, or whitespace-only => ZERO network
                attempts for that entry; the detail names the missing env var.
  error     <=> probe attempted and failed (non-2xx / timeout / DNS): the detail
                carries the HTTP status code + stdlib reason phrase, or a
                timeout/connection marker. Never conflated with ready/needs_key.

Detail strings are DASHBOARD-COMPOSED ONLY: status code, ``http.HTTPStatus``
reason phrase, timeout/DNS marker, and the prober's own endpoint label (from
this module's spec table, never from the wire). Upstream response BODIES are
never read, logged, or surfaced -- vendor error bodies can echo credentials.
We deliberately use the stdlib phrase for the code rather than the upstream
status line, so nothing upstream-controlled reaches the detail string.

Dispatch is DATA-DRIVEN by the entry's ``secret_env`` (credential family),
never by provider id: any registry entry pointing at OPENAI_API_KEY -- whatever
its id -- is probed by the same prober. Each entry gets its OWN request (tagged
with its id in the User-Agent), so ids sharing a key still resolve
independently.

Endpoint choices (verified live 2026-07-18; statuses only, keys never echoed):
  - OpenAI: POST /v1/realtime/client_secrets mints a free ephemeral token
    (real key 200 / bad key 401 / no key 401). GET /v1/models is NOT usable:
    the production key is scope-restricted (missing api.model.read -> 403).
  - NVIDIA: GET api.nvcf.nvidia.com/v2/nvcf/functions (200/403/401).
    integrate.api.nvidia.com/v1/models is PUBLIC (200 with no credential) --
    probing it would be fabrication, not authentication.
  - Google: generativelanguage models-list with the x-goog-api-key HEADER
    (never the ?key= query param, so URLs stay secret-free everywhere).
  - ElevenLabs: GET /v1/user (user-info class, 200/401).
  - OpenRouter: GET /api/v1/key — the auth-gated key-metadata route (200/401,
    verified 2026-07-19). /api/v1/models is PUBLIC (200 keyless) so it can never
    prove a key; probing it would be fabrication.
  All keyless-family endpoints below were verified to REJECT unauthenticated
  requests (401/400), so none can fake a ready.
"""
import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus

import httpx

from . import cascade_config  # BROWSER_UA — the ONE shared source for the Zen CF-1010 UA
from . import hermes_gateway

# Hard per-probe wall-clock cap (contract: <= 5 s). Enforced belt-and-braces:
# as the httpx client timeout AND as an asyncio.wait_for around each request,
# so even a transport that ignores timeouts (e.g. a hung mock) is bounded.
PROBE_TIMEOUT_S = 5.0


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


@dataclass(frozen=True)
class ProbeSpec:
    """One credential family's authenticated verification request."""

    method: str
    url: str
    headers: "object"  # callable: key -> dict of auth headers
    json_body: "dict | None" = None
    # Override the default `hermes-voice-control-probe/<id>` User-Agent. Only OpenCode
    # Zen needs it — its Cloudflare bot-protection 403s ("error code: 1010") any
    # non-browser UA, so a probe with the default UA would falsely read `error` on a
    # perfectly valid key. Left None for every other family (UA stays the probe tag).
    user_agent: "str | None" = None

    @property
    def label(self) -> str:
        """Endpoint label for detail strings — composed from OUR spec, never the wire."""
        u = httpx.URL(self.url)
        return f"{u.host}{u.path}"


# secret_env NAME -> authenticated credential-verification endpoint.
# Keyed by credential family on purpose (c31): NO per-provider-id entries here.
PROBERS: "dict[str, ProbeSpec]" = {
    "OPENAI_API_KEY": ProbeSpec(
        "POST", "https://api.openai.com/v1/realtime/client_secrets", _bearer,
        {"session": {"type": "realtime", "model": "gpt-realtime"}}),
    "GOOGLE_API_KEY": ProbeSpec(
        "GET", "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
        lambda k: {"x-goog-api-key": k}),
    "NVIDIA_API_KEY": ProbeSpec(
        "GET", "https://api.nvcf.nvidia.com/v2/nvcf/functions", _bearer),
    "ELEVENLABS_API_KEY": ProbeSpec(
        "GET", "https://api.elevenlabs.io/v1/user", lambda k: {"xi-api-key": k}),
    "DEEPGRAM_API_KEY": ProbeSpec(
        "GET", "https://api.deepgram.com/v1/auth/token",
        lambda k: {"Authorization": f"Token {k}"}),
    "XAI_API_KEY": ProbeSpec(
        "GET", "https://api.x.ai/v1/models", _bearer),
    "OPENROUTER_API_KEY": ProbeSpec(
        # /api/v1/key is the AUTH-GATED key-metadata endpoint (200 valid / 401 bad or
        # empty). /api/v1/models is PUBLIC and would fake a ready — never probe it.
        "GET", "https://openrouter.ai/api/v1/key", _bearer),
    "ANTHROPIC_API_KEY": ProbeSpec(
        "GET", "https://api.anthropic.com/v1/models",
        lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"}),
    "ZHIPU_API_KEY": ProbeSpec(
        "GET", "https://open.bigmodel.cn/api/paas/v4/models", _bearer),
    "OPENCODE_GO_API_KEY": ProbeSpec(
        # Zen has NO auth-gated GET route we can trust: /usage, /credits, /me all return
        # the HTML console, and /zen/go/v1/models is untrusted (public-200 risk). The
        # honest probe is a real credentialed POST /chat/completions (200 valid /
        # 401 bad-or-empty), capped at 1 token. Needs the browser UA or CF-1010 403s it.
        "POST", "https://opencode.ai/zen/go/v1/chat/completions", _bearer,
        {"model": "minimax-m3", "messages": [{"role": "user", "content": "ping"}],
         "max_tokens": 1},
        user_agent=cascade_config.BROWSER_UA),
    "SONIOX_API_KEY": ProbeSpec(
        "GET", "https://api.soniox.com/v1/models", _bearer),
    "ASSEMBLYAI_API_KEY": ProbeSpec(
        "GET", "https://api.assemblyai.com/v2/transcript?limit=1",
        lambda k: {"Authorization": k}),
    "CARTESIA_API_KEY": ProbeSpec(
        "GET", "https://api.cartesia.ai/voices/",
        lambda k: {"X-API-Key": k, "Cartesia-Version": "2024-06-10"}),
    "INWORLD_API_KEY": ProbeSpec(
        "GET", "https://api.inworld.ai/tts/v1/voices",
        lambda k: {"Authorization": f"Basic {k}"}),
}


@dataclass(frozen=True)
class ProbeResult:
    status: str                  # ready | needs_key | error
    detail: str                  # dashboard-composed, human, secret-free
    checked_at: "str | None"     # ISO-8601 UTC; only set when a probe actually ran
    http_status: "int | None"    # vendor HTTP status when one was received


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reason(code: int) -> str:
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return "Non-standard Status"


def resolve_key(entry: dict, env=None) -> "str | None":
    """The credential value for this entry, or None when unset/empty/whitespace."""
    env = os.environ if env is None else env
    name = entry.get("secret_env") or ""
    value = (env.get(name) or "").strip()
    return value or None


def needs_key_result(entry: dict) -> ProbeResult:
    name = entry.get("secret_env")
    return ProbeResult(
        status="needs_key",
        detail=f"needs {name} — not set in the environment; probe skipped, no request sent",
        checked_at=None,
        http_status=None,
    )


def _spec_for(entry: dict, env) -> "ProbeSpec | None":
    """The prober for this entry's credential family. Every family but one has a fixed
    vendor URL; the Hermes gateway bearer is checked against wherever THIS deployment's
    default gateway lives. ``/health/detailed`` is the auth-gated health route (200 with
    a good bearer, 401 without), so it verifies the token and runs no agent turn. It
    says nothing about any other profile's gateway: those are per Agent, and the pickup
    check on each call is what covers them."""
    name = entry.get("secret_env")
    if name == hermes_gateway.ENV_GATEWAY_TOKEN:
        base = hermes_gateway.gateway_url_for_profile("default", env)
        return ProbeSpec("GET", f"{base}/health/detailed", _bearer)
    return PROBERS.get(name)


async def probe_one(entry: dict, key: str, client: httpx.AsyncClient,
                    env=None) -> ProbeResult:
    """One authenticated round-trip for one registry entry. Never reads the body."""
    spec = _spec_for(entry, os.environ if env is None else env)
    if spec is None:
        return ProbeResult(
            "error",
            f"no prober wired for credential family {entry.get('secret_env')} — "
            "cannot verify this key",
            _now(), None)
    headers = dict(spec.headers(key))
    # Tag each request with the entry id: harmless to vendors, and it keeps
    # per-id probes distinguishable (ids sharing a key resolve independently).
    # A spec may override the UA (Zen's CF-1010 needs a browser UA — see ProbeSpec).
    headers["User-Agent"] = (spec.user_agent
                             or f"hermes-voice-control-probe/{entry.get('id')}")
    try:
        response = await asyncio.wait_for(
            client.request(spec.method, spec.url, headers=headers, json=spec.json_body),
            timeout=PROBE_TIMEOUT_S)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return ProbeResult(
            "error",
            f"timeout — no response from {spec.label} within {PROBE_TIMEOUT_S:.0f} s",
            _now(), None)
    except httpx.ConnectError:
        return ProbeResult(
            "error",
            f"connection failed (DNS or refused) reaching {spec.label}",
            _now(), None)
    except httpx.HTTPError as exc:
        return ProbeResult(
            "error",
            f"transport error ({type(exc).__name__}) reaching {spec.label}",
            _now(), None)
    code = response.status_code
    if 200 <= code < 300:
        return ProbeResult(
            "ready",
            f"HTTP {code} {_reason(code)} — authenticated {spec.method} {spec.label}",
            _now(), code)
    return ProbeResult(
        "error",
        f"HTTP {code} {_reason(code)} from {spec.method} {spec.label}",
        _now(), code)


async def probe_batch(entries, env=None, transport=None) -> "dict[str, ProbeResult]":
    """Resolve every entry: keyless -> needs_key with ZERO network; keyed -> live
    probe. All keyed probes run CONCURRENTLY, each capped at PROBE_TIMEOUT_S."""
    env = os.environ if env is None else env
    results: "dict[str, ProbeResult]" = {}
    keyed: list = []
    for entry in entries:
        key = resolve_key(entry, env)
        if key is None:
            results[entry["id"]] = needs_key_result(entry)
        else:
            keyed.append((entry, key))
    if keyed:
        async with httpx.AsyncClient(
                transport=transport, timeout=PROBE_TIMEOUT_S,
                follow_redirects=False) as client:
            probed = await asyncio.gather(
                *(probe_one(entry, key, client, env) for entry, key in keyed))
        for (entry, _), result in zip(keyed, probed):
            results[entry["id"]] = result
    return results


ELEVENLABS_VOICES_URL = "https://api.elevenlabs.io/v1/voices"


async def fetch_elevenlabs_voices(key: str, transport=None) -> "tuple[list | None, str | None]":
    """The account's voice catalog for the editor dropdown: (voices, None) on success,
    (None, detail) on any failure — the caller renders an honest free-text fallback,
    never a fake list. Details are composed here (status/type only), no upstream body
    fragments beyond the voice ids/names themselves."""
    try:
        async with httpx.AsyncClient(transport=transport, timeout=PROBE_TIMEOUT_S,
                                     follow_redirects=False) as client:
            resp = await client.get(ELEVENLABS_VOICES_URL, headers={"xi-api-key": key})
    except httpx.HTTPError as exc:
        return None, (f"voice fetch failed (transport error {type(exc).__name__}) — "
                      "enter a voice id manually")
    if resp.status_code != 200:
        return None, (f"voice fetch failed (HTTP {resp.status_code}) — enter a voice "
                      "id manually")
    voices = [{"id": v.get("voice_id"), "name": v.get("name") or v.get("voice_id")}
              for v in (resp.json() or {}).get("voices", []) if v.get("voice_id")]
    if not voices:
        return None, "the account returned no voices — enter a voice id manually"
    return voices, None
