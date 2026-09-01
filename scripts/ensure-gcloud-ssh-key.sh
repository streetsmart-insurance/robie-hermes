#!/usr/bin/env bash
# Non-interactive durable SSH material for gcloud compute ssh/scp via IAP + OS Login.
# Keys live under $HOME/.ssh (never /tmp). Registration uses OS Login, not VM metadata.
set -euo pipefail

SSH_DIR="${HOME}/.ssh"
KEY="${SSH_DIR}/google_compute_engine"
PUB="${KEY}.pub"

mkdir -p "${SSH_DIR}"
chmod 700 "${SSH_DIR}"

rotate_passphrase_key() {
  local backup="${KEY}.passphrase-backup.$(date -u +%Y%m%dT%H%M%SZ)"
  echo "rotating passphrase-protected deploy key to ${backup}" >&2
  mv "${KEY}" "${backup}"
  if [[ -f "${PUB}" ]]; then
    mv "${PUB}" "${backup}.pub"
  fi
}

if [[ -f "${KEY}" ]]; then
  if ! ssh-keygen -y -f "${KEY}" -P "" >/dev/null 2>&1; then
    rotate_passphrase_key
  fi
fi

if [[ ! -f "${KEY}" ]]; then
  ssh-keygen -t rsa -b 4096 -f "${KEY}" -N "" -C "$(whoami)@$(hostname -s)" -q
fi

# Refuse to proceed if a passphrase is still required — deploy must stay non-interactive.
if ! ssh-keygen -y -f "${KEY}" -P "" >/dev/null 2>&1; then
  echo "refusing passphrase-protected deploy key: ${KEY}" >&2
  exit 2
fi

chmod 600 "${KEY}"
chmod 644 "${PUB}"

gcloud compute os-login ssh-keys add --key-file="${PUB}" --ttl=0 >/dev/null

echo "os_login_key_ready=${PUB}"
echo "gcloud_ssh_key_file=${KEY}"
