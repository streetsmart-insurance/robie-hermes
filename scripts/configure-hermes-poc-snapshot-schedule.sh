#!/usr/bin/env bash
# Create or verify the daily snapshot schedule for hermes-poc-01 boot disk.
set -euo pipefail

PROJECT="${ROBIE_DR_PROJECT:-streetsmart-hermes-poc}"
REGION="${ROBIE_DR_REGION:-us-east1}"
ZONE="${ROBIE_DR_ZONE:-us-east1-b}"
INSTANCE="${ROBIE_DR_INSTANCE:-hermes-poc-01}"
POLICY="${ROBIE_DR_POLICY:-daily-hermes-poc-snapshot}"
DISK="${INSTANCE}"

if gcloud compute resource-policies describe "${POLICY}" \
  --project="${PROJECT}" --region="${REGION}" >/dev/null 2>&1; then
  echo "policy_exists=${POLICY}"
else
  gcloud compute resource-policies create snapshot-schedule "${POLICY}" \
    --project="${PROJECT}" \
    --region="${REGION}" \
    --max-retention-days=14 \
    --on-source-disk-delete=keep-auto-snapshots \
    --daily-schedule \
    --start-time=04:00 \
    --storage-location=us
  echo "policy_created=${POLICY}"
fi

attached=$(gcloud compute disks describe "${DISK}" \
  --project="${PROJECT}" --zone="${ZONE}" \
  --format='value(resourcePolicies)' 2>/dev/null || true)
if grep -q "${POLICY}" <<<"${attached}"; then
  echo "disk_attached=${DISK}"
else
  gcloud compute disks add-resource-policies "${DISK}" \
    --project="${PROJECT}" \
    --zone="${ZONE}" \
    --resource-policies="${POLICY}"
  echo "disk_policy_attached=${DISK}"
fi

gcloud compute resource-policies describe "${POLICY}" \
  --project="${PROJECT}" --region="${REGION}" --format=yaml
