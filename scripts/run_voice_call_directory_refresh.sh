#!/usr/bin/env bash
# Weekly refresh of data/voice_call_directory.json on hermes-poc-01.
set -euo pipefail

APP_DIR="${ROBIE_APP_DIR:-/opt/renewal-automation-system}"
LOCK="${ROBIE_VOICE_DIR_LOCK:-/tmp/robie_voice_directory_refresh.lock}"
TIMEOUT_SECS="${ROBIE_VOICE_DIR_TIMEOUT:-180}"
LOG_DIR="${APP_DIR}/logs"
mkdir -p "${LOG_DIR}"
LOGFILE="${LOG_DIR}/robie_voice_directory_$(date +%Y-%m-%d).log"

resolve_python() {
    if [[ -n "${ROBIE_PYTHON:-}" && -x "${ROBIE_PYTHON}" ]]; then
        echo "${ROBIE_PYTHON}"
        return
    fi
    if [[ -x "${APP_DIR}/.venv/bin/python3" ]]; then
        echo "${APP_DIR}/.venv/bin/python3"
        return
    fi
    if [[ -x "${APP_DIR}/venv/bin/python3" ]]; then
        echo "${APP_DIR}/venv/bin/python3"
        return
    fi
    command -v python3
}

PYTHON="$(resolve_python)"
EXTRA_ARGS=(--refresh)
for arg in "$@"; do
    EXTRA_ARGS+=("${arg}")
done

exec 9>"${LOCK}"
if ! flock -n 9; then
    echo "$(date -Iseconds) skip: directory refresh already running (${LOCK})" | tee -a "${LOGFILE}"
    exit 0
fi

echo "$(date -Iseconds) start voice directory refresh extra=${EXTRA_ARGS[*]}" >> "${LOGFILE}"

cd "${APP_DIR}"
set +e
if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM "${TIMEOUT_SECS}" \
        env PYTHONPATH="${APP_DIR}${PYTHONPATH:+:${PYTHONPATH}}" \
        "${PYTHON}" -m src.voice.call_directory "${EXTRA_ARGS[@]}" >> "${LOGFILE}" 2>&1
    rc=$?
else
    PYTHONPATH="${APP_DIR}${PYTHONPATH:+:${PYTHONPATH}}" \
        "${PYTHON}" -m src.voice.call_directory "${EXTRA_ARGS[@]}" >> "${LOGFILE}" 2>&1
    rc=$?
fi
set -e

echo "$(date -Iseconds) finish voice directory refresh rc=${rc}" >> "${LOGFILE}"
exit "${rc}"
