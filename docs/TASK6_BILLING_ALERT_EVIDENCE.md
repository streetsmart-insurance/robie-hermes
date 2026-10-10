# Task 6 — Billing alert evidence

**Requirement:** Budget alert for cost spikes.

**Project:** `streetsmart-hermes-poc`  
**Billing account:** `01CAD2-76802C-85A6BB`  
**Evidence date:** 2026-09-01 UTC

## Budget / threshold config (to apply)

| Setting | Value |
| --- | --- |
| **Budget name** | `HERMES POC monthly cost spike budget` |
| **Monthly cap** | **$300 USD** |
| **Project filter** | `projects/streetsmart-hermes-poc` |
| **50% current** | Alert at **$150** month-to-date |
| **80% current** | Alert at **$240** month-to-date (spike early warning) |
| **100% current** | Alert at **$300** month-to-date |
| **100% forecasted** | Alert when forecasted month-end exceeds **$300** |
| **Email channel** | `carlo@streetsmart.insurance` (Monitoring channel `7011715453478665377`) |
| **IAM recipients** | Enabled (billing account role holders) |

Full JSON: `deploy/monitoring/budget-hermes-poc.json`

```json
{
  "displayName": "HERMES POC monthly cost spike budget",
  "budgetFilter": {
    "projects": ["projects/streetsmart-hermes-poc"]
  },
  "amount": {
    "specifiedAmount": {
      "currencyCode": "USD",
      "units": "300"
    }
  },
  "thresholdRules": [
    { "thresholdPercent": 0.5, "spendBasis": "CURRENT_SPEND" },
    { "thresholdPercent": 0.8, "spendBasis": "CURRENT_SPEND" },
    { "thresholdPercent": 1.0, "spendBasis": "CURRENT_SPEND" },
    { "thresholdPercent": 1.0, "spendBasis": "FORECASTED_SPEND" }
  ],
  "notificationsRule": {
    "enableIamRecipients": true
  }
}
```

Apply script injects `monitoringNotificationChannels` at runtime.

## Current GCP state (API read)

### Billing linked — **yes**

```json
{
  "billingAccountName": "billingAccounts/01CAD2-76802C-85A6BB",
  "billingEnabled": true,
  "projectId": "streetsmart-hermes-poc"
}
```

### Budgets — **cannot list** (permission denied)

```text
gcloud billing budgets list --billing-account=01CAD2-76802C-85A6BB
→ 403 USER_PROJECT_DENIED (billingbudgets.googleapis.com)
   pawelstasinskiuk@gmail.com lacks billing.budgets.list / serviceusage on project
```

**Live budget state:** **UNVERIFIED** — repo config ready; admin must apply and paste
`gcloud billing budgets list` output below.

## Admin action (Carlo)

```sh
bash scripts/configure-hermes-billing-budget.sh
```

After apply, paste live budget list here:

```sh
gcloud billing budgets list --billing-account=01CAD2-76802C-85A6BB --format=json
```

## Checklist

| Item | Status |
| --- | --- |
| Budget JSON + thresholds in repo | **DONE** |
| Configure script | **DONE** |
| Runbook | **DONE** (`docs/HERMES_BILLING_ALERTS.md`) |
| Budget live in GCP | **PENDING** (billing admin) |
| Live list proof | **PENDING** |

## Related commits

| Commit | Description |
| --- | --- |
| *(this branch)* | Budget JSON, configure script, runbook, evidence |
