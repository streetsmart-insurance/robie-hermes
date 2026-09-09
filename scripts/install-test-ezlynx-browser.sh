#!/usr/bin/env bash
# Install only the isolated Test Chrome unit. Refuses Production and other hosts.
set -euo pipefail

EXPECTED_HOST="hermes-test-01"
TEST_ROOT="/opt/streetsmart-hermes-test"
TEST_USER="streetsmart-hermes-test"
UNIT_NAME="robie-ezlynx-browser-test.service"

[[ "${EUID}" -eq 0 ]] || { echo "installer requires sudo" >&2; exit 2; }
[[ "$(hostname -s)" == "${EXPECTED_HOST}" ]] || {
  echo "refusing non-Test host: $(hostname -f)" >&2
  exit 2
}

release_root="$(readlink -f "${TEST_ROOT}/releases/current")"
unit_source="${release_root}/deploy/systemd/${UNIT_NAME}"
[[ -f "${unit_source}" ]] || { echo "Test browser unit missing from current release" >&2; exit 2; }
grep -Fqx 'ConditionHost=hermes-test-01.c.streetsmart-hermes-poc.internal' "${unit_source}" || { echo "Test host condition missing" >&2; exit 2; }
grep -Fq 'User=streetsmart-hermes-test' "${unit_source}" || { echo "Test service user missing" >&2; exit 2; }
grep -Fq -- '--remote-debugging-address=127.0.0.1' "${unit_source}" || { echo "CDP is not loopback-only" >&2; exit 2; }
if grep -Eq '(^|[ =])/opt/streetsmart-hermes/' "${unit_source}"; then
  echo "Production path found in Test browser unit" >&2
  exit 2
fi

install -d -m 0700 -o "${TEST_USER}" -g "${TEST_USER}" \
  "${TEST_ROOT}/.hermes/browser-profiles/ezlynx" \
  "${TEST_ROOT}/.hermes/cache/chrome-ezlynx"
install -m 0644 "${unit_source}" "/etc/systemd/system/${UNIT_NAME}"
systemctl daemon-reload
systemctl enable --now "${UNIT_NAME}"

for _ in $(seq 1 20); do
  if curl -fsS -o /dev/null http://127.0.0.1:9222/json/version; then
    echo "TEST_CDP_READY"
    exit 0
  fi
  sleep 1
done

systemctl status "${UNIT_NAME}" --no-pager >&2 || true
echo "Test Chrome started but CDP did not become ready" >&2
exit 2
