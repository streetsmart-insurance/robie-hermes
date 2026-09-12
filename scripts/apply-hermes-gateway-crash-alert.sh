#!/usr/bin/env bash
# Apply only the hermes-poc-01 gateway crash log alert (admin). Validates filter first.
set -euo pipefail

PROJECT="${ROBIE_MONITOR_PROJECT:-streetsmart-hermes-poc}"
POLICY_FILE="$(cd "$(dirname "$0")/../deploy/monitoring" && pwd)/alert-hermes-poc-gateway-crash.json"
CHANNEL_ID="${ROBIE_MONITOR_NOTIFICATION_CHANNEL_ID:-7011715453478665377}"
CHANNEL="projects/${PROJECT}/notificationChannels/${CHANNEL_ID}"
TOKEN=$(gcloud auth print-access-token)

filter="$(python3 -c "
import json
with open('${POLICY_FILE}', encoding='utf-8') as fh:
    print(json.load(fh)['conditions'][0]['conditionMatchedLog']['filter'])
")"

echo "gateway_crash_log_filter=${filter}"
echo "Validating filter via Cloud Logging (read 1 entry, errors surface syntax issues)..."
gcloud logging read "${filter}" --project="${PROJECT}" --limit=1 --format=json >/dev/null
echo "log_filter_valid=ok"

body="$(python3 -c "
import json, sys
with open(sys.argv[1], encoding='utf-8') as fh:
    data = json.load(fh)
data['notificationChannels'] = [sys.argv[2]]
print(json.dumps(data))
" "${POLICY_FILE}" "${CHANNEL}")"

response="$(curl -s -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  "https://monitoring.googleapis.com/v3/projects/${PROJECT}/alertPolicies" \
  -d "${body}")"

python3 -c "
import json, sys
d = json.loads(sys.argv[1])
err = d.get('error')
if err:
    print('create_failed', err.get('message', d))
    raise SystemExit(2)
print('created', d.get('name'), d.get('displayName'))
" "${response}"
