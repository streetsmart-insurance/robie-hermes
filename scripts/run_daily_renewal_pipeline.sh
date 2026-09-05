#!/usr/bin/env bash
# ==============================================================================
# StreetSmart Insurance - Autonomous Renewal Pipeline 5:00 AM Daily Runner
# ==============================================================================

set -e

APP_DIR="/opt/renewal-automation-system"
LOG_DIR="${APP_DIR}/logs"
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")
LOGFILE="${LOG_DIR}/renewal_pipeline_${TIMESTAMP}.log"

mkdir -p "${LOG_DIR}"

echo "=================================================================" >> "$LOGFILE"
echo "🚀 StreetSmart Renewal Daily Pipeline Started at: ${TIMESTAMP}" >> "$LOGFILE"
echo "=================================================================" >> "$LOGFILE"

cd "${APP_DIR}"

send_failure_alert() {
    local exit_code=$?
    echo "=================================================================" >> "$LOGFILE"
    echo "❌ CRITICAL: Pipeline crashed with exit code ${exit_code} at $(date +"%Y-%m-%d %H:%M:%S")" >> "$LOGFILE"
    echo "=================================================================" >> "$LOGFILE"

    PYTHONPATH=. "${APP_DIR}/venv/bin/python3" -c "
from src.email_outreach.gmail_client import GmailRenewalClient
try:
    with open('${LOGFILE}', 'r') as f:
        lines = f.readlines()
    tail = ''.join(lines[-40:])
    client = GmailRenewalClient()
    client.send_email(
        to=['carlo@streetsmart.insurance'],
        subject='🚨 ALERT: StreetSmart Daily Renewal Pipeline Failed on VM',
        body_text=f'The StreetSmart Renewal Pipeline encountered a fatal error on hermes-poc-01 with exit code ${exit_code}.\n\nLog tail (last 40 lines):\n----------------------------------------\n{tail}\n----------------------------------------\nLog file on server: ${LOGFILE}'
    )
    print('Dispatched failure alert email.')
except Exception as e:
    print(f'Failed to dispatch failure alert: {e}')
" >> "$LOGFILE" 2>&1 || true
}

trap send_failure_alert ERR

# Run the master pipeline and handoff reporter
PYTHONPATH=. "${APP_DIR}/venv/bin/python3" scripts/run_and_report_daily_pipeline.py >> "$LOGFILE" 2>&1

echo "=================================================================" >> "$LOGFILE"
echo "✅ StreetSmart Renewal Daily Pipeline Finished at: $(date +"%Y-%m-%d_%H-%M-%S")" >> "$LOGFILE"
echo "=================================================================" >> "$LOGFILE"
