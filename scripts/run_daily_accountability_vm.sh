#!/usr/bin/env bash
# Source of truth for streetsmart-accountability-prod:
#   /opt/streetsmart-daily-accountability/scripts/run_daily_accountability_vm.sh
# GitHub org search found no prior copy; the live tree was VM-only.
set -euo pipefail

# util-linux flock -w/--timeout accepts integer seconds only (not 15m).
# A suffix such as 15m is "invalid timeout value" and exits EX_USAGE (64)
# or EX_TEMPFAIL (75) in tens of milliseconds. That is not lock-busy
# timeout (exit 1). 900 seconds = 15 minutes.
FLOCK_WAIT_SECONDS="${ACCOUNTABILITY_FLOCK_WAIT_SECONDS:-900}"

APP_ROOT="${ACCOUNTABILITY_APP_ROOT:-/opt/streetsmart-daily-accountability}"
LOG_FILE="${ACCOUNTABILITY_LOG_FILE:-${APP_ROOT}/daily_run.log}"
LOCK_FILE="${ACCOUNTABILITY_LOCK_FILE:-${APP_ROOT}/data/run_state/accountability.lock}"
BUSY_MESSAGE="Accountability lock remained busy for 15 minutes"

log() {
  mkdir -p "$(dirname "${LOG_FILE}")"
  printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" | tee -a "${LOG_FILE}"
}

case "${FLOCK_WAIT_SECONDS}" in
  ''|*[!0-9]*)
    log "flock wait must be integer seconds on util-linux; got '${FLOCK_WAIT_SECONDS}'"
    exit 64
    ;;
esac

if [ ! -d "${APP_ROOT}" ]; then
  log "accountability app root is missing: ${APP_ROOT}"
  exit 66
fi

mkdir -p "$(dirname "${LOCK_FILE}")" "$(dirname "${LOG_FILE}")"

if [ -n "${ACCOUNTABILITY_PYTHON:-}" ]; then
  PYTHON="${ACCOUNTABILITY_PYTHON}"
elif [ -x "${APP_ROOT}/venv/bin/python" ]; then
  PYTHON="${APP_ROOT}/venv/bin/python"
else
  log "accountability python is missing: ${APP_ROOT}/venv/bin/python"
  exit 66
fi

if [ ! -x "${PYTHON}" ]; then
  log "accountability python is not executable: ${PYTHON}"
  exit 66
fi

exec 9>"${LOCK_FILE}"
flock_status=0
flock_err="$(mktemp)"
flock -w "${FLOCK_WAIT_SECONDS}" 9 2>"${flock_err}" || flock_status=$?
if [ -s "${flock_err}" ]; then
  tee -a "${LOG_FILE}" < "${flock_err}" >/dev/null
  cat "${flock_err}" >&2 || true
fi
rm -f "${flock_err}"

# Distinguish util-linux timeout/busy (exit 1) from usage/syntax errors
# (64 EX_USAGE, 75 EX_TEMPFAIL, or any other non-1 failure). Never map
# "invalid timeout value" onto the lock-busy message.
if [ "${flock_status}" -eq 1 ]; then
  log "${BUSY_MESSAGE}"
  exit 1
fi
if [ "${flock_status}" -ne 0 ]; then
  log "flock failed with a usage or system error (exit ${flock_status}); not a lock timeout. util-linux -w requires integer seconds."
  exit "${flock_status}"
fi

log "acquired accountability lock; starting python -m src.production_main --publish --deliver"
cd "${APP_ROOT}"
export PYTHONPATH="${APP_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
set +e
"${PYTHON}" -m src.production_main --publish --deliver 2>&1 | tee -a "${LOG_FILE}"
main_status=${PIPESTATUS[0]}
set -e
if [ "${main_status}" -ne 0 ]; then
  log "production_main exited ${main_status}"
  exit "${main_status}"
fi
log "production_main completed"
exit 0
