#!/usr/bin/env bash
# Remove Test VM service account access to Production-only secrets.
# Run once as a GCP admin (secretmanager.secrets.setIamPolicy).
set -euo pipefail

PROJECT="${ROBIE_SECRET_PROJECT:-streetsmart-hermes-poc}"
TEST_SA="${ROBIE_TEST_SERVICE_ACCOUNT:-robie-test-drive-reader@${PROJECT}.iam.gserviceaccount.com}"
MEMBER="serviceAccount:${TEST_SA}"

PRODUCTION_SECRETS=(
  ezlynx-username
  ezlynx-password
  robie-google-oauth-token
)

echo "project=${PROJECT}"
echo "test_service_account=${TEST_SA}"
for secret_id in "${PRODUCTION_SECRETS[@]}"; do
  echo "revoking secretAccessor on ${secret_id} for ${MEMBER}"
  gcloud secrets remove-iam-policy-binding "${secret_id}" \
    --project="${PROJECT}" \
    --member="${MEMBER}" \
    --role="roles/secretmanager.secretAccessor" \
    --quiet
done

echo "done: Test SA must not retain secretAccessor on Production secrets"
