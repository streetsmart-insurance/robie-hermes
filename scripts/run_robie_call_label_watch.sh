#!/usr/bin/env bash
# Robie Call label watcher for hermes-poc-01.
# flock + short timeout so overlapping offset-*/5 runs cannot pile up.
set -euo pipefail

APP_DIR="${ROBIE_APP_DIR:-/opt/renewal-automation-system}"
LOCK="${ROBIE_CALL_WATCH_LOCK:-/tmp/robie_call_label_watch.lock}"
TIMEOUT_SECS="${ROBIE_CALL_WATCH_TIMEOUT:-90}"
LOG_DIR="${APP_DIR}/logs"
mkdir -p "${LOG_DIR}"
LOGFILE="${LOG_DIR}/robie_call_watch_$(date +%Y-%m-%d).log"

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
EXTRA_ARGS=("$@")

exec 9>"${LOCK}"
if ! flock -n 9; then
    echo "$(date -Iseconds) skip: another Robie Call watch is running (${LOCK})" | tee -a "${LOGFILE}"
    exit 0
fi

echo "$(date -Iseconds) start robie call label watch timeout=${TIMEOUT_SECS}s extra=${EXTRA_ARGS[*]:-}" >> "${LOGFILE}"

cd "${APP_DIR}"
set +e
if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM "${TIMEOUT_SECS}" \
        env PYTHONPATH="${APP_DIR}${PYTHONPATH:+:${PYTHONPATH}}" \
        "${PYTHON}" -m src.voice.ezlynx_label_dispatcher --scan-queue "${EXTRA_ARGS[@]}" >> "${LOGFILE}" 2>&1
    rc=$?
else
    PYTHONPATH="${APP_DIR}${PYTHONPATH:+:${PYTHONPATH}}" \
        "${PYTHON}" -m src.voice.ezlynx_label_dispatcher --scan-queue "${EXTRA_ARGS[@]}" >> "${LOGFILE}" 2>&1
    rc=$?
fi
set -e

echo "$(date -Iseconds) finish robie call label watch rc=${rc}" >> "${LOGFILE}"
exit "${rc}"
