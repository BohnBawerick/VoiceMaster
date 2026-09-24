#!/bin/bash
# Hermes Supervisor - runs `hermes gateway run` (and, optionally, hermes-webui)
# as sibling children. wait -n means the first child to die takes the whole
# supervisor down; run it under a restart policy (a container with
# `restart: unless-stopped`, a systemd unit with `Restart=always`) so both
# come back together.
#
# In a container, run this under an init process (`docker run --init` or
# compose `init: true`) so zombies are reaped; it does not do that itself.
#
# Paths default to a stock hermes-agent layout under HERMES_HOME (~/.hermes).
# See hermes/README.md for every variable.
#
# --- Profile discovery -----------------------------------------------------
# Extra per-profile gateways are NOT part of that fate-sharing set. They are
# started, watched and restarted by a discovery loop that runs in its own
# subshell, so a profile gateway dying is invisible to the `wait -n` below and
# cannot bounce the container that answers the phone.
#
# Discovery is by DIRECTORY, not by environment variable: anything under
# ${HERMES_HOME}/profiles/ with a config.yaml gets a gateway, and creating
# that directory is the whole act of creating a profile. No config edit, no
# restart, no dropped calls.
#
# The classification, port assignment and the registry file the voice bridges
# read all live in hermes_profile_registry.py. This script only starts, watches
# and stops processes.

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
SUPERVISOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load the Hermes env file (HERMES_WEBUI_*, OPENAI_*, API_SERVER_*, ...).
if [ -f "${HERMES_HOME}/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "${HERMES_HOME}/.env"
    set +a
fi

HERMES_AGENT_DIR="${HERMES_WEBUI_AGENT_DIR:-${HERMES_HOME}/hermes-agent}"
VENV_BIN="${VENV_BIN:-${HERMES_AGENT_DIR}/.venv/bin}"
# hermes-webui is a separate project. Set WEBUI_SERVER to its server.py to run it
# as a fate-shared sibling; leave it unset and only the gateways run.
WEBUI_SERVER="${WEBUI_SERVER:-}"

# --- Profile discovery configuration ---
REGISTRY_PY="${HERMES_REGISTRY_PY:-${SUPERVISOR_DIR}/hermes_profile_registry.py}"
PROFILES_DIR="${HERMES_PROFILES_DIR:-${HERMES_HOME}/profiles}"
# Written here, read by the voice bridges and the dashboard. VoiceMaster looks
# for it at ${VOICE_CONFIG_DIR}/gateways/gateways.json, so either point
# VOICE_CONFIG_DIR at ${HERMES_HOME}/voice-config or set this to a path inside
# the directory VoiceMaster uses.
REGISTRY_PATH="${HERMES_GATEWAY_REGISTRY:-${HERMES_HOME}/voice-config/gateways/gateways.json}"
DEFAULT_GATEWAY_URL="${HERMES_GATEWAY_URL:-http://localhost:${API_SERVER_PORT:-18789}}"
SCAN_INTERVAL="${HERMES_PROFILE_SCAN_INTERVAL:-15}"
# Restart backoff for a profile gateway that keeps dying.
BACKOFF_BASE="${HERMES_PROFILE_BACKOFF_BASE:-15}"
BACKOFF_MAX="${HERMES_PROFILE_BACKOFF_MAX:-600}"
# A gateway that stayed up this long is considered healthy; its failure count
# resets so a long-lived profile is not punished for one late crash.
HEALTHY_AFTER="${HERMES_PROFILE_HEALTHY_AFTER:-300}"

declare -A PROF_PID PROF_FAILS PROF_NEXT_TRY PROF_STARTED

log(){ echo "[supervisor] $*" >&2; }
now_s(){ date +%s; }

# `${#PROF_PID[@]}` on an EMPTY associative array is an "unbound variable" error
# under `set -u` (bash 5.2). Every loop over PROF_PID guards with this first,
# because the empty case is the normal single-profile container.
no_profiles_tracked(){ [ -z "${PROF_PID[*]+isset}" ]; }

# Gateway overlay directory. Puts POST /v1/audio/transcriptions onto every
# gateway process via sitecustomize. Only gateway children get these exports;
# webui must not inherit them.
hermes_gateway_env() {
    local overlay="${HERMES_GATEWAY_OVERLAYS:-${SUPERVISOR_DIR}/gateway_overlays}"
    export PYTHONPATH="${overlay}${PYTHONPATH:+:${PYTHONPATH}}"
    export HERMES_INSTALL_TRANSCRIPTION_ROUTE=1
}

# --- Profile gateway process management ------------------------------------

# "name:pid,name:pid" for the profiles we currently believe are running.
running_spec() {
    local out="" name
    if no_profiles_tracked; then
        printf ''
        return 0
    fi
    for name in "${!PROF_PID[@]}"; do
        out="${out}${out:+,}${name}:${PROF_PID[$name]}"
    done
    printf '%s' "${out}"
}

# Drop profiles whose gateway has exited, and schedule a backed-off retry.
# A dead profile gateway is a fact to report, never a reason to take anything
# else down.
reap_profiles() {
    local name pid fails delay uptime
    if no_profiles_tracked; then
        return 0
    fi
    for name in "${!PROF_PID[@]}"; do
        pid="${PROF_PID[$name]}"
        if kill -0 "${pid}" 2>/dev/null; then
            continue
        fi
        wait "${pid}" 2>/dev/null || true
        uptime=$(( $(now_s) - ${PROF_STARTED[$name]:-0} ))
        unset "PROF_PID[${name}]"
        unset "PROF_STARTED[${name}]"
        if [ "${uptime}" -ge "${HEALTHY_AFTER}" ]; then
            PROF_FAILS["${name}"]=0
        fi
        fails=$(( ${PROF_FAILS[$name]:-0} + 1 ))
        PROF_FAILS["${name}"]=${fails}
        delay=$(( BACKOFF_BASE * (1 << (fails > 6 ? 6 : fails - 1)) ))
        [ "${delay}" -gt "${BACKOFF_MAX}" ] && delay=${BACKOFF_MAX}
        PROF_NEXT_TRY["${name}"]=$(( $(now_s) + delay ))
        log "profile gateway '${name}' exited after ${uptime}s - isolated, retry in ${delay}s (failures=${fails})"
    done
}

# True while a crashed profile is serving its backoff.
backoff_blocked() {
    local name="$1"
    [ "$(now_s)" -lt "${PROF_NEXT_TRY[$name]:-0}" ]
}

# Spawn one profile gateway.
#
# The inherited API_SERVER_* variables are ALWAYS unset first. That is what stops
# an extra profile racing the default for the shared port 18789: the default
# profile is the only gateway that ever sees the supervisor's own API_SERVER_PORT,
# so it deterministically owns the port the phone line and the HTTP API use.
#
# Whatever the profile's own .env supplies is left to .env. Only the variables
# named in $inject are exported, from values resolved by the discovery pass. The
# API key is passed through the environment, never on the command line, so it
# does not show up in `ps`.
start_profile() {
    local name="$1" port="$2" host="$3" inject="$4"
    local inherited_key="${API_SERVER_KEY:-}"
    local fields=",${inject//+/,},"

    log "starting hermes gateway (profile: ${name}, port: ${port}, inject: ${inject})"
    (
        unset API_SERVER_ENABLED API_SERVER_KEY API_SERVER_HOST API_SERVER_PORT
        case "${fields}" in *,PORT,*) export API_SERVER_PORT="${port}" ;; esac
        case "${fields}" in *,HOST,*) export API_SERVER_HOST="${host}" ;; esac
        case "${fields}" in *,ENABLED,*) export API_SERVER_ENABLED="true" ;; esac
        case "${fields}" in *,KEY,*) export API_SERVER_KEY="${inherited_key}" ;; esac
        hermes_gateway_env
        exec "${VENV_BIN}/hermes" -p "${name}" gateway run
    ) &
    PROF_PID["${name}"]=$!
    PROF_STARTED["${name}"]=$(now_s)
}

stop_profile() {
    local name="$1" reason="$2" pid="${PROF_PID[$1]:-}"
    [ -z "${pid}" ] && return 0
    log "stopping profile gateway '${name}': ${reason}"
    kill -TERM "${pid}" 2>/dev/null || true
    unset "PROF_PID[${name}]"
    unset "PROF_STARTED[${name}]"
}

# One discovery pass: reap, ask the registry what should be running, act on it.
# Returns non-zero on a discovery failure; the caller keeps the existing gateways
# and tries again next tick.
reconcile_once() {
    local plan_file rc action name port host inject reason
    reap_profiles

    plan_file="$(mktemp)"
    # `|| rc=$?` rather than `set +e`: toggling errexit here would leak the
    # relaxed setting back to the caller.
    rc=0
    "${VENV_BIN}/python" "${REGISTRY_PY}" \
        --profiles-dir "${PROFILES_DIR}" \
        --registry "${REGISTRY_PATH}" \
        --default-url "${DEFAULT_GATEWAY_URL}" \
        reconcile --running "$(running_spec)" >"${plan_file}" || rc=$?
    if [ "${rc}" -ne 0 ]; then
        rm -f "${plan_file}"
        log "profile discovery failed (rc=${rc}) - existing gateways left alone"
        return 1
    fi

    while IFS=$'\t' read -r action name port host inject; do
        case "${action}" in
            START)
                [ -z "${name}" ] && continue
                if backoff_blocked "${name}"; then continue; fi
                start_profile "${name}" "${port}" "${host}" "${inject}"
                ;;
            STOP)
                # STOP reuses the third field for the reason.
                reason="${port}"
                stop_profile "${name}" "${reason:-no longer eligible}"
                ;;
            "") ;;
            *) log "unknown plan action: ${action}" ;;
        esac
    done < "${plan_file}"
    rm -f "${plan_file}"
    return 0
}

# Legacy path, used only when hermes_profile_registry.py cannot be found.
# Reproduces the pre-discovery comma-separated behaviour so an install without
# the registry still comes up the way it used to.
legacy_profile_start() {
    local prof
    [ -z "${HERMES_PROFILES:-}" ] && return 0
    log "discovery unavailable - falling back to HERMES_PROFILES='${HERMES_PROFILES}'"
    IFS=',' read -ra _legacy_profiles <<< "${HERMES_PROFILES}"
    for prof in "${_legacy_profiles[@]}"; do
        prof="$(echo "${prof}" | xargs)"
        [ -z "${prof}" ] && continue
        log "starting hermes gateway (legacy profile: ${prof})"
        (
            unset API_SERVER_ENABLED API_SERVER_KEY API_SERVER_HOST API_SERVER_PORT
            hermes_gateway_env
            exec "${VENV_BIN}/hermes" -p "${prof}" gateway run
        ) &
        PROF_PID["${prof}"]=$!
        PROF_STARTED["${prof}"]=$(now_s)
    done
}

# Sleep, but wake immediately on a signal. A plain `sleep` is a foreground child,
# and bash runs a trap only once the foreground command returns - which would make
# container teardown wait out a whole scan interval, longer than Docker's default
# 10s stop grace.
interruptible_sleep() {
    sleep "$1" &
    wait $! || true
}

# Shouted, not whispered. The deployment expects discovery and the running
# install cannot do it, so extra profiles simply do not exist and nothing else
# would ever say so.
discovery_unavailable_banner() {
    log "#############################################################"
    log "# PROFILE DISCOVERY IS NOT AVAILABLE IN THIS INSTALL"
    log "# missing: ${REGISTRY_PY}"
    log "# ${PROFILES_DIR} is NOT being scanned. A profile directory"
    log "# created there will NOT get a gateway, and no registry file"
    log "# is written for the voice bridges to read."
    log "# Extra gateways come only from HERMES_PROFILES='${HERMES_PROFILES:-<unset>}'."
    log "# FIX: install hermes_profile_registry.py next to this script,"
    log "#      or point HERMES_REGISTRY_PY at it."
    log "#############################################################"
}

# The discovery loop. Runs forever in its own subshell. Every failure inside is
# swallowed: this loop exiting would surface to the main `wait -n` and restart
# everything, which is exactly the fail-dead behaviour discovery exists to remove.
profile_supervisor_loop() {
    if [ ! -f "${REGISTRY_PY}" ]; then
        legacy_profile_start || true
        discovery_unavailable_banner
        # Deliberately NOT a refusal to start. Exiting here would take the phone
        # line down; degraded-but-loud keeps the default gateway answering calls
        # while repeating the banner until somebody fixes the install.
        while true; do interruptible_sleep 3600; discovery_unavailable_banner; done
    fi
    if [ -n "${HERMES_PROFILES:-}" ]; then
        log "HERMES_PROFILES='${HERMES_PROFILES}' is set but IGNORED - discovery is active."
        log "It is only read when discovery is unavailable."
    fi
    log "profile discovery watching ${PROFILES_DIR} every ${SCAN_INTERVAL}s (registry: ${REGISTRY_PATH})"
    while true; do
        reconcile_once || true
        interruptible_sleep "${SCAN_INTERVAL}"
    done
}

# Pass a shutdown signal down to every child, then leave.
#
# Without this trap, `docker stop` or a service restart killed this script
# instantly and NOTHING downstream ever saw a SIGTERM: the gateways were orphaned
# and SIGKILLed at teardown, with no chance to flush session state.
shutdown_children() {
    local sig="$1"
    trap - TERM INT
    log "received SIG${sig} - passing it to children (${CHILD_PIDS:-none})"
    if [ -n "${CHILD_PIDS:-}" ]; then
        # shellcheck disable=SC2086
        kill -TERM ${CHILD_PIDS} 2>/dev/null || true
    fi
    wait 2>/dev/null || true
    exit 0
}

main() {
    trap 'shutdown_children TERM' TERM
    trap 'shutdown_children INT' INT

    # Webui's only runtime dep is pyyaml (per its requirements.txt). Hermes venv
    # already includes it, but we install idempotently to be safe - a volume
    # mounted over the venv masks any image-build pip installs.
    # hermes_profile_registry.py wants pyyaml too, and degrades to "cannot
    # validate config.yaml" without it.
    log "ensuring webui deps present in agent venv"
    "${VENV_BIN}/pip" install --quiet --no-cache-dir pyyaml >/dev/null 2>&1 || true

    log "starting hermes gateway (default profile)"
    (
        hermes_gateway_env
        exec "${VENV_BIN}/hermes" gateway run
    ) &
    GATEWAY_PID=$!

    # Watched, restartable, and deliberately NOT fate-shared with the default.
    # shutdown_children TERMs this subshell, whose own trap TERMs the profile
    # gateways it owns, so they are not orphaned on container shutdown.
    (
        trap 'kill -TERM $(jobs -p) 2>/dev/null || true; exit 0' TERM INT
        while true; do profile_supervisor_loop; interruptible_sleep 5; done
    ) &
    PROFSUP_PID=$!

    WEBUI_PID=""
    if [ -n "${WEBUI_SERVER}" ]; then
        log "starting hermes-webui on :${HERMES_WEBUI_PORT:-8787}"
        cd "${HERMES_AGENT_DIR}"
        "${VENV_BIN}/python" "${WEBUI_SERVER}" &
        WEBUI_PID=$!
    else
        log "WEBUI_SERVER is not set - hermes-webui not started"
    fi

    CHILD_PIDS="${GATEWAY_PID}${WEBUI_PID:+ ${WEBUI_PID}} ${PROFSUP_PID}"
    log "supervising children=${CHILD_PIDS}"
    # `wait -n` WITHOUT the `|| DIED=$?` is the whole teardown block's undoing:
    # under `set -e` errexit fires AT the wait when a child exits non-zero, so the
    # log line and the kill below never run and nothing is ever signalled. Verified
    # both ways before this line was written.
    DIED=0
    wait -n || DIED=$?
    trap - TERM INT
    log "child exited (${DIED}) - terminating siblings; container will restart"
    # shellcheck disable=SC2086
    kill -TERM ${CHILD_PIDS} 2>/dev/null || true
    wait 2>/dev/null || true
    exit "${DIED}"
}

# Sourced by tests/test_supervisor.sh to exercise the functions above
# without starting anything.
if [ "${HERMES_SUPERVISOR_LIB:-0}" != "1" ]; then
    main "$@"
fi
