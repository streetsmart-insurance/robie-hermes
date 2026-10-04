#!/usr/bin/env bash
# Install the lease-gated EZLynx keepalive timer from the release on this host.
# Does not restart Chrome, does not log in, and does not post to Chat.
# Run on the VM after this commit is the current release. Refuses the other host.
set -euo pipefail

[[ "${EUID}" -eq 0 ]] || { echo "installer requires sudo" >&2; exit 2; }

short="$(hostname -s)"
case "${short}" in
  hermes-test-01)
    root="/opt/streetsmart-hermes-test"
    units=(robie-ezlynx-keepalive-test.service robie-ezlynx-keepalive-test.timer)
    timer="robie-ezlynx-keepalive-test.timer"
    ;;
  hermes-poc-01)
    root="/opt/streetsmart-hermes"
    units=(robie-ezlynx-keepalive.service robie-ezlynx-keepalive.timer)
    timer="robie-ezlynx-keepalive.timer"
    ;;
  *)
    echo "refusing host ${short}; keepalive installs only on hermes-test-01 or hermes-poc-01" >&2
    exit 2
    ;;
esac

release_root="$(readlink -f "${root}/releases/current")"
for unit in "${units[@]}"; do
  source="${release_root}/deploy/systemd/${unit}"
  [[ -f "${source}" ]] || { echo "unit missing from current release: ${unit}" >&2; exit 2; }
  if [[ "${short}" == "hermes-test-01" ]] && grep -Eq '(^|[ =])/opt/streetsmart-hermes/' "${source}"; then
    echo "Production path found in Test unit ${unit}" >&2
    exit 2
  fi
  install -m 0644 "${source}" "/etc/systemd/system/${unit}"
done

systemctl daemon-reload
systemctl enable --now "${timer}"
systemctl is-active "${timer}"
echo "KEEPALIVE_TIMER_INSTALLED ${timer}"
