#!/usr/bin/env bash
# Owner-only: grant/verify IAM signBlob for delegated Gmail HITL email.
#
# Root cause (PR #394 / hermes-poc-01): HITL email uses
# google.auth.iam.Signer → IAM Credentials signBlob on the DWD SA
# (hermes-poc@…). Caller is the VM default SA (same account on Prod).
# Missing roles/iam.serviceAccountTokenCreator on that SA → HTTP 403.
# This is not a git grant; Chat HITL is a separate channel.
#
# Usage:
#   bash scripts/grant-gmail-signblob-token-creator.sh verify
#   bash scripts/grant-gmail-signblob-token-creator.sh grant
#   bash scripts/grant-gmail-signblob-token-creator.sh probe-prod
#
# probe-prod SSHes hermes-poc-01 and runs signBlob + one Sent read-back
# probe email to carlo@ (real message). Refuse other VMs.
set -euo pipefail

ACTION="${1:?usage: $0 grant|verify|probe-prod}"
PROJECT="${ROBIE_SSH_PROJECT:-streetsmart-hermes-poc}"
ZONE="${ROBIE_SSH_ZONE:-us-east1-b}"
VM="${ROBIE_SSH_VM:-hermes-poc-01}"
DWD_SA="${ROBIE_GMAIL_DWD_SA:-hermes-poc@streetsmart-hermes-poc.iam.gserviceaccount.com}"
CALLER_SA="${ROBIE_GMAIL_SIGNBLOB_CALLER_SA:-hermes-poc@streetsmart-hermes-poc.iam.gserviceaccount.com}"
ROLE="roles/iam.serviceAccountTokenCreator"
MEMBER="serviceAccount:${CALLER_SA}"

grant() {
  gcloud iam service-accounts add-iam-policy-binding "${DWD_SA}" \
    --project="${PROJECT}" \
    --member="${MEMBER}" \
    --role="${ROLE}"
  echo "granted ${ROLE} on ${DWD_SA} to ${MEMBER}"
}

verify() {
  echo "DWD_SA=${DWD_SA}"
  echo "CALLER_SA=${CALLER_SA}"
  echo "ROLE=${ROLE}"
  policy="$(gcloud iam service-accounts get-iam-policy "${DWD_SA}" \
    --project="${PROJECT}" --format=json)"
  MEMBER="${MEMBER}" ROLE="${ROLE}" python3 -c '
import json, os, sys
member = os.environ["MEMBER"]
role = os.environ["ROLE"]
policy = json.loads(sys.stdin.read())
hits = [
    b for b in policy.get("bindings", [])
    if b.get("role") == role and member in (b.get("members") or [])
]
if not hits:
    print(f"MISSING: {member} lacks {role} on DWD SA")
    sys.exit(2)
print(f"OK: {member} has {role}")
' <<<"${policy}"
}

probe_prod() {
  if [[ "${VM}" != "hermes-poc-01" ]]; then
    echo "refusing probe: VM must be hermes-poc-01 (got ${VM})" >&2
    exit 2
  fi
  gcloud compute ssh "${VM}" --project="${PROJECT}" --zone="${ZONE}" --tunnel-through-iap \
    --command='
set -euo pipefail
sudo bash -lc "
set -a
source /etc/streetsmart-hermes/robie-message-runtime.env
set +a
export PYTHONPATH=/opt/streetsmart-hermes/releases/current HOME=/opt/streetsmart-hermes
sudo -u carlo_streetsmart_insurance -E env HOME=/opt/streetsmart-hermes \
  PYTHONPATH=/opt/streetsmart-hermes/releases/current \
  ROBIE_VERIFICATION_MAIL_SENDER=\"\$ROBIE_VERIFICATION_MAIL_SENDER\" \
  ROBIE_VERIFICATION_GMAIL_DELEGATED_SERVICE_ACCOUNT=\"\$ROBIE_VERIFICATION_GMAIL_DELEGATED_SERVICE_ACCOUNT\" \
  ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT=\"\$ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT\" \
  /opt/streetsmart-hermes/.hermes/hermes-agent/venv/bin/python - <<'"'"'PY'"'"'
import google.auth
from google.auth import iam
from google.auth.transport.requests import Request
from robie_job_engine.verification_mailer import send_verification_email
from robie_job_engine.accountability_delivery import _delegated_gmail_sender
import os

source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
sa = os.environ["ROBIE_VERIFICATION_GMAIL_DELEGATED_SERVICE_ACCOUNT"]
signer = iam.Signer(Request(), source, sa)
sig = signer.sign(b"hitl-signblob-probe")
print("signBlob_OK", len(sig))
receipt = send_verification_email(
    to=["carlo@streetsmart.insurance"],
    cc=[],
    subject="[ROBIE PROBE] HITL signBlob TokenCreator check",
    text_body="Owner probe: delegated HITL email path.\\n",
    plain_only=True,
)
print("EMAIL_SEND_OK", receipt["message_id"])
gmail = _delegated_gmail_sender(sa, os.environ["ROBIE_VERIFICATION_MAIL_SENDER"])
msg = gmail.users().messages().get(
    userId="me", id=receipt["message_id"], format="metadata",
    metadataHeaders=["Subject", "To", "From"],
).execute()
labels = msg.get("labelIds") or []
assert "SENT" in labels, labels
print("SENT_READBACK_OK", msg.get("id"), labels)
PY
"
'
}

case "${ACTION}" in
  grant) grant; verify ;;
  verify) verify ;;
  probe-prod) probe_prod ;;
  *) echo "usage: $0 grant|verify|probe-prod" >&2; exit 2 ;;
esac
