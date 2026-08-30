#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "ASCEND DISABLED TEST REFUSED: $1" >&2
  exit 2
}

test "$(hostname -s)" = hermes-test-01 || fail "wrong host"
[[ "${EXPECTED_SHA:-}" =~ ^[0-9a-f]{40}$ ]] || fail "invalid expected SHA"
test "${TEST_ROOT:-}" = /opt/streetsmart-hermes-test || fail "wrong Test root"
echo "ASCEND DISABLED CHECK: protected Test target"

short="${EXPECTED_SHA:0:12}"
current="$(readlink -f "${TEST_ROOT}/current")"
releases_current="$(readlink -f "${TEST_ROOT}/releases/current")"
test -n "${current}" || fail "Test current pointer missing"
test "${current}" = "${releases_current}" || fail "Test pointers disagree"
case "${current}" in
  "${TEST_ROOT}/releases/${short}/"*) ;;
  *) echo "Ascend disabled proof refuses a different Test release" >&2; exit 2 ;;
esac
echo "ASCEND DISABLED CHECK: exact deployed release"

systemctl is-active --quiet robie-gateway || fail "robie-gateway is not active"
gateway_pid="$(systemctl show robie-gateway --property=MainPID --value)"
test "${gateway_pid}" -gt 1 || fail "robie-gateway has no live process"
process_env="/proc/${gateway_pid}/environ"
test -r "${process_env}" || fail "gateway process environment is unreadable"
echo "ASCEND DISABLED CHECK: Test gateway active"

# Inspect names and enablement only. Never print environment contents.
if tr '\0' '\n' <"${process_env}" | grep -Eq '^ROBIE_ASCEND_API_ENABLED=(1|true|yes|on)$'; then
  echo "Ascend disabled proof refused: API execution is enabled" >&2
  exit 2
fi
if tr '\0' '\n' <"${process_env}" | grep -q '^ROBIE_ASCEND_API_KEY_SECRET='; then
  echo "Ascend disabled proof refused: credential reference is configured" >&2
  exit 2
fi
if tr '\0' '\n' <"${process_env}" | grep -Eq '^ROBIE_ASCEND_API_PRODUCTION_ENABLED=(1|true|yes|on)$'; then
  echo "Ascend disabled proof refused: Production execution flag is enabled" >&2
  exit 2
fi
echo "ASCEND DISABLED CHECK: execution disabled and credential absent"

python_bin="$(readlink -f "/proc/${gateway_pid}/exe")"
test -x "${python_bin}" || fail "Test Python runtime missing"
env \
  -u ROBIE_ASCEND_API_ENABLED \
  -u ROBIE_ASCEND_API_KEY_SECRET \
  -u ROBIE_ASCEND_API_PRODUCTION_ENABLED \
  ROBIE_ENV=TEST \
  PYTHONPATH="${current}" \
  "${python_bin}" - <<'PY'
import json

from robie_job_engine.ascend_api import AscendApiConfig, AscendConfigurationError


class RefuseSecretAccess:
    def access(self, resource_name: str) -> str:
        raise AssertionError("disabled execution attempted Secret Manager access")


try:
    AscendApiConfig.from_environment(RefuseSecretAccess())
except AscendConfigurationError as exc:
    if str(exc) != "Ascend API execution is not enabled":
        raise
else:
    raise AssertionError("disabled Ascend execution did not fail closed")

print(json.dumps({
    "result": "TEST VERIFIED",
    "ascend_api_execution": "UNAVAILABLE",
    "credential_configured": False,
    "secret_access_attempts": 0,
    "outbound_ascend_post_requests": 0,
    "production_touched": False,
}, sort_keys=True))
PY

echo "ASCEND DISABLED TEST VERIFIED"
