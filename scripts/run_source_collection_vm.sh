#!/usr/bin/env bash
# Weekday evening Magellan/SAD source collection.
# Source of truth for:
#   /opt/streetsmart-daily-accountability/scripts/run_source_collection_vm.sh
#
# production_main defaults to the previous business day when --date is omitted.
# --skip-if-prepared then reuses that prior day's snapshot when it is ready.
# Team Lead Chat EOD at 17:05 America/New_York verifies *today's* snapshot, so
# this wrapper always passes today's Eastern calendar day.
#
# No --publish, no --deliver, no Chat. Morning 09:00 stays on
# run_daily_accountability_vm.sh. This file was not in git before the
# 2026-09-25 same-day fix; install copies this script plus
# src/engine/collection_target.py and leaves production_main.py's default
# and the morning wrapper unchanged.
set -euo pipefail

PROJECT_ROOT="${ACCOUNTABILITY_APP_ROOT:-/opt/streetsmart-daily-accountability}"
VENV_PY="${ACCOUNTABILITY_PYTHON:-$PROJECT_ROOT/venv/bin/python}"
LOG_FILE="$PROJECT_ROOT/data/logs/source_collection.log"
LOCK_FILE="${SOURCE_COLLECTION_LOCK_FILE:-/tmp/streetsmart_source_collection.lock}"

mkdir -p "$PROJECT_ROOT/data/logs" "$PROJECT_ROOT/data/outputs"

# Same intentional-stop contract as the morning wrapper. The evening unit
# should list SuccessExitStatus=80 if it alerts on any nonzero status.
STOPPED_EXIT=80
on_stop() {
    trap - TERM ERR
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Received SIGTERM (intentional stop); exiting $STOPPED_EXIT so OnFailure does not alert." >> "$LOG_FILE"
    exit "$STOPPED_EXIT"
}
trap on_stop TERM

exec 9>"$LOCK_FILE"
if ! flock -w 900 9; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Source collection lock remained busy for 15 minutes." >> "$LOG_FILE"
    exit 75
fi

trap 'code=$?; echo "[$(date "+%Y-%m-%d %H:%M:%S")] Source collection FAILED (exit $code); no success was recorded." >> "$LOG_FILE"; exit "$code"' ERR

cd "$PROJECT_ROOT"
export PYTHONPATH="$PROJECT_ROOT"
TARGET_DATE="$("$VENV_PY" -m src.engine.collection_target --evening)"
if ! [[ "$TARGET_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Evening collect refused invalid target date: ${TARGET_DATE}" >> "$LOG_FILE"
    exit 2
fi

echo "=== [$(date '+%Y-%m-%d %H:%M:%S')] Evening source collection for Eastern day ${TARGET_DATE} ===" >> "$LOG_FILE"

# Collect only. Do not add --publish, --deliver, or a source wait that would
# hold this run past the 17:05 EOD check on purpose. Unverified empty Magellan
# extracts stay fail-closed on the publish/deliver path inside production_main.
timeout --signal=TERM --kill-after=30s 55m nice -n 10 \
    "$VENV_PY" -m src.production_main --skip-if-prepared --date "$TARGET_DATE" \
    >> "$LOG_FILE" 2>&1

echo "=== [$(date '+%Y-%m-%d %H:%M:%S')] Evening source collection complete for ${TARGET_DATE} ===" >> "$LOG_FILE"
