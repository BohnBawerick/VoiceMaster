#!/usr/bin/env bash
# Integration tests for the profile-discovery half of hermes-supervisor.sh.
#
# Run from the repo root:
#
#     bash tests/test_supervisor.sh      (from the hermes/ directory)
#
# The supervisor is sourced with HERMES_SUPERVISOR_LIB=1, which skips main(), so
# the functions can be driven directly. The real hermes_profile_registry.py and
# the real python3 are used; only the `hermes` binary is faked. The fake records
# its argv and the API_SERVER_* environment it was handed, and either sleeps
# (healthy gateway) or exits immediately (crashing gateway) depending on a
# sentinel file, which is how the crash-isolation cases are exercised.

set -uo pipefail

HERMES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUPERVISOR="${HERMES_DIR}/supervisor/hermes-supervisor.sh"
REGISTRY_PY_SRC="${HERMES_DIR}/supervisor/hermes_profile_registry.py"
OVERLAYS_DIR="${HERMES_DIR}/supervisor/gateway_overlays"

PASS=0
FAIL=0

ok(){ PASS=$((PASS + 1)); echo "  ok   - $1"; }
bad(){ FAIL=$((FAIL + 1)); echo "  FAIL - $1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$3', got '$2')"; fi; }
check_contains(){ case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (expected '$2' to contain '$3')" ;; esac; }
section(){ echo; echo "== $1"; }

TMPROOT="$(mktemp -d)"
cleanup(){ pkill -P $$ 2>/dev/null; rm -rf "${TMPROOT}"; }
trap cleanup EXIT

# --- fake venv -------------------------------------------------------------
FAKE_BIN="${TMPROOT}/bin"
mkdir -p "${FAKE_BIN}"
cat > "${FAKE_BIN}/hermes" <<'FAKE'
#!/usr/bin/env bash
# Fake hermes. Records how it was invoked, then behaves as told.
profile="unknown"
prev=""
for a in "$@"; do
    if [ "$prev" = "-p" ]; then profile="$a"; fi
    prev="$a"
done
# Records the signal it was sent, so teardown can be asserted on rather than
# assumed. Without this, "nothing receives SIGTERM" looks identical to success.
trap 'echo "TERM" > "${FAKE_LOG_DIR}/${profile}.term"; exit 0' TERM
{
    echo "argv=$*"
    echo "pid=$$"
    echo "API_SERVER_PORT=${API_SERVER_PORT-<unset>}"
    echo "API_SERVER_HOST=${API_SERVER_HOST-<unset>}"
    echo "API_SERVER_ENABLED=${API_SERVER_ENABLED-<unset>}"
    echo "API_SERVER_KEY=${API_SERVER_KEY-<unset>}"
    echo "HERMES_INSTALL_TRANSCRIPTION_ROUTE=${HERMES_INSTALL_TRANSCRIPTION_ROUTE-<unset>}"
    echo "PYTHONPATH=${PYTHONPATH-<unset>}"
} > "${FAKE_LOG_DIR}/${profile}.launch"
if [ -f "${FAKE_LOG_DIR}/crash-${profile}" ]; then
    exit 3
fi
while true; do sleep 0.2 & wait $!; done
FAKE
chmod +x "${FAKE_BIN}/hermes"
ln -sf "$(command -v python3)" "${FAKE_BIN}/python"
printf '#!/bin/sh\nexit 0\n' > "${FAKE_BIN}/pip"
chmod +x "${FAKE_BIN}/pip"

export FAKE_LOG_DIR="${TMPROOT}/launches"
mkdir -p "${FAKE_LOG_DIR}"

# --- supervisor configuration ---------------------------------------------
PROFILES_ROOT="${TMPROOT}/profiles"
mkdir -p "${PROFILES_ROOT}"

# Never read the developer's real ~/.hermes/.env.
export HERMES_HOME="${TMPROOT}/hermes-home"
mkdir -p "${HERMES_HOME}"
export HERMES_SUPERVISOR_LIB=1
export VENV_BIN="${FAKE_BIN}"
export HERMES_REGISTRY_PY="${TMPROOT}/hermes_profile_registry.py"
cp "${REGISTRY_PY_SRC}" "${HERMES_REGISTRY_PY}"
export HERMES_PROFILES_DIR="${PROFILES_ROOT}"
export HERMES_GATEWAY_REGISTRY="${TMPROOT}/gateways/gateways.json"
export HERMES_GATEWAY_URL="http://localhost:18789"
export HERMES_PROFILE_BACKOFF_BASE=60
export API_SERVER_PORT=18789
export API_SERVER_KEY="inherited-secret-key="
export API_SERVER_HOST="0.0.0.0"
export API_SERVER_ENABLED="true"
export HERMES_WEBUI_PORT=8787

# Sourcing runs `set -euo pipefail`; the harness wants to keep going after a
# failed assertion, so relax it again straight after.
# shellcheck source=/dev/null
. "${SUPERVISOR}"
set +e

make_profile(){ # name [env-contents]
    local name="$1" envtext="${2-}"
    mkdir -p "${PROFILES_ROOT}/${name}"
    printf 'model: test\n' > "${PROFILES_ROOT}/${name}/config.yaml"
    if [ -n "${envtext}" ]; then
        printf '%s' "${envtext}" > "${PROFILES_ROOT}/${name}/.env"
    fi
}

wait_for_file(){ # path timeout-tenths
    local path="$1" tries="${2:-40}"
    while [ "${tries}" -gt 0 ]; do
        [ -f "${path}" ] && return 0
        sleep 0.1
        tries=$((tries - 1))
    done
    return 1
}

registry_get(){ # jq-ish: profile field
    python3 -c "
import json
d=json.load(open('${HERMES_GATEWAY_REGISTRY}'))
print(d['profiles'].get('$1', {}).get('$2'))
" 2>/dev/null
}

# ===========================================================================
section "creating a profile directory starts a gateway, with no env var and no redeploy"

unset HERMES_PROFILES
make_profile alpha
reconcile_once
check "alpha gateway was started" "${PROF_PID[alpha]:+yes}" "yes"
wait_for_file "${FAKE_LOG_DIR}/alpha.launch" || bad "alpha never launched"
check_contains "launched with -p alpha" "$(cat "${FAKE_LOG_DIR}/alpha.launch")" "argv=-p alpha gateway run"

section "the default profile deterministically owns the shared API port"

launch="$(cat "${FAKE_LOG_DIR}/alpha.launch")"
check_contains "extra profile did NOT inherit the shared port 18789" "${launch}" "API_SERVER_PORT=18790"
check_contains "extra profile bound to loopback, not 0.0.0.0" "${launch}" "API_SERVER_HOST=127.0.0.1"
check_contains "extra profile got the api server enabled" "${launch}" "API_SERVER_ENABLED=true"
check_contains "extra profile got the inherited key via env, not argv" "${launch}" "API_SERVER_KEY=inherited-secret-key="
check_contains "extra profile installs the transcription overlay" "${launch}" "HERMES_INSTALL_TRANSCRIPTION_ROUTE=1"
check_contains "extra profile puts the overlay on PYTHONPATH" "${launch}" "${OVERLAYS_DIR}"
argv_line="$(grep '^argv=' "${FAKE_LOG_DIR}/alpha.launch")"
case "${argv_line}" in *"inherited-secret-key"*) bad "the API key leaked onto the command line" ;;
    *) ok "the API key never appears in argv" ;; esac

section "the registry file the bridges read"

reconcile_once   # second pass records the live pid
check "registry records alpha as ok" "$(registry_get alpha status)" "ok"
check "registry publishes alpha's gateway url" "$(registry_get alpha gateway_url)" "http://127.0.0.1:18790"
check "registry records alpha as running" "$(registry_get alpha running)" "True"
check "bridge resolves alpha from the file" \
    "$(python3 "${HERMES_REGISTRY_PY}" --registry "${HERMES_GATEWAY_REGISTRY}" resolve alpha)" \
    "http://127.0.0.1:18790"

section "a running profile is not restarted on the next pass"

first_pid="${PROF_PID[alpha]}"
reconcile_once
check "alpha kept the same pid" "${PROF_PID[alpha]}" "${first_pid}"

section "a second profile appears while the first keeps running"

make_profile bravo
reconcile_once
check "bravo gateway was started" "${PROF_PID[bravo]:+yes}" "yes"
check "alpha was untouched" "${PROF_PID[alpha]}" "${first_pid}"
wait_for_file "${FAKE_LOG_DIR}/bravo.launch" || bad "bravo never launched"
check_contains "bravo got the next free port" "$(cat "${FAKE_LOG_DIR}/bravo.launch")" "API_SERVER_PORT=18791"

section "a profile with its own .env port keeps it, un-injected"

make_profile charlie "API_SERVER_PORT=18795
API_SERVER_KEY=charlie-own-key
"
reconcile_once
wait_for_file "${FAKE_LOG_DIR}/charlie.launch" || bad "charlie never launched"
launch="$(cat "${FAKE_LOG_DIR}/charlie.launch")"
check_contains "charlie's own port is left to its .env" "${launch}" "API_SERVER_PORT=<unset>"
check_contains "charlie's own key is left to its .env" "${launch}" "API_SERVER_KEY=<unset>"
reconcile_once   # a gateway is only advertised once it is confirmed running
check "registry advertises charlie on its own port" "$(registry_get charlie gateway_url)" "http://127.0.0.1:18795"

section "a crashing profile is isolated, never fatal to its neighbours"

touch "${FAKE_LOG_DIR}/crash-delta"
make_profile delta
reconcile_once
wait_for_file "${FAKE_LOG_DIR}/delta.launch" 40 || bad "delta never launched"
sleep 0.5
reconcile_once
check "delta was reaped" "${PROF_PID[delta]:+yes}" ""
check "delta has a failure counted" "${PROF_FAILS[delta]:-0}" "1"
check "delta is in backoff" "$([ "${PROF_NEXT_TRY[delta]:-0}" -gt "$(date +%s)" ] && echo yes)" "yes"
check "alpha survived delta's crash" "$(kill -0 "${PROF_PID[alpha]}" 2>/dev/null && echo alive)" "alive"
check "bravo survived delta's crash" "$(kill -0 "${PROF_PID[bravo]}" 2>/dev/null && echo alive)" "alive"
rm -f "${FAKE_LOG_DIR}/delta.launch"
reconcile_once
check "delta is not restarted while in backoff" "$([ -f "${FAKE_LOG_DIR}/delta.launch" ] && echo restarted)" ""
check "registry shows delta as not running" "$(registry_get delta running)" "False"

section "a half-created and an invalid profile do not stop anything else"

mkdir -p "${PROFILES_ROOT}/half"                      # mkdir only, no config.yaml
mkdir -p "${PROFILES_ROOT}/rotten"
printf 'a: [\n' > "${PROFILES_ROOT}/rotten/config.yaml"
make_profile echo9
reconcile_once
check "echo9 started despite the broken neighbours" "${PROF_PID[echo9]:+yes}" "yes"
check "half is reported as incomplete" "$(registry_get half status)" "incomplete"
check "rotten is reported as invalid" "$(registry_get rotten status)" "invalid"
check "rotten is not routable" "$(registry_get rotten gateway_url)" "None"
check "alpha still alive" "$(kill -0 "${PROF_PID[alpha]}" 2>/dev/null && echo alive)" "alive"

section "two profiles claiming the same port: deterministic winner, no crash"

make_profile yankee "API_SERVER_PORT=18800
"
make_profile xray "API_SERVER_PORT=18800
"
reconcile_once
wait_for_file "${FAKE_LOG_DIR}/xray.launch" 40 || bad "xray never launched"
reconcile_once   # confirm-running pass
check "xray (first by name) won the port" "$(registry_get xray gateway_url)" "http://127.0.0.1:18800"
check "yankee is reported as a conflict" "$(registry_get yankee status)" "conflict"
check "yankee was not started" "${PROF_PID[yankee]:+yes}" ""
check "alpha still alive through the conflict" "$(kill -0 "${PROF_PID[alpha]}" 2>/dev/null && echo alive)" "alive"

section "a deleted profile is stopped and dropped"

bravo_pid="${PROF_PID[bravo]}"
rm -rf "${PROFILES_ROOT:?}/bravo"
reconcile_once
check "bravo was stopped" "${PROF_PID[bravo]:+yes}" ""
sleep 0.3
check "bravo's process is gone" "$(kill -0 "${bravo_pid}" 2>/dev/null && echo alive)" ""
check "alpha still alive" "$(kill -0 "${PROF_PID[alpha]}" 2>/dev/null && echo alive)" "alive"

section "discovery failing leaves the running gateways alone"

alpha_pid="${PROF_PID[alpha]}"
saved_py="$(cat "${HERMES_REGISTRY_PY}")"
printf 'import sys\nsys.exit(9)\n' > "${HERMES_REGISTRY_PY}"
reconcile_once
rc=$?
check "reconcile reported the failure" "${rc}" "1"
check "alpha was not touched" "${PROF_PID[alpha]}" "${alpha_pid}"
check "alpha is still alive" "$(kill -0 "${alpha_pid}" 2>/dev/null && echo alive)" "alive"
printf '%s' "${saved_py}" > "${HERMES_REGISTRY_PY}"

section "a profile may not take a port another service in the netns is listening on"

# When the VoiceMaster services share the gateways' network namespace, 3336 is the
# phone bridge: a profile that holds it when the bridge restarts kills the inbound
# number.
make_profile modecthief "API_SERVER_PORT=3336
"
make_profile talkthief "API_SERVER_PORT=3338
"
make_profile vcthief "API_SERVER_PORT=3737
"
reconcile_once
check "the phone bridge's port was refused" "${PROF_PID[modecthief]:+yes}" ""
check "modecthief is a conflict" "$(registry_get modecthief status)" "conflict"
check_contains "and the log names who owns 3336" "$(registry_get modecthief error)" "phone bridge"
check "the Talk bridge's port was refused" "${PROF_PID[talkthief]:+yes}" ""
check "the dashboard's port was refused" "${PROF_PID[vcthief]:+yes}" ""
check "alpha survived all three" "$(kill -0 "${PROF_PID[alpha]}" 2>/dev/null && echo alive)" "alive"
rm -rf "${PROFILES_ROOT:?}/modecthief" "${PROFILES_ROOT:?}/talkthief" "${PROFILES_ROOT:?}/vcthief"

section "a profile directory named 'default' is refused, not started twice"

mkdir -p "${PROFILES_ROOT}/default"
printf 'model: test\n' > "${PROFILES_ROOT}/default/config.yaml"
reconcile_once
check "no second gateway over the default profile home" "${PROF_PID[default]:+yes}" ""
check "default is reported as invalid" "$(registry_get default status)" "invalid"
rm -rf "${PROFILES_ROOT:?}/default"

section "an entry that can never start is still reported (nothing vanishes silently)"

mkdir -p "${PROFILES_ROOT}/my agent"
printf 'model: test\n' > "${PROFILES_ROOT}/my agent/config.yaml"
ln -sf "${TMPROOT}/nowhere" "${PROFILES_ROOT}/danglinglink"
printf 'notes\n' > "${PROFILES_ROOT}/README.md"
reconcile_once
check "a real profile in a badly named directory reaches the registry" \
    "$(registry_get 'my agent' status)" "ignored"
check "a dangling symlink reaches the registry" "$(registry_get danglinglink status)" "ignored"
check "scratch files stay out of it" "$(registry_get README.md status)" "None"
check "alpha still alive" "$(kill -0 "${PROF_PID[alpha]}" 2>/dev/null && echo alive)" "alive"
rm -rf "${PROFILES_ROOT:?}/my agent" "${PROFILES_ROOT:?}/danglinglink" "${PROFILES_ROOT:?}/README.md"

section "no profiles directory degrades to today's single-gateway behaviour"

if ! no_profiles_tracked; then
    for name in "${!PROF_PID[@]}"; do
        kill -TERM "${PROF_PID[$name]}" 2>/dev/null
        unset "PROF_PID[${name}]"
    done
fi
EMPTY_ROOT="${TMPROOT}/empty"
PROFILES_DIR="${EMPTY_ROOT}"
reconcile_once
check "no gateways started" "$(no_profiles_tracked && echo none)" "none"
check "registry still written with an empty profile set" \
    "$(python3 -c "import json;print(json.load(open('${HERMES_GATEWAY_REGISTRY}'))['profiles'])")" "{}"
check "the default profile is still advertised" \
    "$(python3 -c "import json;print(json.load(open('${HERMES_GATEWAY_REGISTRY}'))['default']['gateway_url'])")" \
    "http://localhost:18789"
# PROFILES_DIR is read by the sourced supervisor, not by this file.
# shellcheck disable=SC2034
PROFILES_DIR="${PROFILES_ROOT}"

section "legacy fallback when the discovery script is absent from the image"

# REGISTRY_PY is read by the sourced supervisor, not by this file.
# shellcheck disable=SC2034
REGISTRY_PY="${TMPROOT}/does-not-exist.py"
# HERMES_PROFILES is read by the sourced supervisor, not by this file.
# shellcheck disable=SC2034
HERMES_PROFILES="legacyone, legacytwo"
legacy_profile_start
check "legacyone started" "${PROF_PID[legacyone]:+yes}" "yes"
check "legacytwo started" "${PROF_PID[legacytwo]:+yes}" "yes"
wait_for_file "${FAKE_LOG_DIR}/legacyone.launch" || bad "legacyone never launched"
check_contains "legacy child had the inherited API vars unset" \
    "$(cat "${FAKE_LOG_DIR}/legacyone.launch")" "API_SERVER_PORT=<unset>"

# ===========================================================================
# Everything above drives the supervisor's functions as a library, which is how
# main() came to be the one part of this script no test touched - and where the
# `set -e` + `wait -n` teardown bug lived. The rest of this file runs the REAL
# script as a process and signals it.
# ===========================================================================

MAINLAB="${TMPROOT}/mainlab"

# Start the real supervisor as its own process. Echoes the pid.
start_real_supervisor(){ # run-name [extra env assignments...]
    local run="$1"; shift
    local dir="${MAINLAB}/${run}"
    mkdir -p "${dir}/launches" "${dir}/profiles/mike"
    printf 'model: test\n' > "${dir}/profiles/mike/config.yaml"
    printf 'import time\nwhile True: time.sleep(0.2)\n' > "${dir}/webui.py"
    env -u HERMES_SUPERVISOR_LIB \
        FAKE_LOG_DIR="${dir}/launches" \
        VENV_BIN="${FAKE_BIN}" \
        WEBUI_SERVER="${dir}/webui.py" \
        HERMES_WEBUI_AGENT_DIR="${dir}" \
        HERMES_REGISTRY_PY="${HERMES_REGISTRY_PY}" \
        HERMES_PROFILES_DIR="${dir}/profiles" \
        HERMES_GATEWAY_REGISTRY="${dir}/gateways/gateways.json" \
        HERMES_PROFILE_SCAN_INTERVAL=1 \
        "$@" \
        bash "${SUPERVISOR}" > "${dir}/sup.out" 2>&1 &
    echo $!
}

wait_for_gateways(){ # dir
    wait_for_file "$1/launches/unknown.launch" 100 || return 1   # the default gateway
    wait_for_file "$1/launches/mike.launch" 100 || return 1      # a discovered profile
}

pid_of(){ sed -n 's/^pid=//p' "$1"; }

section "real main(): container shutdown reaches every gateway, not just the supervisor"

RUN="${MAINLAB}/term"
# SCAN_INTERVAL=30 ensures profile_supervisor_loop is inside interruptible_sleep.
# If interruptible_sleep stops being interruptible (e.g. reverted to a plain sleep),
# SIGTERM won't reach the profile gateway until the 30s sleep completes, causing
# the prompt 1.5s wait_for_file assertion below to fail.
SUP_PID="$(start_real_supervisor term HERMES_PROFILE_SCAN_INTERVAL=30)"
if ! wait_for_gateways "${RUN}"; then
    bad "the real supervisor never brought both gateways up"
else
    ok "the real supervisor started the default gateway and discovered mike"
    check_contains "default gateway installs the transcription overlay" \
        "$(cat "${RUN}/launches/unknown.launch")" "HERMES_INSTALL_TRANSCRIPTION_ROUTE=1"
    check_contains "default gateway puts the overlay on PYTHONPATH" \
        "$(cat "${RUN}/launches/unknown.launch")" "${OVERLAYS_DIR}"
    check_contains "discovered profile also installs the overlay" \
        "$(cat "${RUN}/launches/mike.launch")" "HERMES_INSTALL_TRANSCRIPTION_ROUTE=1"
    kill -TERM "${SUP_PID}" 2>/dev/null
    wait_for_file "${RUN}/launches/unknown.term" 60
    # 15 deciseconds = 1.5s deadline. Real interruptible_sleep takes ~0.1s; plain sleep takes 30s.
    if wait_for_file "${RUN}/launches/mike.term" 15; then
        ok "interruptible_sleep woke immediately on SIGTERM (mike received SIGTERM promptly)"
    else
        bad "profile gateway did not receive SIGTERM promptly during sleep (interruptible_sleep regression)"
    fi
    check "the default gateway received SIGTERM" \
        "$([ -f "${RUN}/launches/unknown.term" ] && echo yes)" "yes"
    check "the discovered profile's gateway received SIGTERM" \
        "$([ -f "${RUN}/launches/mike.term" ] && echo yes)" "yes"
    check_contains "the supervisor said it was passing the signal down" \
        "$(cat "${RUN}/sup.out")" "received SIGTERM"
    sleep 0.5
    check "no gateway was left orphaned" \
        "$(kill -0 "$(pid_of "${RUN}/launches/mike.launch")" 2>/dev/null && echo orphan)" ""
fi
kill -9 "${SUP_PID}" 2>/dev/null

section "real main(): a dying child tears the container down - and the teardown block RUNS"

# `wait -n` under `set -e` used to abort the script at the wait itself, so the log
# line and the `kill -TERM ${CHILD_PIDS}` after it were dead code and nothing was
# ever signalled. This is that path.
RUN="${MAINLAB}/died"
SUP_PID="$(start_real_supervisor died)"
if ! wait_for_gateways "${RUN}"; then
    bad "the real supervisor never brought both gateways up (died run)"
else
    kill -9 "$(pid_of "${RUN}/launches/unknown.launch")" 2>/dev/null   # default gateway dies
    wait_for_file "${RUN}/launches/mike.term" 80
    check_contains "the teardown block ran instead of being skipped by errexit" \
        "$(cat "${RUN}/sup.out")" "terminating siblings"
    check "the surviving profile gateway was signalled, not orphaned" \
        "$([ -f "${RUN}/launches/mike.term" ] && echo yes)" "yes"
    check "the supervisor exited" "$(kill -0 "${SUP_PID}" 2>/dev/null && echo alive)" ""
fi
kill -9 "${SUP_PID}" 2>/dev/null
pkill -9 -f "${MAINLAB}" 2>/dev/null

section "real main(): HERMES_PROFILES is ignored while discovery is available"

# An older supervisor starts extra gateways only from HERMES_PROFILES, so a
# deployment may keep it set. With discovery available it must change nothing.
RUN="${MAINLAB}/ignored"
SUP_PID="$(start_real_supervisor ignored HERMES_PROFILES=concierge)"
if ! wait_for_gateways "${RUN}"; then
    bad "the real supervisor never brought both gateways up (ignored run)"
else
    sleep 0.5
    check "no gateway was started from HERMES_PROFILES" \
        "$([ -f "${RUN}/launches/concierge.launch" ] && echo started)" ""
    check_contains "and the supervisor says so out loud" \
        "$(cat "${RUN}/sup.out")" "is set but IGNORED"
fi
kill -TERM "${SUP_PID}" 2>/dev/null
sleep 0.3
kill -9 "${SUP_PID}" 2>/dev/null

section "real main(): an install without the discovery script shouts instead of going quiet"

RUN="${MAINLAB}/nodiscovery"
SUP_PID="$(start_real_supervisor nodiscovery \
    HERMES_REGISTRY_PY="${TMPROOT}/does-not-exist.py" HERMES_PROFILES=concierge)"
wait_for_file "${RUN}/launches/concierge.launch" 100
sleep 0.5
check_contains "the banner names the missing file" \
    "$(cat "${RUN}/sup.out")" "PROFILE DISCOVERY IS NOT AVAILABLE IN THIS INSTALL"
check_contains "and tells the reader how to fix it" \
    "$(cat "${RUN}/sup.out")" "install hermes_profile_registry.py next to this script"
check "the legacy fallback still started concierge" \
    "$([ -f "${RUN}/launches/concierge.launch" ] && echo yes)" "yes"
check "and the default gateway is still answering" \
    "$([ -f "${RUN}/launches/unknown.launch" ] && echo yes)" "yes"
kill -TERM "${SUP_PID}" 2>/dev/null
sleep 0.3
kill -9 "${SUP_PID}" 2>/dev/null
pkill -9 -f "${MAINLAB}" 2>/dev/null

section "real main(): with WEBUI_SERVER unset only the gateways run"

# hermes-webui is a separate project. A stock install without it must not start
# a python process on an empty path, which would die and take the gateways down.
RUN="${MAINLAB}/nowebui"
SUP_PID="$(start_real_supervisor nowebui WEBUI_SERVER=)"
if ! wait_for_gateways "${RUN}"; then
    bad "the real supervisor never brought both gateways up (nowebui run)"
else
    sleep 1
    check_contains "the supervisor says webui was skipped" \
        "$(cat "${RUN}/sup.out")" "hermes-webui not started"
    check "the supervisor is still running without webui" \
        "$(kill -0 "${SUP_PID}" 2>/dev/null && echo alive)" "alive"
fi
kill -TERM "${SUP_PID}" 2>/dev/null
sleep 0.3
kill -9 "${SUP_PID}" 2>/dev/null
pkill -9 -f "${MAINLAB}" 2>/dev/null

echo
echo "================================"
echo "passed: ${PASS}  failed: ${FAIL}"
echo "================================"
[ "${FAIL}" -eq 0 ]
