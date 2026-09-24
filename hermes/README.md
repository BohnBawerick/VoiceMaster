# Hermes-side pieces for VoiceMaster

VoiceMaster gives [Hermes Agent](https://github.com/NousResearch/hermes-agent) personas a phone
number. Most of it runs as its own services (`services/` in this repo), but a few pieces have to
run inside Hermes itself. This directory holds them, so you can add them to a stock
hermes-agent install.

We plan to offer these pieces upstream to NousResearch/hermes-agent. Until they land there,
install them from here.

## What is here

| Piece | Path | What it does | Status |
|---|---|---|---|
| Profile supervisor | `supervisor/hermes-supervisor.sh` | Runs the default `hermes gateway run`, then starts one extra gateway per Hermes profile it finds, restarts a crashed one with backoff, and stops one whose directory is removed. | Runs a live phone line in the reference deployment. |
| Profile registry | `supervisor/hermes_profile_registry.py` | Decides which profiles can start, gives each a port, and writes `gateways.json`. VoiceMaster's `voicecore/hermes_gateway.py` reads that file to find the gateway for an Agent's `hermes_profile`. Also a CLI: `show`, `resolve`, `doctor`. | Runs a live phone line in the reference deployment. |
| Transcription route | `gateway_overlays/` | Adds `POST /v1/audio/transcriptions` (OpenAI multipart shape) to every gateway, using that profile's own STT config. The dashboard's Mission dictation calls it. | Deployed in the reference setup against hermes-agent v0.21.3. |
| Nextcloud Talk plugin | `plugins/nextcloud_talk/` | A Hermes platform plugin: chat with an Agent in Nextcloud Talk, with an OWNER/GUEST tag on every message, and optional voice-call coordination with VoiceMaster's Talk voice bridge. | Experimental. Runs in the reference deployment; the Talk voice path is less proven than the phone path. |
| `make-phone-call` skill | `skills/make-phone-call/` | Lets an Agent place an outbound call through the phone or Talk bridge with a mission brief. | Works against the current bridges. |
| `manage-voice-agents` skill | `skills/manage-voice-agents/` | Lets an Agent read and edit VoiceMaster Agents through the dashboard API. | **Stale.** `activate`, `restore`, `validate` and `fire` target endpoints the dashboard no longer has. See the note at the top of its `SKILL.md`. |

## What these pieces assume about Hermes

- `hermes gateway run` starts the gateway, and `hermes -p <name> gateway run` starts one for a
  named profile.
- Profiles live in `$HERMES_HOME/profiles/<name>/`, each with a `config.yaml`.
- The gateway's HTTP API server is configured with `API_SERVER_ENABLED`, `API_SERVER_KEY`,
  `API_SERVER_HOST` and `API_SERVER_PORT` (default `18789`).
- The transcription overlay patches `gateway.platforms.api_server.APIServerAdapter._http_route_table`
  and calls `tools.transcription_tools.transcribe_audio`. If either name changes upstream, the
  overlay logs a warning and the gateway starts without the route.
- The Talk plugin subclasses `gateway.platforms.base.BasePlatformAdapter` and is loaded from
  `$HERMES_HOME/plugins/`.

These match hermes-agent v0.21.3. They have not been tested against other releases.

Two request fields VoiceMaster sends are only useful if the gateway honours them:
`tool_choice` (`none` keeps per-call summaries, Mission authoring and a tools-off direct-lane
Agent from acting; `auto` lets a tools-on Agent act) and the `X-Hermes-Session-Id` header (the
direct lane keeps one Hermes session per call). Neither is added by anything in this directory.
Check your hermes-agent release before relying on `tool_choice: none` as a guard.

## Install

With the script:

```bash
cd hermes
./install.sh --dry-run          # show what it would do
./install.sh                    # supervisor, overlays and both skills
./install.sh --with-talk        # also the Nextcloud Talk plugin
```

Options: `--hermes-home DIR` (default `$HERMES_HOME`, else `~/.hermes`), `--prefix DIR` (where the
supervisor, registry and overlays go; default `$HERMES_HOME/voicemaster`), `--force` to overwrite
files you have changed. The script leaves identical files alone, and without `--force` it refuses
to overwrite a file that differs and exits non-zero.

By hand, the same layout:

```bash
PREFIX=~/.hermes/voicemaster
mkdir -p "$PREFIX" ~/.hermes/skills/communication ~/.hermes/plugins
cp supervisor/hermes-supervisor.sh supervisor/hermes_profile_registry.py "$PREFIX/"
cp -r gateway_overlays "$PREFIX/"
cp -r skills/make-phone-call skills/manage-voice-agents ~/.hermes/skills/communication/
cp -r plugins/nextcloud_talk ~/.hermes/plugins/          # optional
```

Then run `$PREFIX/hermes-supervisor.sh` wherever you ran `hermes gateway run` before (a container
entrypoint, or a systemd unit with `Restart=always`). It exits when the default gateway exits, so
whatever supervises it restarts everything together. To add a profile, create
`$HERMES_HOME/profiles/<name>/` and write its `config.yaml` last. Within one scan interval the
profile gets a gateway and an entry in `gateways.json`. Check it with:

```bash
python3 "$PREFIX/hermes_profile_registry.py" doctor --expect <name>
```

### Connecting VoiceMaster to the registry

With the repository's `docker-compose.yml`, VoiceMaster's config directory is `data/voice-config`
in the clone, so run the supervisor with
`HERMES_GATEWAY_REGISTRY=/path/to/VoiceMaster/data/voice-config/gateways/gateways.json`.

VoiceMaster reads `${VOICE_CONFIG_DIR}/gateways/gateways.json`. The supervisor writes
`$HERMES_HOME/voice-config/gateways/gateways.json` by default. Either set VoiceMaster's
`VOICE_CONFIG_DIR` to `$HERMES_HOME/voice-config` (mounted into its containers if you use them),
or set `HERMES_GATEWAY_REGISTRY` on the supervisor to a path inside VoiceMaster's config
directory.

## Environment

Supervisor (`hermes-supervisor.sh`):

| Variable | Default | Meaning |
|---|---|---|
| `HERMES_HOME` | `~/.hermes` | Hermes home. `$HERMES_HOME/.env` is sourced at start. |
| `HERMES_WEBUI_AGENT_DIR` | `$HERMES_HOME/hermes-agent` | hermes-agent checkout; its `.venv/bin` holds `hermes`. |
| `VENV_BIN` | `$HERMES_WEBUI_AGENT_DIR/.venv/bin` | Where `hermes`, `python` and `pip` are. |
| `WEBUI_SERVER` | unset | Path to hermes-webui's `server.py`. Unset means webui is not started. |
| `HERMES_REGISTRY_PY` | next to the script | `hermes_profile_registry.py`. |
| `HERMES_PROFILES_DIR` | `$HERMES_HOME/profiles` | Where profiles are discovered. |
| `HERMES_GATEWAY_REGISTRY` | `$HERMES_HOME/voice-config/gateways/gateways.json` | The registry file written. |
| `HERMES_GATEWAY_URL` | `http://localhost:$API_SERVER_PORT` | The default profile's URL, as advertised in the registry. |
| `HERMES_GATEWAY_OVERLAYS` | `gateway_overlays/` next to the script | Put on `PYTHONPATH` for gateway processes only. |
| `HERMES_PROFILE_SCAN_INTERVAL` | `15` | Seconds between discovery passes. |
| `HERMES_PROFILE_BACKOFF_BASE` / `_MAX` | `15` / `600` | Restart backoff for a crashing profile gateway. |
| `HERMES_PROFILE_HEALTHY_AFTER` | `300` | Uptime after which a profile's failure count resets. |
| `HERMES_PROFILES` | unset | Comma list of profiles. Only read when the registry script is missing. |

Registry (`hermes_profile_registry.py`):

| Variable | Default | Meaning |
|---|---|---|
| `API_SERVER_PORT` | `18789` | The default gateway's port. No profile may take it. |
| `HERMES_WEBUI_PORT` | `8787` | Reserved against profiles. |
| `HERMES_RESERVED_PORTS` | unset | Extra ports no profile may take, comma separated. |
| `VOICE_CONFIG_DIR` | unset | Reader side: the registry is read from `$VOICE_CONFIG_DIR/gateways/gateways.json`. |
| `HERMES_PROFILE_GATEWAY_URLS` | unset | Reader fallback: `name=url,name=url` when there is no registry. |

Profiles get ports from `18790-18849`, and keep the same port across restarts. A profile can ask
for its own with `API_SERVER_PORT` in its `.env` or `api_server.port` in `config.yaml`. These
ports are always refused: `3336` (VoiceMaster phone bridge), `3338` (Talk voice bridge), `3737`
(dashboard), `8787` (hermes-webui), `9119` (hermes dashboard), `18789` (default gateway). They
matter when the bridges share a host or network namespace with Hermes. If anything else listens
there (sshd on a high port, a proxy), add its port to `HERMES_RESERVED_PORTS`.

Transcription overlay: `HERMES_INSTALL_TRANSCRIPTION_ROUTE=1` turns it on. The supervisor sets it
for gateway processes only.

Skills: `MODE_C_SIDECAR_URL` (phone bridge, default `http://localhost:3336`),
`TALK_VOICE_SIDECAR_URL` (Talk voice bridge, default `http://localhost:3338`), `VOICE_CONTROL_URL`
(dashboard, default `http://localhost:3737`). The bridges check `Authorization: Bearer` against
their `HERMES_GATEWAY_TOKEN`; `place_call.py` sends `HERMES_GATEWAY_TOKEN`, else `API_SERVER_KEY`.

Talk plugin: see `plugins/nextcloud_talk/plugin.yaml` for its variables (`NEXTCLOUD_BASE_URL`,
`NEXTCLOUD_TALK_USER`, `NEXTCLOUD_TALK_APP_PASSWORD`, owner and room settings,
`TALK_VOICE_SIDECAR_URL`).

## Tests

From this directory, with Python 3.10+ and `tests/requirements.txt` installed (a venv is fine):

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests -q
bash tests/test_supervisor.sh
```

Neither needs hermes-agent, a network or credentials. The supervisor test fakes the `hermes`
binary and runs the real script as a process. `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` keeps
unrelated globally installed pytest plugins out of the run.
