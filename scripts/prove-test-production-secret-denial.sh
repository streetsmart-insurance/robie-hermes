#!/usr/bin/env bash
# Prove hermes-test-01 cannot read Production EZLynx secrets (exit non-zero).
# Run from an operator workstation with IAP SSH to Test.
set -euo pipefail

PROJECT="${ROBIE_SECRET_PROJECT:-streetsmart-hermes-poc}"
ZONE="${ROBIE_SSH_ZONE:-us-east1-b}"
VM="${ROBIE_TEST_VM:-hermes-test-01}"

gcloud compute ssh "${VM}" \
  --project="${PROJECT}" \
  --zone="${ZONE}" \
  --tunnel-through-iap \
  --command='
set -euo pipefail
SA=$(curl -sf -H Metadata-Flavor:Google \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email)
echo "host=$(hostname -s) service_account=${SA}"
for secret_id in ezlynx-username ezlynx-password; do
  if gcloud secrets versions access latest \
    --secret="${secret_id}" \
    --project=streetsmart-hermes-poc >/dev/null 2>/tmp/robie-deny-test.err; then
    echo "FAIL ${secret_id}: read succeeded (isolation broken)"
    exit 2
  fi
  code=$?
  echo "DENIED ${secret_id}: exit=${code} stderr=$(tr "\n" " " </tmp/robie-deny-test.err | head -c 240)"
  rm -f /tmp/robie-deny-test.err
done
echo "PASS: Production secrets denied on Test VM"
'
