#!/usr/bin/env bash
# ==============================================================================
# StreetSmart Insurance - Scheduled Carrier Audit Calls 9:00 AM EDT Runner
# ==============================================================================

set -e

APP_DIR="/opt/renewal-automation-system"
LOG_DIR="${APP_DIR}/logs"
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")
LOGFILE="${LOG_DIR}/scheduled_audit_calls_${TIMESTAMP}.log"

mkdir -p "${LOG_DIR}"

echo "=================================================================" >> "${LOGFILE}"
echo "🚀 ROBIE 1.0 Scheduled Audit Calls Runner Started at: ${TIMESTAMP}" >> "${LOGFILE}"
echo "=================================================================" >> "${LOGFILE}"

cd "${APP_DIR}"

# Execute the scheduled calls runner, passing through any arguments
PYTHONPATH=. "${APP_DIR}/venv/bin/python3" scripts/scheduled_audit_carrier_calls.py "$@" >> "${LOGFILE}" 2>&1

EXIT_CODE=$?

echo "=================================================================" >> "${LOGFILE}"
if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ ROBIE 1.0 Scheduled Audit Calls Runner Finished at: $(date +"%Y-%m-%d_%H-%M-%S")" >> "${LOGFILE}"
else
    echo "❌ Scheduled Audit Calls failed with exit code ${EXIT_CODE} at $(date +"%Y-%m-%d_%H-%M-%S")" >> "${LOGFILE}"
fi
echo "=================================================================" >> "${LOGFILE}"
exit $EXIT_CODE
