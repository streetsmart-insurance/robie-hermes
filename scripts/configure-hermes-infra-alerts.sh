#!/usr/bin/env bash
# Configure GCP infra alert policies (VM downtime, disk, service crash). Admin only.
set -euo pipefail

PROJECT="${ROBIE_MONITOR_PROJECT:-streetsmart-hermes-poc}"
POLICY_DIR="$(cd "$(dirname "$0")/../deploy/monitoring" && pwd)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TOKEN=$(gcloud auth print-access-token)

prepare_ssh() {
  if [[ -f "${SCRIPT_DIR}/ensure-gcloud-ssh-key.sh" ]]; then
    bash "${SCRIPT_DIR}/ensure-gcloud-ssh-key.sh"
  fi
}

pick_channel() {
  local id="${ROBIE_MONITOR_NOTIFICATION_CHANNEL_ID:-7011715453478665377}"
  if [[ -n "${id}" ]]; then
    echo "projects/${PROJECT}/notificationChannels/${id}"
    return
  fi
  curl -s -H "Authorization: Bearer ${TOKEN}" \
    "https://monitoring.googleapis.com/v3/projects/${PROJECT}/notificationChannels" \
    | python3 -c "
import json,sys
data=json.load(sys.stdin)
for ch in data.get('notificationChannels', []):
    if ch.get('enabled'):
        print(ch['name'])
        break
else:
    raise SystemExit('no enabled notification channel found')
"
}

install_ops_agent() {
  local vm="$1"
  gcloud compute ssh "${vm}" \
    --project="${PROJECT}" \
    --zone="${ROBIE_MONITOR_ZONE:-us-east1-b}" \
    --tunnel-through-iap \
    --command='curl -sSO https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh && sudo bash add-google-cloud-ops-agent-repo.sh --also-install && sudo systemctl is-active google-cloud-ops-agent' 2>&1
}

create_policy() {
  local file="$1"
  local channel="$2"
  local body
  body=$(python3 -c "
import json, sys
with open(sys.argv[1], encoding='utf-8') as fh:
    data = json.load(fh)
data['notificationChannels'] = [sys.argv[2]]
print(json.dumps(data))
" "${file}" "${channel}")
  curl -s -X POST \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    "https://monitoring.googleapis.com/v3/projects/${PROJECT}/alertPolicies" \
    -d "${body}" \
    | python3 -c "
import json, sys
d = json.load(sys.stdin)
err = d.get('error')
print('created', d.get('name', 'ERROR'), d.get('displayName', ''))
if err:
    raise SystemExit(err.get('message', 'create failed'))
"
}

prepare_ssh
CHANNEL=$(pick_channel)
echo "notification_channel=${CHANNEL}"

if [[ "${ROBIE_MONITOR_INSTALL_OPS_AGENT:-1}" == "1" ]]; then
  echo "Installing Ops Agent on hermes-poc-01..."
  install_ops_agent hermes-poc-01 || echo "warn: ops agent install failed on poc"
  echo "Installing Ops Agent on hermes-test-01..."
  install_ops_agent hermes-test-01 || echo "warn: ops agent install failed on test"
fi

for policy in "${POLICY_DIR}"/alert-*.json; do
  echo "Applying ${policy}..."
  create_policy "${policy}" "${CHANNEL}"
done

curl -s -H "Authorization: Bearer ${TOKEN}" \
  "https://monitoring.googleapis.com/v3/projects/${PROJECT}/alertPolicies" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('policy_count', len(d.get('alertPolicies',[]))); [print(p['displayName']) for p in d.get('alertPolicies',[])]"
