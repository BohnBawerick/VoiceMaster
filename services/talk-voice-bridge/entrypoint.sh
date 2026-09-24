#!/usr/bin/env bash
set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/pulse-$(id -u)}"
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"
# A `docker restart` preserves /tmp, so a leftover PulseAudio pid/socket from the previous
# run makes a fresh daemon abort with "Daemon startup failed". Clear stale state first
# (both no-ops on a clean first boot / recreate).
pulseaudio -k 2>/dev/null || true
rm -rf "$XDG_RUNTIME_DIR"/pulse 2>/dev/null || true
pulseaudio -D --exit-idle-time=-1 --disallow-exit --disallow-module-loading=false
for i in $(seq 1 20); do pactl info >/dev/null 2>&1 && break; sleep 0.5; done
pactl info >/dev/null 2>&1 || { echo "FATAL: pulseaudio did not start" >&2; exit 1; }

RATE="${AUDIO_RATE:-24000}"
pactl load-module module-null-sink sink_name=talk_speaker \
  rate="$RATE" channels=1 sink_properties=device.description=talk_speaker
pactl load-module module-null-sink sink_name=talk_mic_sink \
  rate="$RATE" channels=1 sink_properties=device.description=talk_mic_sink
pactl load-module module-remap-source source_name=talk_mic master=talk_mic_sink.monitor \
  source_properties=device.description=talk_mic
pactl set-default-sink talk_speaker
pactl set-default-source talk_mic
export PULSE_SINK=talk_speaker PULSE_SOURCE=talk_mic

# Chromium must run HEADED to render live WebRTC/MediaStream audio to a real output
# device — headless decodes the remote track (getStats shows energy) but plays SILENCE to
# the talk_speaker sink, so the caller is never heard. This is the same reason Nextcloud's
# own talk-recording backend runs Chromium headed under Xvfb. Start a virtual X display
# and export DISPLAY so the Chromium that Playwright launches (headless=False) has a screen.
DISPLAY_NUM="${DISPLAY_NUM:-99}"
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}" 2>/dev/null || true
Xvfb ":${DISPLAY_NUM}" -screen 0 1280x720x24 -nolisten tcp &
export DISPLAY=":${DISPLAY_NUM}"
for i in $(seq 1 40); do [ -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && break; sleep 0.25; done
[ -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ] || { echo "FATAL: Xvfb did not start on :$DISPLAY_NUM" >&2; exit 1; }

# exec so uvicorn becomes PID 1 (direct SIGTERM → clean lifespan call-drain). Xvfb keeps
# running as a background child and is reaped when the container stops.
exec uvicorn server:app --host 0.0.0.0 --port "${PORT:-3338}"
