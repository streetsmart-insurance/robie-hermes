#!/usr/bin/env bash
# Create or update the HERMES POC billing budget alert. Billing admin only.
set -euo pipefail

PROJECT="${ROBIE_BILLING_PROJECT:-streetsmart-hermes-poc}"
BILLING_ACCOUNT="${ROBIE_BILLING_ACCOUNT:-01CAD2-76802C-85A6BB}"
MONTHLY_USD="${ROBIE_BUDGET_MONTHLY_USD:-300}"
BUDGET_FILE="$(cd "$(dirname "$0")/../deploy/monitoring" && pwd)/budget-hermes-poc.json"
TOKEN=$(gcloud auth print-access-token)

pick_channel() {
  local id="${ROBIE_MONITOR_NOTIFICATION_CHANNEL_ID:-7011715453478665377}"
  echo "projects/${PROJECT}/notificationChannels/${id}"
}

list_budgets() {
  curl -s -H "Authorization: Bearer ${TOKEN}" \
    "https://billingbudgets.googleapis.com/v1/billingAccounts/${BILLING_ACCOUNT}/budgets" \
    | python3 -m json.tool
}

create_budget() {
  local channel="$1"
  python3 - "${BUDGET_FILE}" "${channel}" "${MONTHLY_USD}" <<'PY'
import json, sys
path, channel, monthly = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, encoding="utf-8") as fh:
    body = json.load(fh)
body["amount"]["specifiedAmount"]["units"] = str(monthly)
body["notificationsRule"]["monitoringNotificationChannels"] = [channel]
print(json.dumps(body))
PY
}

post_budget() {
  local body="$1"
  curl -s -X POST \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    "https://billingbudgets.googleapis.com/v1/billingAccounts/${BILLING_ACCOUNT}/budgets" \
    -d "${body}" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print('created', d.get('name','ERROR'), d.get('displayName',''))"
}

echo "billing_account=${BILLING_ACCOUNT} project=${PROJECT} monthly_usd=${MONTHLY_USD}"
echo "existing_budgets:"
list_budgets

body=$(create_budget "$(pick_channel)")
post_budget "${body}"

echo "budgets_after_create:"
list_budgets
echo "HERMES_BILLING_BUDGET_CONFIGURED"
