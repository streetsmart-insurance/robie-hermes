#!/usr/bin/env bash
set -euo pipefail
[[ "${EUID}" -eq 0 ]] || { echo "requires sudo" >&2; exit 2; }
[[ "$(hostname -s)" == "hermes-poc-01" ]] || { echo "refusing non-Production host" >&2; exit 2; }
root=/opt/streetsmart-hermes
install -d -o streetsmart-hermes -g streetsmart-hermes -m 0700 "${root}/lost-customer-retention"
if [[ ! -f "${root}/lost-customer-retention/config.json" ]]; then
  install -o streetsmart-hermes -g streetsmart-hermes -m 0600 \
    "${root}/releases/current/deploy/accountability/lost-customer-retention-config.example.json" \
    "${root}/lost-customer-retention/config.json"
fi
install -o root -g root -m 0644 "${root}/releases/current/deploy/systemd/streetsmart-lost-customer-retention.service" /etc/systemd/system/
install -o root -g root -m 0644 "${root}/releases/current/deploy/systemd/streetsmart-lost-customer-retention.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now streetsmart-lost-customer-retention.timer
systemctl is-enabled --quiet streetsmart-lost-customer-retention.timer
systemctl is-active --quiet streetsmart-lost-customer-retention.timer
systemctl show streetsmart-lost-customer-retention.timer -p NextElapseUSecRealtime -p LastTriggerUSec -p Unit --no-pager
systemctl show streetsmart-lost-customer-retention.service -p ExecStart -p User -p WorkingDirectory --no-pager

