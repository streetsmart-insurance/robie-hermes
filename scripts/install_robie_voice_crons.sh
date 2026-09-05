#!/usr/bin/env bash
# Idempotent installer for Robie Voice crons on hermes-poc-01.
#
# ADDS only these two lines if missing (does not replace or edit existing jobs):
#   3,8,13,18,23,28,33,38,43,48,53,58 * * * *  <APP>/scripts/run_robie_call_label_watch.sh
#   0 12 * * 0                                  <APP>/scripts/run_voice_call_directory_refresh.sh
#
# NEVER touches:
#   0 9 * * *  .../run_daily_renewal_pipeline.sh
#   20 * * * * .../run_daily_robie_cleaner.sh
#
# Usage:
#   scripts/install_robie_voice_crons.sh
#   scripts/install_robie_voice_crons.sh --dry-run
#   scripts/install_robie_voice_crons.sh --crontab-file /tmp/test.cron
set -euo pipefail

APP_DIR="${ROBIE_APP_DIR:-/opt/renewal-automation-system}"
DRY_RUN=0
CRONTAB_FILE=""

WATCH_MINUTES="3,8,13,18,23,28,33,38,43,48,53,58"
WATCH_SCHEDULE="${WATCH_MINUTES} * * * *"
DIR_SCHEDULE="0 12 * * 0"
WATCH_SCRIPT="${APP_DIR}/scripts/run_robie_call_label_watch.sh"
DIR_SCRIPT="${APP_DIR}/scripts/run_voice_call_directory_refresh.sh"

PROTECTED_MARKERS=(
    "run_daily_renewal_pipeline.sh"
    "run_daily_robie_cleaner.sh"
)

usage() {
    cat <<EOF
Usage: $0 [--dry-run] [--crontab-file PATH] [--app-dir PATH]

Idempotently add Robie Call watch + weekly directory refresh crontab lines.
Does not modify the 09:00 daily renewal pipeline or the :20 robie cleaner.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --crontab-file) CRONTAB_FILE="${2:-}"; shift 2 ;;
        --app-dir) APP_DIR="${2:-}"; WATCH_SCRIPT="${APP_DIR}/scripts/run_robie_call_label_watch.sh"; DIR_SCRIPT="${APP_DIR}/scripts/run_voice_call_directory_refresh.sh"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

WATCH_LINE="${WATCH_SCHEDULE} ${WATCH_SCRIPT}"
DIR_LINE="${DIR_SCHEDULE} ${DIR_SCRIPT}"

read_current() {
    if [[ -n "${CRONTAB_FILE}" ]]; then
        if [[ -f "${CRONTAB_FILE}" ]]; then
            cat "${CRONTAB_FILE}"
        else
            printf ""
        fi
    else
        crontab -l 2>/dev/null || true
    fi
}

current="$(read_current)"
# Preserve a trailing newline if present; normalize for append.
if [[ -n "${current}" && "${current}" != *$'\n' ]]; then
    current="${current}"$'\n'
fi

contains_script() {
    local script="$1"
    printf '%s' "${current}" | grep -F -q "${script}"
}

assert_protected_untouched() {
    local proposed="$1"
    local marker
    for marker in "${PROTECTED_MARKERS[@]}"; do
        local before after
        before="$(printf '%s' "${current}" | grep -F "${marker}" || true)"
        after="$(printf '%s' "${proposed}" | grep -F "${marker}" || true)"
        if [[ "${before}" != "${after}" ]]; then
            echo "Refusing to modify protected cron line matching ${marker}" >&2
            exit 3
        fi
    done
}

to_add=()
if ! contains_script "${WATCH_SCRIPT}"; then
    to_add+=("${WATCH_LINE}")
fi
if ! contains_script "${DIR_SCRIPT}"; then
    to_add+=("${DIR_LINE}")
fi

echo "Recommended crontab lines (UTC):"
echo "  ${WATCH_LINE}"
echo "  ${DIR_LINE}"

if [[ ${#to_add[@]} -eq 0 ]]; then
    echo "No changes: both Robie Voice cron lines already present."
    exit 0
fi

echo "Would add:"
for line in "${to_add[@]}"; do
    echo "  ${line}"
done

proposed="${current}"
for line in "${to_add[@]}"; do
    proposed+="${line}"$'\n'
done
assert_protected_untouched "${proposed}"

if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "Dry-run: crontab not written."
    exit 0
fi

if [[ -n "${CRONTAB_FILE}" ]]; then
    printf '%s' "${proposed}" > "${CRONTAB_FILE}"
    echo "Updated crontab file: ${CRONTAB_FILE}"
else
    printf '%s' "${proposed}" | crontab -
    echo "Updated user crontab."
fi
