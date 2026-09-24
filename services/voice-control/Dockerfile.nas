# Voice Control dashboard image (pinned base, same as the phone bridge's Dockerfile.nas).
#
# BUILD CONTEXT = the repo's services/ directory (NOT this dir): the effective-config
# preview shells into the sibling bridge dirs' venvs (app.py PREVIEW_BRIDGES), and
# voicecore.profiles falls back to ../voice-config/providers.yaml — so the
# image replicates the repo layout under /app/services/. That copy of both bridges and
# voicecore is why this image must be rebuilt after every bridge or voicecore change.
# docker-compose.yml builds it; an install's own deploy scripts may tag it per commit.
FROM python:3.12-slim@sha256:3d5ed973e45820f5ba5e46bd065bd88b3a504ff0724d85980dcd05eab361fcf4

# The dashboard only READS recordings, so it needs no encoder — ffmpeg is deliberately
# absent here (ticket 07).
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app/services

COPY voice-config/ voice-config/
COPY voice/ voice/
COPY talk-voice-bridge/ talk-voice-bridge/
COPY voice-control/ voice-control/
COPY voicecore/ voicecore/

# Dashboard deps into the system python; each bridge gets its OWN venv at the
# exact path app.py probes (<bridge>/.venv/bin/python) so the preview runs the
# real builder code. Test-only deps are skipped for the bridges.
RUN pip install --no-cache-dir -r voice-control/requirements.txt \
 && pip install --no-cache-dir --no-deps -e voicecore \
 && python -m venv voice/.venv \
 && voice/.venv/bin/pip install --no-cache-dir -r voice/requirements.txt \
 && voice/.venv/bin/pip install --no-cache-dir --no-deps -e voicecore \
 && python -m venv talk-voice-bridge/.venv \
 && talk-voice-bridge/.venv/bin/pip install --no-cache-dir \
      fastapi 'uvicorn[standard]' httpx python-dotenv 'websockets>=14' \
      'playwright==1.48.*' pyyaml \
 && talk-voice-bridge/.venv/bin/pip install --no-cache-dir --no-deps -e voicecore

WORKDIR /app/services/voice-control

EXPOSE 3737

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:3737/healthz || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "3737"]
