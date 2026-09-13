#!/usr/bin/env bash
# One-time owner bootstrap: least-privilege keyless IAP/SSH for the Test deployer.
# Does NOT create SA JSON keys, open public SSH, or widen WIF trust.
#
# IAP is scoped to hermes-test-01 only. Test and Production share
# streetsmart-hermes-poc; unbound project-level iap.tunnelResourceAccessor would
# also open hermes-poc-01.
#
# IMPORTANT: roles/iap.tunnelResourceAccessor is NOT a Compute Engine instance
# IAM role (gcloud compute instances add-iam-policy-binding → HTTP 400). Grant it
# on the IAP tunnel instance resource instead (IAP API), matching Google's
# "Grant access to a specific VM" docs.
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
IAP_CONDITION_TITLE="${ROBIE_IAP_CONDITION_TITLE:-github-test-deployer-ssh}"

# Refuse accidental Production targeting.
if [[ "${VM}" != "hermes-test-01" ]]; then
  echo "refusing grant: VM must be hermes-test-01 (got ${VM})" >&2
  exit 2
fi

project_number() {
  gcloud projects describe "${PROJECT}" --format='value(projectNumber)'
}

vm_id() {
  gcloud compute instances describe "${VM}" \
    --project="${PROJECT}" --zone="${ZONE}" --format='value(id)'
}

iap_tunnel_url() {
  local pn="$1"
  local instance_ref="$2"
  echo "https://iap.googleapis.com/v1/projects/${pn}/iap_tunnel/zones/${ZONE}/instances/${instance_ref}"
}

# Merge MEMBER into roles/iap.tunnelResourceAccessor on the IAP tunnel instance.
iap_tunnel_set_member() {
  local mode="$1" # add|remove
  local pn token url etag bindings tmp policy
  pn="$(project_number)"
  token="$(gcloud auth print-access-token)"
  # Prefer immutable instance id; name also works for get/set.
  url="$(iap_tunnel_url "${pn}" "$(vm_id)")"

  tmp="$(mktemp)"
  trap 'rm -f "${tmp}"' RETURN

  curl -fsS -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    -X POST "${url}:getIamPolicy" \
    -d '{}' >"${tmp}"

  python3 - "${tmp}" "${MEMBER}" "${mode}" <<'PY'
import json, sys
path, member, mode = sys.argv[1], sys.argv[2], sys.argv[3]
policy = json.load(open(path))
bindings = policy.setdefault("bindings", [])
role = "roles/iap.tunnelResourceAccessor"
binding = next((b for b in bindings if b.get("role") == role and not b.get("condition")), None)
if binding is None and mode == "add":
    binding = {"role": role, "members": []}
    bindings.append(binding)
if binding is None:
    json.dump(policy, open(path, "w"))
    raise SystemExit(0)
members = list(binding.get("members") or [])
if mode == "add" and member not in members:
    members.append(member)
if mode == "remove":
    members = [m for m in members if m != member]
binding["members"] = members
if not binding["members"]:
    bindings[:] = [b for b in bindings if b is not binding]
json.dump({"policy": policy}, open(path, "w"))
PY

  curl -fsS -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    -X POST "${url}:setIamPolicy" \
    -d @"${tmp}" >/dev/null

  echo "iap_tunnel_iam_${mode}=${url}"
}

grant() {
  # OS Login on the test instance only.
  gcloud compute instances add-iam-policy-binding "${VM}" \
    --project="${PROJECT}" --zone="${ZONE}" \
    --member="${MEMBER}" \
    --role="roles/compute.osAdminLogin"

  # IAP tunnel SCOPED to hermes-test-01 via IAP tunnel-instance IAM (not Compute).
  iap_tunnel_set_member add

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

  echo "granted deployer OS Login + IAP-tunnel-instance IAM + viewer + actAs for ${VM}"
  echo "note: do not use 'gcloud compute instances ... roles/iap.tunnelResourceAccessor' (HTTP 400)"
  echo "note: live Owner may also keep project conditional binding title=${IAP_CONDITION_TITLE}"
}

rollback() {
  gcloud compute instances remove-iam-policy-binding "${VM}" \
    --project="${PROJECT}" --zone="${ZONE}" \
    --member="${MEMBER}" \
    --role="roles/compute.osAdminLogin" || true

  iap_tunnel_set_member remove || true

  gcloud iam service-accounts remove-iam-policy-binding "${ATTACHED_SA}" \
    --project="${PROJECT}" \
    --member="${MEMBER}" \
    --role="roles/iam.serviceAccountUser" || true

  echo "rolled back deployer OS Login/IAP-tunnel/actAs for ${VM}"
  echo "note: left roles/compute.viewer in place (needed by readiness describe); remove manually if desired"
  echo "note: if a project conditional binding title=${IAP_CONDITION_TITLE} exists, remove it manually as Owner"
}

verify() {
  local pn token
  echo "## instance Compute IAM (${VM}) — expect osAdminLogin for deployer (IAP is NOT here)"
  gcloud compute instances get-iam-policy "${VM}" \
    --project="${PROJECT}" --zone="${ZONE}" \
    --format=json

  echo "## IAP tunnel instance IAM (${VM} / $(vm_id))"
  pn="$(project_number)"
  token="$(gcloud auth print-access-token)"
  curl -fsS -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    -X POST "$(iap_tunnel_url "${pn}" "$(vm_id)"):getIamPolicy" \
    -d '{}' || echo "(IAP getIamPolicy denied or unavailable for this identity)"

  echo "## firewall hermes-allow-iap-ssh"
  gcloud compute firewall-rules describe hermes-allow-iap-ssh \
    --project="${PROJECT}" \
    --format='yaml(name,disabled,sourceRanges,allowed,targetTags,network)'

  echo "## VM OS Login + attached SA"
  gcloud compute instances describe "${VM}" \
    --project="${PROJECT}" --zone="${ZONE}" \
    --format='yaml(metadata.items,serviceAccounts,tags.items,status,id)'
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
