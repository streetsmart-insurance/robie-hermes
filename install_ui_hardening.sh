#!/usr/bin/env bash
set -euo pipefail

ROOT=/opt/streetsmart-hermes/robie-job-engine
PYTHON=/opt/streetsmart-hermes/.hermes/hermes-agent/venv/bin/python
PKG_ROOT="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$ROOT/data/deploy-backups/ui-hardening-$STAMP"

install -d -m 0700 "$BACKUP"
for name in decisions.py sheets_sync.py; do
  if [[ -f "$ROOT/robie_job_engine/$name" ]]; then
    cp -p "$ROOT/robie_job_engine/$name" "$BACKUP/$name"
  fi
  install -m 0644 "$PKG_ROOT/robie_job_engine/$name" "$ROOT/robie_job_engine/$name"
done

PYTHONPYCACHEPREFIX=/tmp/robie-ui-hardening-pycache \
  "$PYTHON" -m py_compile \
  "$ROOT/robie_job_engine/decisions.py" \
  "$ROOT/robie_job_engine/sheets_sync.py"

PYTHONPATH="$ROOT" "$PYTHON" -c \
  "from robie_job_engine.decisions import DecisionStore; DecisionStore('$ROOT/data/jobs.db'); print('decision schema: ready')"

systemctl restart robie-scheduler.timer
systemctl start robie-scheduler.service
systemctl --no-pager --full status robie-scheduler.service || true

echo "ROBIE UI hardening installed. Backup: $BACKUP"
