#!/usr/bin/env bash
# Install the VoiceMaster pieces into a Hermes Agent home.
#
#   ./install.sh [--hermes-home DIR] [--prefix DIR] [--with-talk] [--dry-run] [--force]
#
# What goes where (defaults):
#   supervisor + registry   -> $PREFIX/                     ($PREFIX = $HERMES_HOME/voicemaster)
#   gateway overlays        -> $PREFIX/gateway_overlays/
#   skills                  -> $HERMES_HOME/skills/communication/{make-phone-call,manage-voice-agents}/
#   Nextcloud Talk plugin   -> $HERMES_HOME/plugins/nextcloud_talk/   (only with --with-talk)
#
# Idempotent: a file that is already identical is left alone. A file that exists
# and differs is NOT overwritten unless --force is given; the script reports it
# and exits non-zero. --dry-run prints what would happen and writes nothing.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
PREFIX=""
WITH_TALK=0
DRY_RUN=0
FORCE=0

usage() {
    sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --hermes-home) HERMES_HOME="$2"; shift 2 ;;
        --hermes-home=*) HERMES_HOME="${1#*=}"; shift ;;
        --prefix) PREFIX="$2"; shift 2 ;;
        --prefix=*) PREFIX="${1#*=}"; shift ;;
        --with-talk) WITH_TALK=1; shift ;;
        --dry-run|-n) DRY_RUN=1; shift ;;
        --force|-f) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

PREFIX="${PREFIX:-${HERMES_HOME}/voicemaster}"

CHANGED=0
SKIPPED=0
CONFLICTS=0

say() { echo "$*"; }

# install_file SRC DEST MODE
install_file() {
    local src="$1" dest="$2" mode="$3"
    if [ -e "${dest}" ]; then
        if cmp -s "${src}" "${dest}"; then
            SKIPPED=$((SKIPPED + 1))
            return 0
        fi
        if [ "${FORCE}" -ne 1 ]; then
            say "CONFLICT  ${dest} differs from ${src#"${SRC}"/} (use --force to overwrite)"
            CONFLICTS=$((CONFLICTS + 1))
            return 0
        fi
        say "overwrite ${dest}"
    else
        say "install   ${dest}"
    fi
    CHANGED=$((CHANGED + 1))
    if [ "${DRY_RUN}" -eq 1 ]; then
        return 0
    fi
    mkdir -p "$(dirname "${dest}")"
    cp "${src}" "${dest}.tmp.$$"
    chmod "${mode}" "${dest}.tmp.$$"
    mv -f "${dest}.tmp.$$" "${dest}"
}

# install_tree SRCDIR DESTDIR - every regular file, minus tests and caches.
install_tree() {
    local srcdir="$1" destdir="$2" rel mode
    while IFS= read -r -d '' f; do
        rel="${f#"${srcdir}"/}"
        mode=644
        [ -x "${f}" ] && mode=755
        install_file "${f}" "${destdir}/${rel}" "${mode}"
    done < <(find "${srcdir}" -type f \
                ! -path '*/__pycache__/*' ! -name '*.pyc' ! -path '*/tests/*' -print0 | sort -z)
}

[ "${DRY_RUN}" -eq 1 ] && say "(dry run - nothing will be written)"
say "HERMES_HOME=${HERMES_HOME}"
say "PREFIX=${PREFIX}"

install_file "${SRC}/supervisor/hermes-supervisor.sh"     "${PREFIX}/hermes-supervisor.sh"     755
install_file "${SRC}/supervisor/hermes_profile_registry.py" "${PREFIX}/hermes_profile_registry.py" 755
install_tree "${SRC}/gateway_overlays"                    "${PREFIX}/gateway_overlays"
install_tree "${SRC}/skills/make-phone-call"      "${HERMES_HOME}/skills/communication/make-phone-call"
install_tree "${SRC}/skills/manage-voice-agents"  "${HERMES_HOME}/skills/communication/manage-voice-agents"
if [ "${WITH_TALK}" -eq 1 ]; then
    install_tree "${SRC}/plugins/nextcloud_talk"  "${HERMES_HOME}/plugins/nextcloud_talk"
fi

say
say "changed: ${CHANGED}  unchanged: ${SKIPPED}  conflicts: ${CONFLICTS}"
if [ "${CONFLICTS}" -gt 0 ]; then
    exit 1
fi
if [ "${DRY_RUN}" -eq 0 ] && [ "${CHANGED}" -gt 0 ]; then
    say
    say "Next: run ${PREFIX}/hermes-supervisor.sh in place of 'hermes gateway run'."
    say "See the README in the VoiceMaster hermes/ directory for the environment it reads."
fi
