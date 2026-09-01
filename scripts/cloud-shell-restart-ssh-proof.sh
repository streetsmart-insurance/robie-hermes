#!/usr/bin/env bash
# Cloud Shell restart + non-interactive SSH proof for Task 1 evidence.
# Run after a fresh Cloud Shell session (or simulate restart: open new Cloud Shell tab).
# Capture full terminal output for the transcript record.
set -euo pipefail

PROJECT="${ROBIE_SSH_PROJECT:-streetsmart-hermes-poc}"
ZONE="${ROBIE_SSH_ZONE:-us-east1-b}"
VM="${ROBIE_SSH_VM:-hermes-test-01}"

echo "ROBIE_CLOUD_SHELL_SSH_PROOF_BEGIN"
echo "timestamp_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "host=$(hostname -s)"
echo "user=$(whoami)"
echo "gcloud_account=$(gcloud auth list --filter=status:ACTIVE --format='value(account)' | head -n1)"
echo "gcloud_project=$(gcloud config get-value project 2>/dev/null)"

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
ensure="${repo_root}/scripts/ensure-gcloud-ssh-key.sh"
[[ -f "${ensure}" ]] || {
  echo "missing ensure-gcloud-ssh-key.sh (clone robie-hermes first)" >&2
  exit 2
}

bash "${ensure}"

KEY="${HOME}/.ssh/google_compute_engine"
echo "ssh_key_path=${KEY}"
ssh-keygen -y -f "${KEY}" -P "" >/dev/null && echo "ssh_key_passphrase_check=empty_ok"

gcloud compute ssh "${VM}" \
  --project="${PROJECT}" \
  --zone="${ZONE}" \
  --tunnel-through-iap \
  --quiet \
  --ssh-flag=-o --ssh-flag=BatchMode=yes \
  --command='echo SSH_OK host=$(hostname -s) user=$(whoami)'

echo "ROBIE_CLOUD_SHELL_SSH_PROOF_END"
