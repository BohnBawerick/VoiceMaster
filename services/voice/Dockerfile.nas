# Mode C Voice Pipeline — NAS Deployment (pinned base image)
# Build context: services/
FROM python:3.12-slim@sha256:3d5ed973e45820f5ba5e46bd065bd88b3a504ff0724d85980dcd05eab361fcf4

# ffmpeg (ticket 07): the call-recording encoder. Without it on PATH every call logs a
# refusal and produces no audio - the call itself is unaffected, but the feature is inert.
RUN apt-get update && apt-get install -y --no-install-recommends curl ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app/services
COPY voicecore/ voicecore/
COPY voice/requirements.txt voice/requirements.txt
RUN pip install --no-cache-dir -r voice/requirements.txt \
 && pip install --no-cache-dir --no-deps -e voicecore

WORKDIR /app/services/voice
COPY voice/server.py .
COPY voice/outbound.py .
COPY voice/preview_effective.py .

EXPOSE 3336

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:3336/health || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "3336"]
