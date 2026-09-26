#!/usr/bin/env bash
# Team Lead Chat end-of-day wrap-up for streetsmart-accountability-prod.
# Source of truth for:
#   /opt/streetsmart-daily-accountability/scripts/run_team_lead_chat_eod.sh
# Posts Chat only. Does not publish the Doc, send leadership email, or run
# source collection. Separate lock from the morning and evening collect jobs.
set -euo pipefail

PROJECT_ROOT="${ACCOUNTABILITY_APP_ROOT:-/opt/streetsmart-daily-accountability}"
VENV_PY="${ACCOUNTABILITY_PYTHON:-$PROJECT_ROOT/venv/bin/python}"
LOG_FILE="$PROJECT_ROOT/data/logs/team_lead_chat_eod.log"
LOCK_FILE="${TEAM_LEAD_CHAT_EOD_LOCK_FILE:-/tmp/streetsmart_team_lead_chat_eod.lock}"

mkdir -p "$PROJECT_ROOT/data/logs" "$PROJECT_ROOT/data/run_state"

STOPPED_EXIT=80
on_stop() {
    trap - TERM
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Received SIGTERM (intentional stop); exiting $STOPPED_EXIT so OnFailure does not alert." >> "$LOG_FILE"
    exit "$STOPPED_EXIT"
}
trap on_stop TERM

exec 9>"$LOCK_FILE"
if ! flock -w 60 9; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Team Lead Chat EOD lock remained busy for 60 seconds." >> "$LOG_FILE"
    exit 75
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Team Lead Chat EOD." >> "$LOG_FILE"
cd "$PROJECT_ROOT"
set +e
PYTHONPATH="$PROJECT_ROOT" "$VENV_PY" -m src.reporters.team_lead_chat_eod >> "$LOG_FILE" 2>&1
code=$?
set -e
if [ "$code" -ne 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Team Lead Chat EOD failed (exit $code)." >> "$LOG_FILE"
    exit "$code"
fi
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Team Lead Chat EOD finished." >> "$LOG_FILE"
