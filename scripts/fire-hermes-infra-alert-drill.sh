#!/usr/bin/env bash
# Fire a short-lived infra alert drill policy that emails the notification channel.
set -euo pipefail

PROJECT="${ROBIE_MONITOR_PROJECT:-streetsmart-hermes-poc}"
TOKEN=$(gcloud auth print-access-token)
CHANNEL="${ROBIE_MONITOR_NOTIFICATION_CHANNEL:-}"

if [[ -z "${CHANNEL}" ]]; then
  CHANNEL=$(curl -s -H "Authorization: Bearer ${TOKEN}" \
    "https://monitoring.googleapis.com/v3/projects/${PROJECT}/notificationChannels" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(next(c['name'] for c in d['notificationChannels'] if c.get('enabled')))")
fi

BODY=$(cat <<EOF
{
  "displayName": "INFRA ALERT DRILL (delete after verify)",
  "combiner": "OR",
  "enabled": true,
  "notificationChannels": ["${CHANNEL}"],
  "conditions": [{
    "displayName": "CPU utilization always above -1 (drill)",
    "conditionThreshold": {
      "filter": "resource.type = \"gce_instance\" AND resource.labels.instance_id = \"5649534881067121807\" AND metric.type = \"compute.googleapis.com/instance/cpu/utilization\"",
      "comparison": "COMPARISON_GT",
      "thresholdValue": -1,
      "duration": "0s",
      "aggregations": [{
        "alignmentPeriod": "60s",
        "perSeriesAligner": "ALIGN_MEAN"
      }]
    }
  }]
}
EOF
)

created=$(curl -s -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  "https://monitoring.googleapis.com/v3/projects/${PROJECT}/alertPolicies" \
  -d "${BODY}")
POLICY_NAME=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['name'])" "${created}")
echo "drill_policy_created=${POLICY_NAME}"
echo "Wait 2-5 minutes for email at the configured notification channel, then deleting drill policy..."
sleep 180
curl -s -X DELETE -H "Authorization: Bearer ${TOKEN}" \
  "https://monitoring.googleapis.com/v3/${POLICY_NAME}" >/dev/null
echo "drill_policy_deleted=${POLICY_NAME}"
echo "INFRA_ALERT_DRILL_COMPLETE"
