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

# Fail closed on expired Carlo approval or missing authenticated EZLynx tab
# before any process-death cycle mutates the disposable documents.
# Self-contained so an older Test release without je_kill_preflight.py still gates.
preflight_log="$(mktemp)"
runner_log="$(mktemp)"
trap 'rm -f -- "${preflight_log}" "${runner_log}"' EXIT
if ! "${python_bin}" - "${FIXTURE}" <<'PY' >"${preflight_log}" 2>&1
import json
import sys
from datetime import datetime, timezone
from urllib.request import urlopen

fixture_path = sys.argv[1]
errors = []
data = json.loads(open(fixture_path, encoding="utf-8").read())
if data.get("test_only") is not True or data.get("disposable") is not True:
    errors.append("fixture must declare test_only=true and disposable=true")
if str(data.get("approved_by") or "").strip() != "Carlo Ferrara":
    errors.append("approved_by must be 'Carlo Ferrara'")
if data.get("approval_scope") != "JE-KILL-01":
    errors.append("approval_scope must be 'JE-KILL-01'")
approved_at = str(data.get("approved_at") or "").strip()
try:
    approval_time = datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
except ValueError:
    errors.append("approved_at must be an ISO-8601 timestamp")
    approval_time = None
if approval_time is not None:
    if approval_time.tzinfo is None:
        errors.append("approved_at must include a timezone")
    else:
        age = (datetime.now(timezone.utc) - approval_time.astimezone(timezone.utc)).total_seconds()
        if age < -300 or age > 7 * 24 * 60 * 60:
            errors.append(
                f"fixture approval expired ({round(age/86400, 2)} days old); "
                "Carlo must re-stamp approved_at within seven days "
                "(bash scripts/refresh-je-kill-fixture-approval.sh REAPPROVE_JE_KILL_01_FIXTURE)"
            )
try:
    tabs = json.loads(urlopen("http://127.0.0.1:9222/json/list", timeout=5).read().decode())
except Exception as exc:  # noqa: BLE001 — surface any CDP failure
    errors.append(
        f"CDP unavailable at http://127.0.0.1:9222: {type(exc).__name__}: {exc}. "
        "Ensure robie-ezlynx-browser-test is running."
    )
    tabs = []
pages = [t for t in tabs if isinstance(t, dict) and t.get("type") == "page"]
eligible = [t for t in pages if "ezlynx.com" in str(t.get("url") or "").casefold()]
if len(eligible) != 1:
    errors.append(
        "expected exactly one EZLynx page tab for JE-KILL; "
        f"observed {len(eligible)}. Carlo: authenticate Test Chrome to "
        "app.ezlynx.com/web/ (not login)."
    )
elif True:
    url = str(eligible[0].get("url") or "").casefold()
    title = str(eligible[0].get("title") or "").casefold()
    if "login" in url or "signin" in url or title == "login":
        errors.append(
            "EZLynx Test session is on the login page. Carlo: complete "
            "Test EZLynx login/MFA on hermes-test-01 before JE-KILL."
        )
if errors:
    print("JE-KILL PREFLIGHT BLOCKED:")
    for item in errors:
        print(f"- {item}")
    raise SystemExit(2)
print("JE-KILL PREFLIGHT OK")
PY
then
  cat "${preflight_log}" >&2
  echo "JE-KILL REFUSED: preflight blocked (fixture approval and/or EZLynx session)" >&2
  exit 2
fi
cat "${preflight_log}"

# Python chdir's into the invoking cwd. Operator homes such as
# /home/carlo_streetsmart_insurance are not readable by the Test user.
cd "${TEST_ROOT}"
export HOME="${TEST_ROOT}"

install -d -o "${test_user}" -g "${test_user}" "${RUN_ROOT}"

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
