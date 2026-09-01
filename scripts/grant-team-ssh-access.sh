#!/usr/bin/env bash
# Grant least-privilege human SSH to one Hermes VM. Run as project/org admin.
# Scoped: instance IAM (osLogin) + project IAP tunnel (required for --tunnel-through-iap).
set -euo pipefail

PROJECT="${ROBIE_SSH_PROJECT:-streetsmart-hermes-poc}"
ZONE="${ROBIE_SSH_ZONE:-us-east1-b}"
MEMBER="${1:?usage: $0 user:email@domain instance-name}"
INSTANCE="${2:?usage: $0 user:email@domain instance-name}"

gcloud compute instances add-iam-policy-binding "${INSTANCE}" \
  --project="${PROJECT}" \
  --zone="${ZONE}" \
  --member="${MEMBER}" \
  --role="roles/compute.osLogin"

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="${MEMBER}" \
  --role="roles/iap.tunnelResourceAccessor" \
  --condition=None

echo "granted osLogin on ${INSTANCE} and iap.tunnelResourceAccessor on ${PROJECT} for ${MEMBER}"
