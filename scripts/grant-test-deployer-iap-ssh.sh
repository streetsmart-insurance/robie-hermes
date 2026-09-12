#!/usr/bin/env bash
# One-time owner bootstrap: least-privilege keyless IAP/SSH for the Test deployer.
# Does NOT create SA JSON keys, open public SSH, or widen WIF trust.
#
# IAP is instance-scoped to hermes-test-01 only. Test and Production share
# streetsmart-hermes-poc; project-level iap.tunnelResourceAccessor would also
# open hermes-poc-01.
#
# Usage:
#   bash scripts/grant-test-deployer-iap-ssh.sh grant
#   bash scripts/grant-test-deployer-iap-ssh.sh rollback
#   bash scripts/grant-test-deployer-iap-ssh.sh verify
set -euo pipefail

ACTION="${1:?usage: $0 grant|rollback|verify}"
PROJECT="${ROBIE_SSH_PROJECT:-streetsmart-hermes-poc}"
ZONE="${ROBIE_SSH_ZONE:-us-east1-b}"
VM="${ROBIE_SSH_VM:-hermes-test-01}"
DEPLOYER="${ROBIE_TEST_DEPLOYER:-robie-test-deployer@streetsmart-robie-test.iam.gserviceaccount.com}"
MEMBER="serviceAccount:${DEPLOYER}"
ATTACHED_SA="${ROBIE_TEST_VM_SA:-robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com}"

# Refuse accidental Production targeting.
if [[ "${VM}" != "hermes-test-01" ]]; then
  echo "refusing grant: VM must be hermes-test-01 (got ${VM})" >&2
  exit 2
fi

grant() {
  # OS Login on the test instance only.
  gcloud compute instances add-iam-policy-binding "${VM}" \
    --project="${PROJECT}" --zone="${ZONE}" \
    --member="${MEMBER}" \
    --role="roles/compute.osAdminLogin"

  # IAP tunnel SCOPED to hermes-test-01 only (not project-level).
  gcloud compute instances add-iam-policy-binding "${VM}" \
    --project="${PROJECT}" --zone="${ZONE}" \
    --member="${MEMBER}" \
    --role="roles/iap.tunnelResourceAccessor"

  # viewer for readiness describe (project-level; read-only).
  gcloud projects add-iam-policy-binding "${PROJECT}" \
    --member="${MEMBER}" \
    --role="roles/compute.viewer" \
    --condition=None

  # actAs only on the SA attached to hermes-test-01.
  gcloud iam service-accounts add-iam-policy-binding "${ATTACHED_SA}" \
    --project="${PROJECT}" \
    --member="${MEMBER}" \
    --role="roles/iam.serviceAccountUser"

  echo "granted deployer OS Login + instance-scoped IAP + viewer + actAs for ${VM}"
}

rollback() {
  gcloud compute instances remove-iam-policy-binding "${VM}" \
    --project="${PROJECT}" --zone="${ZONE}" \
    --member="${MEMBER}" \
    --role="roles/compute.osAdminLogin" || true

  gcloud compute instances remove-iam-policy-binding "${VM}" \
    --project="${PROJECT}" --zone="${ZONE}" \
    --member="${MEMBER}" \
    --role="roles/iap.tunnelResourceAccessor" || true

  gcloud iam service-accounts remove-iam-policy-binding "${ATTACHED_SA}" \
    --project="${PROJECT}" \
    --member="${MEMBER}" \
    --role="roles/iam.serviceAccountUser" || true

  echo "rolled back deployer OS Login/IAP/actAs for ${VM}"
  echo "note: left roles/compute.viewer in place (needed by readiness describe); remove manually if desired"
}

verify() {
  echo "## instance IAM (${VM}) — expect osAdminLogin + iap.tunnelResourceAccessor for deployer"
  gcloud compute instances get-iam-policy "${VM}" \
    --project="${PROJECT}" --zone="${ZONE}" \
    --format=json

  echo "## firewall hermes-allow-iap-ssh"
  gcloud compute firewall-rules describe hermes-allow-iap-ssh \
    --project="${PROJECT}" \
    --format='yaml(name,disabled,sourceRanges,allowed,targetTags,network)'

  echo "## VM OS Login + attached SA"
  gcloud compute instances describe "${VM}" \
    --project="${PROJECT}" --zone="${ZONE}" \
    --format='yaml(metadata.items,serviceAccounts,tags.items,status)'
}

case "${ACTION}" in
  grant) grant ;;
  rollback) rollback ;;
  verify) verify ;;
  *)
    echo "usage: $0 grant|rollback|verify" >&2
    exit 2
    ;;
esac
