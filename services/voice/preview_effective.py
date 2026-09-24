"""Effective-config preview for the Voice Control dashboard (s3, Mode C flavor).

Run INSIDE this service's venv by the dashboard's POST /api/agents/preview:
    .venv/bin/python preview_effective.py   (JSON on stdin, JSON on stdout)

Input:  {"doc": {<agent document / editor draft>}}
Output: {"bridge": "mode-c", "direction": "inbound", "as_if_selected": true,
         "agent_id": ..., "url": ..., "session_update": {...},
         "effective": {"retain": bool, "retain_source": "profile"|"env"}}
        or {"error": "<ProfileError message>"} (exit 0 — the dashboard maps it to 422).

This is deliberately NOT a reimplementation of the payload/URL builders. The draft is
staged as the only agent in a throwaway config dir (mirroring the real dir's registry
resolution), selected via VOICE_AGENT, activated through the REAL
profiles.load_effective_profile path, and the payload/URL come from the REAL builders —
server._send_session_update captured off a fake ws, and server._realtime_url — exactly
as media_stream threads its one per-call snapshot. Everything else (retain env,
allow-list env, base prompt, module constants) is read from this process's inherited
env, i.e. the same snapshot the bridge would read. "As-if-selected" means precisely
this VOICE_AGENT override: the caller's own VOICE_AGENT/active.yaml state never leaks
into what is previewed.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


class _RecordingWS:
    """Captures what the builder would send over the OpenAI socket."""

    def __init__(self):
        self.raw = []

    async def send(self, data):
        self.raw.append(data)


def main() -> int:
    req = json.load(sys.stdin)
    doc = req["doc"]

    from voicecore import profiles
    aid = doc.get("id")
    if not isinstance(aid, str) or not profiles._ID_RE.match(aid):
        print(json.dumps({"error": f"id: {aid!r} is not a valid agent id"}))
        return 0

    import yaml
    with tempfile.TemporaryDirectory(prefix="voice-preview-") as td:
        stage = Path(td)
        (stage / "agents").mkdir()
        (stage / "agents" / f"{aid}.yaml").write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
        # Preserve the real registry resolution: if the live config dir carries its own
        # providers.yaml the staged dir must too, else both fall back to the canonical one.
        real_registry = profiles.config_dir() / "providers.yaml"
        if real_registry.is_file():
            shutil.copy(real_registry, stage / "providers.yaml")

        # As-if-selected: the draft wins over whatever VOICE_AGENT/active.yaml the
        # dashboard process carries. All other env (retain, allow-list, base prompt,
        # model/voice defaults) is inherited untouched.
        os.environ[profiles.ENV_CONFIG_DIR] = str(stage)
        os.environ[profiles.ENV_AGENT] = aid

        import server  # the Mode C bridge — the REAL builders live here

        try:
            snapshot = profiles.load_effective_profile(
                "inbound", outlet=profiles.OUTLET_PHONE)
        except profiles.ProfileError as exc:
            print(json.dumps({"error": str(exc)}))
            return 0

        url = server._realtime_url(profile=snapshot, direction="inbound")
        ws = _RecordingWS()
        token = server._CALL_PROFILE.set(snapshot)
        try:
            asyncio.run(server._send_session_update(
                ws, server.build_system_prompt(), outbound=False))
        finally:
            server._CALL_PROFILE.reset(token)
        session_update = json.loads(ws.raw[0])

        retain_profile = (None if snapshot is None
                          else (snapshot.doc.get("memory") or {}).get("retain"))
        effective = {
            "retain": (snapshot.retain_enabled(server.RETAIN_ENABLED)
                       if snapshot is not None else server.RETAIN_ENABLED),
            "retain_source": "profile" if isinstance(retain_profile, bool) else "env",
        }
        print(json.dumps({
            "bridge": "mode-c",
            "direction": "inbound",
            "as_if_selected": True,
            "agent_id": aid,
            "url": url,
            "session_update": session_update,
            "effective": effective,
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
