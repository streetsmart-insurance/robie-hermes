#!/usr/bin/env bash
# Rewrite Test EZLynx login secret pins to newest ENABLED (states only).
# Never prints payloads. Run on hermes-test-01 as root / via approved SSH.
set -euo pipefail

test "$(hostname -s)" = hermes-test-01
test "${ROBIE_ENV:-TEST}" = TEST

ROOT=/opt/streetsmart-hermes-test
ETC=/etc/streetsmart-hermes-test
PYTHON="${ROOT}/venv/bin/python"
RESOLVER="${ROOT}/releases/current/scripts/read-login-secret-versions.py"
test -x "${PYTHON}"
test -f "${RESOLVER}"

mapfile -t LINES < <(sudo -u streetsmart-hermes-test env \
  PYTHONPATH="${ROOT}/releases/current" \
  "${PYTHON}" "${RESOLVER}" --project streetsmart-hermes-poc)
test "${#LINES[@]}" -eq 2

USER_LINE="${LINES[0]}"
PASS_LINE="${LINES[1]}"
[[ "${USER_LINE}" =~ ^ROBIE_EZLYNX_USERNAME_SECRET=projects/.+/secrets/ezlynx-username/versions/[1-9][0-9]*$ ]] \
  || { echo "REFUSED: unexpected username pin line" >&2; exit 2; }
[[ "${PASS_LINE}" =~ ^ROBIE_EZLYNX_PASSWORD_SECRET=projects/.+/secrets/ezlynx-password/versions/[1-9][0-9]*$ ]] \
  || { echo "REFUSED: unexpected password pin line" >&2; exit 2; }

USER_REF="${USER_LINE#ROBIE_EZLYNX_USERNAME_SECRET=}"
PASS_REF="${PASS_LINE#ROBIE_EZLYNX_PASSWORD_SECRET=}"
PASS_VER="${PASS_REF##*/versions/}"
echo "newest ENABLED pins resolved (password versions/${PASS_VER}; username versions/${USER_REF##*/versions/})"

rewrite_file() {
  local path="$1"
  test -f "${path}" || return 0
  local tmp
  tmp="$(mktemp)"
  # shellcheck disable=SC2016
  awk -v u="${USER_REF}" -v p="${PASS_REF}" '
    BEGIN { u_set=0; p_set=0 }
    /^ROBIE_EZLYNX_USERNAME_SECRET=/ { print "ROBIE_EZLYNX_USERNAME_SECRET=" u; u_set=1; next }
    /^ROBIE_EZLYNX_PASSWORD_SECRET=/ { print "ROBIE_EZLYNX_PASSWORD_SECRET=" p; p_set=1; next }
    { print }
    END {
      if (!u_set) print "ROBIE_EZLYNX_USERNAME_SECRET=" u
      if (!p_set) print "ROBIE_EZLYNX_PASSWORD_SECRET=" p
    }
  ' "${path}" >"${tmp}"
  chmod --reference="${path}" "${tmp}" 2>/dev/null || chmod 600 "${tmp}"
  chown --reference="${path}" "${tmp}" 2>/dev/null || true
  mv "${tmp}" "${path}"
  echo "rewrote ${path}"
}

rewrite_file "${ETC}/robie-message-runtime.env"
rewrite_file "${ETC}/accountability.env"
rewrite_file "${ETC}/robie-accountability.env"
rewrite_file "${ROOT}/accountability/robie-accountability.env"

# Gateway + scheduler pick up EnvironmentFile on next restart. Do not restart
# Chrome. Restart only units that load these files when idle.
systemctl daemon-reload
for unit in robie-gateway robie-scheduler hermes-email-watcher; do
  if systemctl show "${unit}" -p LoadState --value 2>/dev/null | grep -qx loaded; then
    if systemctl is-active --quiet "${unit}" 2>/dev/null; then
      systemctl restart "${unit}"
      systemctl is-active --quiet "${unit}"
      echo "restarted ${unit}"
    fi
  fi
done

echo "TEST login secret pins now follow newest ENABLED (payloads never printed)"
