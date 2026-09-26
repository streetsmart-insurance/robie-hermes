#!/usr/bin/env bash
# ==============================================================================
# StreetSmart Insurance — Daily Accountability VM Runner
# Designed for headless automated execution on GCP Compute Engine.
# ==============================================================================
# Source of truth for streetsmart-accountability-prod:
#   /opt/streetsmart-daily-accountability/scripts/run_daily_accountability_vm.sh
# Based on the live copy read 2026-09-24
# (sha256 74c56724f60ca76d74affcbd136a196076cc651270027cc3439f0b98259fa358)
# plus the intentional-stop trap below. The ACCOUNTABILITY_* overrides exist
# for tests only; their defaults are the live values.
set -euo pipefail

PROJECT_ROOT="${ACCOUNTABILITY_APP_ROOT:-/opt/streetsmart-daily-accountability}"
VENV_PY="${ACCOUNTABILITY_PYTHON:-$PROJECT_ROOT/venv/bin/python}"
LOG_FILE="$PROJECT_ROOT/data/logs/daily_run.log"
LOCK_FILE="${ACCOUNTABILITY_LOCK_FILE:-/tmp/streetsmart_daily_accountability.lock}"

# Workspace DWD key lives in Secret Manager (never on disk).
export GOOGLE_DWD_SECRET="${GOOGLE_DWD_SECRET:-accountability-google-dwd-key}"

mkdir -p "$PROJECT_ROOT/data/logs" "$PROJECT_ROOT/data/outputs"

# An intentional `systemctl stop` sends SIGTERM to the whole unit cgroup.
# Untrapped, bash exits 143, Type=oneshot counts that as failure, and
# OnFailure= sends a false ACTION REQUIRED alert. Exit with a dedicated code
# the unit lists in SuccessExitStatus= instead. Real failures keep their own
# nonzero exit and still alert: the inner `timeout` expiring exits 124, and
# python killed on its own (not this wrapper) exits 143. A systemd
# TimeoutStartSec expiry is Result=timeout and alerts regardless of exit 80.
STOPPED_EXIT=80
on_stop() {
    trap - TERM ERR
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Received SIGTERM (intentional stop); exiting $STOPPED_EXIT so OnFailure does not alert." >> "$LOG_FILE"
    exit "$STOPPED_EXIT"
}
trap on_stop TERM

# Hold a real kernel lock for the entire run. The report can take more than
# eight minutes, so timestamp-based lock files are not sufficient protection.
exec 9>"$LOCK_FILE"
if ! flock -w 900 9; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Accountability lock remained busy for 15 minutes." >> "$LOG_FILE"
    exit 75
fi

trap 'code=$?; echo "[$(date "+%Y-%m-%d %H:%M:%S")] Accountability run FAILED (exit $code); no success was recorded." >> "$LOG_FILE"; exit "$code"' ERR

# Agency holiday calendar: do not issue an accountability report for a closed day.
TODAY=$(date '+%Y-%m-%d')
if PYTHONPATH="$PROJECT_ROOT" "$VENV_PY" -c 'from datetime import date; import sys; from src.engine.date_utils import federal_holidays; value=date.fromisoformat(sys.argv[1]); raise SystemExit(0 if value in federal_holidays(value.year) else 1)' "$TODAY"; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Agency closed for a federal holiday ($TODAY). Skipping automated morning run." >> "$LOG_FILE"
    exit 0
fi

echo "============================================================" >> "$LOG_FILE"
echo "=== [$(date '+%Y-%m-%d %H:%M:%S')] Starting StreetSmart Daily Accountability Run on GCP VM ===" >> "$LOG_FILE"
echo "============================================================" >> "$LOG_FILE"

# Fail-closed Production pipeline: exact scheduled attachments + Magellan +
# authoritative trackers -> persistent department-only Doc + department workbook.
cd "$PROJECT_ROOT"
# Run below normal CPU priority and stop an unhealthy browser/API wait after
# 45 minutes. A timed-out run sends no success notice and cron records failure.
timeout --signal=TERM --kill-after=30s 55m nice -n 10 \
    "$VENV_PY" -m src.production_main --publish --deliver \
    --prefer-prepared --source-wait-minutes 30 >> "$LOG_FILE" 2>&1

echo "=== [$(date '+%Y-%m-%d %H:%M:%S')] Run Complete Successfully ===" >> "$LOG_FILE"
