"""Effective-config preview for the Voice Control dashboard (s3, Mode V flavor).

Run INSIDE this service's venv by the dashboard's POST /api/agents/preview:
    .venv/bin/python preview_effective.py   (JSON on stdin, JSON on stdout)

Input:  {"doc": {<agent document / editor draft>}}
Output: {"bridge": "mode-v", "direction": "inbound", "as_if_selected": true,
         "agent_id": ..., "url": ..., "session_update": {...},
         "effective": {"retain": bool, "retain_source": "profile"|"env"}}
        or {"error": "<ProfileError message>"} (exit 0 — the dashboard maps it to 422).

NOT a reimplementation: the draft is staged as the only agent in a throwaway config
dir (mirroring the real registry resolution), selected via VOICE_AGENT, activated
through the REAL profiles.load_effective_profile path, overlaid with
config.overlay_profile onto config.load_base(), and the payload/URL come from the REAL
builders — RealtimeBridge._send_session_update captured off a fake ws, and
realtime_bridge.realtime_url — exactly how CallSession.start wires an inbound call
(hermes.build_system_prompt(cfg.config_dir, trust="owner"), profile threaded in).
"As-if-selected" means precisely the VOICE_AGENT override: the caller's own
VOICE_AGENT/active.yaml state never leaks into what is previewed.
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
        real_registry = profiles.config_dir() / "providers.yaml"
        if real_registry.is_file():
            shutil.copy(real_registry, stage / "providers.yaml")

        os.environ[profiles.ENV_CONFIG_DIR] = str(stage)
        os.environ[profiles.ENV_AGENT] = aid

        import config
        import hermes
        import realtime_bridge
        from approval import ApprovalStore

        try:
            snapshot = profiles.load_effective_profile(
                "inbound", outlet=profiles.OUTLET_TALK)
        except profiles.ProfileError as exc:
            print(json.dumps({"error": str(exc)}))
            return 0

        # Mirrors CallSession.start's inbound path: ONE snapshot overlays the base
        # Config AND rides into the bridge, so URL and session.update share it.
        cfg = config.overlay_profile(config.load_base(), snapshot)
        url = realtime_bridge.realtime_url(cfg)
        prompt = hermes.build_system_prompt(cfg.config_dir, trust="owner")
        bridge = realtime_bridge.RealtimeBridge(
            cfg, prompt, ApprovalStore(),
            token_ctx={"token": "preview", "caller": ""},
            mission=None, profile=snapshot)
        ws = _RecordingWS()
        asyncio.run(bridge._send_session_update(ws))
        session_update = json.loads(ws.raw[0])

        retain_profile = (None if snapshot is None
                          else (snapshot.doc.get("memory") or {}).get("retain"))
        effective = {
            "retain": cfg.retain_enabled,
            "retain_source": "profile" if isinstance(retain_profile, bool) else "env",
        }
        print(json.dumps({
            "bridge": "mode-v",
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
