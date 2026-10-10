#!/usr/bin/env bash
# Apply known least-privilege remediations. Admin only.
set -euo pipefail

PROJECT="${ROBIE_IAM_PROJECT:-streetsmart-hermes-poc}"
ZONE="${ROBIE_IAM_ZONE:-us-east1-b}"

echo "project=${PROJECT}"

# 1. Disable public SSH on unused default network (Hermes uses hermes-poc-net + IAP).
gcloud compute firewall-rules update default-allow-ssh \
  --project="${PROJECT}" \
  --disabled
echo "ok default-allow-ssh disabled"

# 2. Remove legacy VM metadata SSH keys if present.
for vm in hermes-test-01 hermes-poc-01; do
  if gcloud compute instances describe "${vm}" \
    --project="${PROJECT}" --zone="${ZONE}" \
    --format='value(metadata.items.ssh-keys)' 2>/dev/null | grep -q .; then
    gcloud compute instances remove-metadata "${vm}" \
      --project="${PROJECT}" --zone="${ZONE}" \
      --keys=ssh-keys
    echo "ok removed ssh-keys metadata from ${vm}"
  else
    echo "ok ${vm} has no ssh-keys metadata"
  fi
done

# 3. Isolate Test VM SA from Production secrets (idempotent).
if [[ -f "$(dirname "$0")/isolate-test-from-production-secrets.sh" ]]; then
  bash "$(dirname "$0")/isolate-test-from-production-secrets.sh"
else
  echo "skip isolate script (not present); run manually if needed"
fi

echo "HERMES_IAM_HARDEN_COMPLETE"
echo "Manual follow-ups: narrow VM OAuth scopes (requires stop), audit chat/diag SAs, run audit-hermes-iam.sh"
