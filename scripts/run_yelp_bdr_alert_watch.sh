#!/usr/bin/env bash
# Yelp Lead & BDR Alert Watcher for hermes-poc-01.
# Runs via flock every 5 minutes to ingest new Yelp leads and dispatch BDR calls.
set -euo pipefail

APP_DIR="${YELP_APP_DIR:-/opt/renewal-automation-system}"
LOCK="/tmp/yelp_bdr_alert_watch.lock"
TIMEOUT_SECS=120
LOG_DIR="${APP_DIR}/logs"
mkdir -p "${LOG_DIR}"
LOGFILE="${LOG_DIR}/yelp_bdr_watch_$(date +%Y-%m-%d).log"

resolve_python() {
    if [[ -x "${APP_DIR}/venv/bin/python3" ]]; then
        echo "${APP_DIR}/venv/bin/python3"
        return
    fi
    if [[ -x "${APP_DIR}/.venv/bin/python3" ]]; then
        echo "${APP_DIR}/.venv/bin/python3"
        return
    fi
    command -v python3
}

PYTHON="$(resolve_python)"

exec 9>"${LOCK}"
if ! flock -n 9; then
    echo "$(date -Iseconds) skip: another Yelp BDR alert watch is currently running (${LOCK})" >> "${LOGFILE}"
    exit 0
fi

echo "$(date -Iseconds) [START] Yelp BDR alert watch initiated" >> "${LOGFILE}"

cd "${APP_DIR}"
set +e
if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM "${TIMEOUT_SECS}" \
        env PYTHONPATH="${APP_DIR}${PYTHONPATH:+:${PYTHONPATH}}" \
        "${PYTHON}" -m src.yelp.lead_manager --scan >> "${LOGFILE}" 2>&1
    rc=$?
else
    PYTHONPATH="${APP_DIR}${PYTHONPATH:+:${PYTHONPATH}}" \
        "${PYTHON}" -m src.yelp.lead_manager --scan >> "${LOGFILE}" 2>&1
    rc=$?
fi
set -e

echo "$(date -Iseconds) [FINISH] Yelp BDR alert watch completed rc=${rc}" >> "${LOGFILE}"
exit "${rc}"
