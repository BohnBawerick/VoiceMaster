"""s14a-2a — the bridge-side cascade readiness endpoint.

WHY THIS EXISTS. Until now the ONLY cascade readiness signal was the dashboard's Talk
dry-run, which runs inside `voice-control` and therefore resolves providers against
*voice-control's* environment. That is structurally blind to THIS process: s14a-1 caught
`hermes-talk-voice` carrying ZERO cascade credentials while the Talk dry-run happily
reported `would_place=true`. s14a-1 fixed the keys; this endpoint fixes the blindness, by
answering the readiness question in the process that actually runs the cascade.

Three stanzas, each covering a failure class the dashboard cannot see from outside:
  live_wiring       — the `cunt` incident: a schema-valid, keyed provider whose STT/TTS is
                      bench-only, so the bridge refuses the call. NEVER checked on the Talk
                      lane before (run_dry_run_talk has no live_wiring stanza at all).
  providers         — the s14a-1 incident: the key is missing/empty/wrong IN THIS PROCESS.
  eventlog_writable — the swallowed-EACCES bug: talk-voice runs uid 1000, the shared
                      eventlog was root:root, and append_event SWALLOWS the failure, so the
                      Talk lane lost observability SILENTLY. A readiness report blind to
                      this green-lights a live campaign that records nothing.
"""
import json
import os
import stat
from pathlib import Path

import httpx
import pytest

from voicecore import cascade_config
from voicecore import eventlog
import readiness


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------

def _registry():
    """The real provider registry — never a hand-rolled stub, so a registry change that
    breaks wiring shows up here rather than being papered over by a fixture."""
    from voicecore import profiles
    return profiles.load_registry()


def _doc(stt="deepgram", llm="openrouter", tts="elevenlabs"):
    return {
        "id": "s14a2a-probe",
        "pipeline": "cascade",
        "providers": {"stt": stt, "llm": llm, "tts": tts},
    }


def _bench_only_stt():
    """An STT provider that is REGISTERED and cascade-wired but has NO live client.

    Derived, never hardcoded — and it must satisfy all three conditions. An earlier draft
    of this fixture used the string "nvidia", which is not a registry id at all (the entry
    is "nvidia-nemotron"), so the wiring test passed on "unknown provider" instead of on
    "bench-only" — a green test proving the wrong thing. The assertions below make that
    failure mode impossible to reintroduce silently.
    """
    registry = _registry()
    candidates = [p for p in cascade_config.CASCADE_WIRING["stt"]
                  if p in registry and p not in cascade_config.CASCADE_WIRING["stt_live"]]
    assert candidates, ("registry/wiring changed: no registered STT provider is "
                        "bench-only, so the `cunt` incident can no longer be reproduced")
    provider = candidates[0]
    assert provider in registry, f"{provider} is not a registry id"
    assert provider not in cascade_config.CASCADE_WIRING["stt_live"]
    return provider


def _keyed_env(**over):
    """An env where every cascade role holds a plausible non-empty key."""
    env = {
        "DEEPGRAM_API_KEY": "dg-" + "0" * 37,
        "ELEVENLABS_API_KEY": "el-" + "1" * 37,
        "ELEVENLABS_VOICE_ID": "voice-abc",
        "OPENROUTER_API_KEY": "or-" + "2" * 37,
        "NVIDIA_API_KEY": "nv-" + "3" * 37,
        "GOOGLE_API_KEY": "gg-" + "4" * 37,
        "OPENCODE_GO_API_KEY": "oc-" + "5" * 37,
        # the bench-only STT provider's key: present so that when a test swaps STT to it,
        # the ONLY thing that sinks the report is the missing live client, not a
        # coincidentally absent credential.
        "OPENAI_API_KEY": "sk-" + "6" * 37,
    }
    env.update(over)
    return env


class _Sentinel(httpx.AsyncBaseTransport):
    """An httpx transport that RECORDS every request and answers 200.

    c4 hinges on this: a "zero network" claim proven by a sentinel that can never fire is
    worthless (the /dev/tcp fails-closed trap). Every zero-network assertion in this file
    is paired with a positive control proving the very same sentinel DOES record when a
    key is present.
    """

    def __init__(self):
        self.requests = []

    def handle_request(self, request):  # sync arm, unused but kept honest
        self.requests.append(request)
        return httpx.Response(200, json={})

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={})


# --------------------------------------------------------------------------------------
# c3 — live_wiring resolves in THIS process against THIS process's env
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_wiring_fails_on_bench_only_stt_and_names_it():
    """A bench-only STT provider must FAIL, and the detail must name the provider.

    This is the `cunt` incident class: the provider is registered, cascade-wired and
    keyable, but is NOT in CASCADE_WIRING["stt_live"], so the bridge refuses the call at
    dial time. The Talk lane had no stanza that could catch it.
    """
    provider = _bench_only_stt()

    report = await readiness.cascade_readiness(
        _doc(stt=provider), _registry(), _keyed_env(), transport=_Sentinel())

    wiring = report["checks"]["live_wiring"]
    assert wiring["status"] == "fail"
    assert provider in wiring["detail"]


@pytest.mark.asyncio
async def test_live_wiring_passes_when_all_three_stages_have_live_clients():
    report = await readiness.cascade_readiness(
        _doc(), _registry(), _keyed_env(), transport=_Sentinel())
    assert report["checks"]["live_wiring"]["status"] == "pass"


@pytest.mark.asyncio
async def test_live_wiring_reads_the_env_it_is_handed_not_os_environ(monkeypatch):
    """The whole point of the endpoint: the verdict must come from the env passed in
    (which the route fills from THIS process), never from a global the caller controls."""
    monkeypatch.setenv("DEEPGRAM_API_KEY", "")
    report = await readiness.cascade_readiness(
        _doc(), _registry(), _keyed_env(), transport=_Sentinel())
    # os.environ's DEEPGRAM is empty, the handed env's is not -> the handed env wins.
    assert report["checks"]["live_wiring"]["status"] == "pass"


# --------------------------------------------------------------------------------------
# c4 — the REAL probe_batch, with probe discipline intact
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_keyless_provider_is_needs_key_with_zero_network():
    """Keyless -> needs_key, and NOT ONE byte of network. Paired with the positive
    control below, which proves this very sentinel does record when a key exists."""
    sentinel = _Sentinel()
    env = _keyed_env(DEEPGRAM_API_KEY="", ELEVENLABS_API_KEY="",
                     OPENROUTER_API_KEY="")

    report = await readiness.cascade_readiness(
        _doc(), _registry(), env, transport=sentinel)

    assert report["checks"]["providers"]["status"] == "fail"
    statuses = {r["role"]: r["status"] for r in report["checks"]["providers"]["roles"]}
    assert set(statuses.values()) == {"needs_key"}
    assert sentinel.requests == [], "keyless probe must make ZERO network calls"


@pytest.mark.asyncio
async def test_positive_control_the_sentinel_does_record_when_keyed():
    """The control for the assertion above. Without this, `requests == []` could mean
    'zero network' OR 'this sentinel is incapable of recording anything'."""
    sentinel = _Sentinel()
    await readiness.cascade_readiness(
        _doc(), _registry(), _keyed_env(), transport=sentinel)
    assert sentinel.requests, "sentinel never fired even WITH keys — it cannot prove anything"


@pytest.mark.asyncio
async def test_providers_uses_the_real_probe_batch_not_a_reimplementation():
    """Guards against a duplicated lookup drifting from the shared probe discipline —
    the same class of bug the live_wiring docstring calls out."""
    from voicecore import probes
    calls = []
    real = probes.probe_batch

    async def spy(entries, env=None, transport=None):
        calls.append(sorted(e["id"] for e in entries))
        return await real(entries, env=env, transport=transport)

    probes.probe_batch = spy
    try:
        await readiness.cascade_readiness(
            _doc(), _registry(), _keyed_env(), transport=_Sentinel())
    finally:
        probes.probe_batch = real
    assert calls, "readiness did not go through probes.probe_batch"


# --------------------------------------------------------------------------------------
# c5 — eventlog_writable ATTEMPTS an append (a mode-bits guess does not count)
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_eventlog_writable_fails_on_a_read_only_path(tmp_path):
    """The bug this covers is invisible to a stat() check: the file can be
    mode-permissive and still un-appendable by THIS uid, or vice versa. So the stanza
    must actually open it for append."""
    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    target = ro_dir / "voice_events.jsonl"
    target.write_text("")
    os.chmod(target, 0o444)
    os.chmod(ro_dir, 0o555)
    try:
        if os.access(target, os.W_OK):
            pytest.skip("running as root — cannot construct an unwritable path")
        report = await readiness.cascade_readiness(
            _doc(), _registry(), _keyed_env(), transport=_Sentinel(),
            eventlog_path=str(target))
        assert report["checks"]["eventlog_writable"]["status"] == "fail"
    finally:
        os.chmod(ro_dir, 0o755)
        os.chmod(target, 0o644)


@pytest.mark.asyncio
async def test_eventlog_writable_passes_and_leaves_no_junk_behind(tmp_path):
    """Readiness is READ-ONLY (c2): proving appendability must not leave a bogus event
    in a log the s14b campaign will be read as evidence."""
    target = tmp_path / "voice_events.jsonl"
    target.write_text('{"pre": true}\n')
    before = target.read_text()

    report = await readiness.cascade_readiness(
        _doc(), _registry(), _keyed_env(), transport=_Sentinel(),
        eventlog_path=str(target))

    assert report["checks"]["eventlog_writable"]["status"] == "pass"
    assert target.read_text() == before, "readiness probe polluted the event log"


# --------------------------------------------------------------------------------------
# c6 — no key material anywhere in the payload
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_key_value_appears_anywhere_in_the_serialized_report():
    """Values, not names. The report names providers and roles freely; it must never
    echo a credential, in any stanza, at any nesting depth."""
    env = _keyed_env()
    report = await readiness.cascade_readiness(
        _doc(), _registry(), env, transport=_Sentinel())
    body = json.dumps(report)
    for name, value in env.items():
        assert value not in body, f"{name}'s VALUE leaked into the readiness payload"


@pytest.mark.asyncio
async def test_no_key_value_leaks_on_the_failure_path_either():
    """Failure details are where secrets usually escape — a 401 handler that echoes the
    request. Same assertion against a report where every stanza is failing."""
    env = _keyed_env()

    class _Boom(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(401, json={"error": "bad key"})

    report = await readiness.cascade_readiness(
        _doc(stt=_bench_only_stt()), _registry(), env, transport=_Boom(),
        eventlog_path="/nonexistent/dir/voice_events.jsonl")
    body = json.dumps(report)
    for name, value in env.items():
        assert value not in body, f"{name}'s VALUE leaked on the failure path"


# --------------------------------------------------------------------------------------
# ready roll-up
# --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ready_is_the_conjunction_of_every_stanza(tmp_path):
    """An explicit eventlog_path because the default is the container's /app/events —
    unreachable from a dev box, which would make `ready` false for the wrong reason."""
    log = str(tmp_path / "voice_events.jsonl")

    ok = await readiness.cascade_readiness(
        _doc(), _registry(), _keyed_env(), transport=_Sentinel(), eventlog_path=log)
    assert ok["ready"] is True

    # one failing stanza is enough to sink the roll-up
    bad = await readiness.cascade_readiness(
        _doc(stt=_bench_only_stt()), _registry(), _keyed_env(), transport=_Sentinel(),
        eventlog_path=log)
    assert bad["ready"] is False
    assert bad["checks"]["live_wiring"]["status"] == "fail"
    assert bad["checks"]["providers"]["status"] == "pass"   # the sink is wiring, not keys


# --------------------------------------------------------------------------------------
# c7 — the DURABLE eventlog fix, proven on a FRESHLY CREATED file
# --------------------------------------------------------------------------------------

def test_a_freshly_created_eventlog_is_group_and_other_writable(tmp_path):
    """The live `chmod 666` from s14a-1 patched the EXISTING file. It does nothing if the
    file is ever deleted and recreated by root — which is exactly how the bug arose. So
    this must be proven on a file append_event creates from scratch, never a pre-existing
    one.
    """
    target = tmp_path / "fresh" / "voice_events.jsonl"
    assert not target.exists()

    eventlog.append_event({"probe": "s14a2a"}, path=str(target))

    assert target.exists()
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode & stat.S_IWGRP, f"group-write missing (mode {mode:o})"
    assert mode & stat.S_IWOTH, f"other-write missing (mode {mode:o})"


@pytest.mark.asyncio
async def test_readiness_probe_creating_the_log_leaves_it_multi_uid_writable(tmp_path):
    """The detector must not reintroduce the bug it detects.

    `open(path, "a")` CREATES the file, so a readiness poll that lands before the first real
    event is itself the creating writer. If it leaves a 0644 file owned by this uid, the
    OTHER bridge is locked out and its appends fail silently — exactly the c7 bug, arriving
    through the readiness path instead of the logging path. Found by the s14a-2a evaluator,
    not by the original criteria.
    """
    target = tmp_path / "fresh" / "voice_events.jsonl"
    assert not target.exists()

    report = await readiness.cascade_readiness(
        _doc(), _registry(), _keyed_env(), transport=_Sentinel(),
        eventlog_path=str(target))

    assert report["checks"]["eventlog_writable"]["status"] == "pass"
    assert target.exists(), "probe reported writable without proving it"
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode & stat.S_IWGRP and mode & stat.S_IWOTH, \
        f"readiness created a log the other bridge cannot append to (mode {mode:o})"
    assert target.read_text() == "", "readiness probe wrote CONTENT to the event log"


def test_append_event_still_swallows_errors_after_the_chmod_change():
    """The chmod must not turn the best-effort logger into something that raises into a
    LIVE call path. A broken log must never drop a call."""
    eventlog.append_event({"probe": "x"}, path="/proc/1/cannot-write-here.jsonl")
