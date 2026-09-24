# Contributing

Thanks for looking. Issues and pull requests are welcome. For anything larger than a bug fix,
open an issue first so we can agree on the shape before you write it.

## Before you change anything

Read [`CONTEXT.md`](CONTEXT.md) (four terms carry the design), then
[`docs/decisions.md`](docs/decisions.md) and [`docs/adr/`](docs/adr/). [`AGENTS.md`](AGENTS.md)
lists the rules the code keeps; it is written for coding agents and for people alike.

## Development setup

Python 3.12 (the images use it; 3.13 works with the `audioop-lts` backport the requirements
pull in). Each service has its own virtualenv and installs `services/voicecore` editable.

```bash
cd services/voice
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt -e ../voicecore
.venv/bin/python -m pytest tests/ -q

cd ../talk-voice-bridge
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -e ../voicecore
.venv/bin/python -m pytest tests/ -q

cd ../voice-control
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt -e ../voicecore
.venv/bin/playwright install chromium          # the browser-level tests
.venv/bin/python -m pytest tests/ -q
```

Build the two bridge venvs before running the dashboard's suite: its effective-config preview
runs each bridge's own code in that bridge's `.venv`.

The dashboard UI is React + Vite in `services/voice-control/ui`:

```bash
cd services/voice-control/ui
npm ci
npm test
npm run build        # writes services/voice-control/static/
```

The Hermes add-ons have their own tests; see [`hermes/README.md`](hermes/README.md).

## Rules the code keeps

These come from real failures. A pull request that breaks one will be asked to change.

- **Sabotage each fix before trusting its test.** Remove the fix, run the test, expect red.
  Several times here a fixture agreed with the buggy code instead of with reality, and the test
  went green for the wrong reason.
- **`services/voice-control/static/` is committed and is one bundle holding every screen.**
  Rebuild it with `npm run build` when `ui/src` changes and commit the result. Resolve a merge
  conflict in `static/assets/` by deleting both sides and rebuilding, never by picking one.
  `tests/test_static_assets.py` lists the per-screen markers a new screen must add.
- **There is one placement.** `place_call.place_from_request` validates, refuses, dials and
  grades the answer. `POST /api/calls/place` and the scheduler both call it; do not add
  behaviour to the route alone.
- **One seam per job.** Off-call Hermes traffic goes through `voicecore/hermes_gateway.py`,
  on-call traffic through `voicecore/hermes_voice.py`, the archive write through
  `hindsight.retain_detached`, profile resolution through
  `hermes_gateway.gateway_url_for_profile`. A second copy is how two paths drift apart.
- **The call is the product.** Recording, archiving and summarising run off the call path,
  bounded, and never raise into call teardown.
- **Nothing is invented.** An absent field means "not recorded"; never write `""`, `"unknown"`
  or `0` in its place, and never summarise a call nobody spoke on.
- **There is no dry run.** A real call is the only proof that a configuration works. Say so
  in your pull request if you could not make one.

## Pull requests

- Keep each one to one change, with tests, and run the suites you touched.
- Never commit secrets, real phone numbers, recordings or transcripts. CI runs gitleaks on every
  push; use the fictional numbers already in the tests (`+61491570156`, `+15550100000`).
- Describe how you checked it: which suites, and whether a real call carried it.

By contributing you agree that your contribution is licensed under the MIT license.
