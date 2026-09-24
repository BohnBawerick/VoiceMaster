# Build context: services/
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy
USER root
# ffmpeg (ticket 07): the call-recording encoder.
RUN apt-get update && apt-get install -y --no-install-recommends pulseaudio pulseaudio-utils ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app/services
COPY voicecore/ voicecore/
COPY talk-voice-bridge/requirements.txt talk-voice-bridge/requirements.txt
RUN pip install --no-cache-dir -r talk-voice-bridge/requirements.txt \
 && pip install --no-cache-dir --no-deps -e voicecore \
 && python -m playwright install chromium
COPY talk-voice-bridge/ talk-voice-bridge/
WORKDIR /app/services/talk-voice-bridge
RUN chmod +x entrypoint.sh
USER pwuser
EXPOSE 3338
ENTRYPOINT ["./entrypoint.sh"]
