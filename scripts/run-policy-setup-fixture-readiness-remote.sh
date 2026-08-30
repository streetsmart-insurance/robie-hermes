#!/usr/bin/env bash
set -euo pipefail

test "$(hostname -s)" = hermes-test-01
test "${ROBIE_ENV:-}" = TEST
test "${TEST_ROOT:-}" = /opt/streetsmart-hermes-test
test "${EXPECTED_TEST_SHA:-}" = e18fcfa2ed13

job_db="${TEST_ROOT}/robie-job-engine/data/jobs.db"
test -f "${job_db}"
python3 - "${job_db}" <<'PY'
import sqlite3
import sys

con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
blocking = con.execute(
    """SELECT COUNT(*) FROM jobs
       WHERE status IN ('RUNNING','VERIFYING') OR lease_owner IS NOT NULL"""
).fetchone()[0]
con.close()
if blocking:
    raise SystemExit(f"fixture readiness refused: {blocking} active Test jobs/leases")
print("fixture_job_inventory=0")
PY

if ! systemctl is-active --quiet robie-gateway; then
  echo 'fixture readiness refused: Test gateway is not active' >&2
  exit 1
fi
gateway_environment="$(systemctl show robie-gateway -p Environment --value --no-pager)"
if ! grep -Fq 'ROBIE_CANONICAL_JOB_ENGINE_ROOT=/opt/streetsmart-hermes-test/releases/current' \
    <<<"${gateway_environment}"; then
  echo 'fixture readiness refused: Test gateway canonical root is missing' >&2
  exit 1
fi
if grep -Fq '/opt/streetsmart-hermes/releases/current' <<<"${gateway_environment}"; then
  echo 'fixture readiness refused: Test gateway references the Production release root' >&2
  exit 1
fi

python_bin=/opt/streetsmart-hermes-test/.hermes/hermes-agent/venv/bin/python
if ! test -x "${python_bin}"; then
  echo 'fixture readiness refused: Test browser Python runtime is missing' >&2
  exit 1
fi
sudo -u streetsmart-hermes env ROBIE_ENV=TEST \
  "${python_bin}" "${AUDIT_SCRIPT}" \
  --expected-sha "${EXPECTED_TEST_SHA}" \
  --cdp-url http://127.0.0.1:9222
