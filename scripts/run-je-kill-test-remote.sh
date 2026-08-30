#!/usr/bin/env bash
set -euo pipefail

required=(ROBIE_ENV EXPECTED_SHA TEST_ROOT FIXTURE RUN_ROOT)
for name in "${required[@]}"; do
  test -n "${!name:-}" || {
    echo "JE-KILL REFUSED: missing ${name}" >&2
    exit 2
  }
done

test "$(hostname -s)" = hermes-test-01
test "${ROBIE_ENV}" = TEST
test "${TEST_ROOT}" = /opt/streetsmart-hermes-test
test "${FIXTURE}" = /opt/streetsmart-hermes-test/je-kill/fixture.json
test "${RUN_ROOT}" = /opt/streetsmart-hermes-test/je-kill/runs

current="$(readlink -f "${TEST_ROOT}/current")"
releases_current="$(readlink -f "${TEST_ROOT}/releases/current")"
test -n "${current}"
test "${current}" = "${releases_current}"
case "${current}" in
  "${TEST_ROOT}/releases/${EXPECTED_SHA}/"*) ;;
  *) echo "Test release does not match workflow commit" >&2; exit 2 ;;
esac

test -f "${FIXTURE}" || {
  echo "JE-KILL FIXTURE MISSING: an approved disposable Test fixture is required" >&2
  exit 2
}

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
    raise SystemExit(f"JE-KILL REFUSED: {blocking} active Test Job(s) or lease(s)")
print("JE-KILL Test inventory: 0 blocking Jobs/leases")
PY

# Test Chrome / Job Engine run as streetsmart-hermes-test (uid 999).
# Production user paths do not exist on hermes-test-01.
test_user=streetsmart-hermes-test
id -u "${test_user}" >/dev/null
python_bin="${TEST_ROOT}/venv/bin/python"
test -x "${python_bin}"
test "${python_bin}" = /opt/streetsmart-hermes-test/venv/bin/python

# Python chdir's into the invoking cwd. Operator homes such as
# /home/carlo_streetsmart_insurance are not readable by the Test user.
cd "${TEST_ROOT}"
export HOME="${TEST_ROOT}"

install -d -o "${test_user}" -g "${test_user}" "${RUN_ROOT}"

runner_log="$(mktemp)"
trap 'rm -f -- "${runner_log}"' EXIT
if ! sudo -u "${test_user}" -H env \
    HOME="${TEST_ROOT}" \
    ROBIE_ENV=TEST \
    PYTHONPATH="${current}" \
    ROBIE_PLAYWRIGHT_CDP_URL=http://127.0.0.1:9222 \
    "${python_bin}" "${current}/scripts/run-je-kill-live-test.py" \
      --fixture "${FIXTURE}" \
      --run-root "${RUN_ROOT}" 2>&1 | tee "${runner_log}"; then
  echo "JE-KILL FAILED: live runner exited nonzero" >&2
  exit 3
fi
grep -Fq '"result": "TEST VERIFIED"' "${runner_log}" || {
  echo "JE-KILL UNVERIFIED: live runner did not emit TEST VERIFIED" >&2
  exit 3
}
echo "JE-KILL REMOTE VERIFIED"
