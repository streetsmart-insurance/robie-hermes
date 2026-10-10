#!/usr/bin/env bash
# Export full IAM inventory for streetsmart-hermes-poc. Admin only.
set -euo pipefail

PROJECT="${ROBIE_IAM_PROJECT:-streetsmart-hermes-poc}"
ZONE="${ROBIE_IAM_ZONE:-us-east1-b}"
OUT="${ROBIE_IAM_AUDIT_DIR:-/tmp/hermes-iam-audit-$(date -u +%Y%m%dT%H%M%SZ)}"
INSTANCES=(hermes-poc-01 hermes-test-01)

mkdir -p "${OUT}"

dump() {
  local name="$1"
  shift
  echo "== ${name} ==" | tee -a "${OUT}/audit.log"
  if "$@" >"${OUT}/${name}.json" 2>"${OUT}/${name}.err"; then
    echo "ok ${name}" | tee -a "${OUT}/audit.log"
  else
    echo "FAIL ${name} (see ${OUT}/${name}.err)" | tee -a "${OUT}/audit.log"
  fi
}

dump project-iam gcloud projects get-iam-policy "${PROJECT}" --format=json
dump service-accounts gcloud iam service-accounts list --project="${PROJECT}" --format=json

for sa in \
  hermes-poc \
  robie-test-drive-reader \
  robie-production-deployer \
  robie-chat-build \
  robie-chat-bridge \
  claude-cloud-diag; do
  dump "sa-iam-${sa}" \
    gcloud iam service-accounts get-iam-policy \
    "${sa}@${PROJECT}.iam.gserviceaccount.com" \
    --project="${PROJECT}" --format=json
done

for vm in "${INSTANCES[@]}"; do
  dump "instance-iam-${vm}" \
    gcloud compute instances get-iam-policy "${vm}" \
    --project="${PROJECT}" --zone="${ZONE}" --format=json
  dump "instance-describe-${vm}" \
    gcloud compute instances describe "${vm}" \
    --project="${PROJECT}" --zone="${ZONE}" --format=json
done

dump firewall-rules gcloud compute firewall-rules list --project="${PROJECT}" --format=json

gcloud secrets list --project="${PROJECT}" --format='value(name)' | while read -r secret_id; do
  [[ -z "${secret_id}" ]] && continue
  safe_name="secret-iam-${secret_id//[^a-zA-Z0-9._-]/_}"
  dump "${safe_name}" \
    gcloud secrets get-iam-policy "${secret_id}" \
    --project="${PROJECT}" --format=json
done

python3 - "${OUT}" <<'PY'
import json
from pathlib import Path

out = Path(__import__("sys").argv[1])
rows = []
project = json.loads((out / "project-iam.json").read_text(encoding="utf-8"))
for binding in project.get("bindings", []):
    role = binding["role"]
    for member in binding.get("members", []):
        rows.append((member, role, "project"))

for path in sorted(out.glob("instance-iam-*.json")):
    vm = path.stem.replace("instance-iam-", "")
    data = json.loads(path.read_text(encoding="utf-8"))
    for binding in data.get("bindings", []):
        for member in binding.get("members", []):
            rows.append((member, binding["role"], f"instance:{vm}"))

for path in sorted(out.glob("secret-iam-*.json")):
    secret = path.stem.replace("secret-iam-", "")
    data = json.loads(path.read_text(encoding="utf-8"))
    for binding in data.get("bindings", []):
        for member in binding.get("members", []):
            rows.append((member, binding["role"], f"secret:{secret}"))

summary = out / "account-role-summary.md"
with summary.open("w", encoding="utf-8") as fh:
    fh.write("# IAM account / role summary\n\n")
    fh.write("| Member | Role | Scope |\n")
    fh.write("| --- | --- | --- |\n")
    for member, role, scope in sorted(rows):
        fh.write(f"| `{member}` | `{role}` | {scope} |\n")
print(f"written {summary}")
PY

echo "HERMES_IAM_AUDIT_COMPLETE dir=${OUT}"
