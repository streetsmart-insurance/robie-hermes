# HERMES billing budget alerts

GCP **billing budget** alerts for cost spikes on `streetsmart-hermes-poc`. Separate from
Cloud Monitoring infra alerts (`docs/HERMES_INFRA_MONITORING.md`) and application health.

## Budget config (repo)

| Field | Value |
| --- | --- |
| **Display name** | `HERMES POC monthly cost spike budget` |
| **Scope** | Project `streetsmart-hermes-poc` only |
| **Billing account** | `01CAD2-76802C-85A6BB` |
| **Monthly amount** | **$300 USD** (override: `ROBIE_BUDGET_MONTHLY_USD`) |
| **Calendar period** | Monthly (GCP default) |

### Threshold rules

| Threshold | Basis | Alert when |
| --- | --- | --- |
| **50%** | `CURRENT_SPEND` | Month-to-date spend exceeds $150 |
| **80%** | `CURRENT_SPEND` | Month-to-date spend exceeds $240 (early spike warning) |
| **100%** | `CURRENT_SPEND` | Month-to-date spend exceeds $300 |
| **100%** | `FORECASTED_SPEND` | Forecasted month-end spend exceeds $300 |

### Notification recipients

- **IAM billing recipients** (`enableIamRecipients`: billing account admins)
- **Monitoring email channel**: `carlo@streetsmart.insurance`
  (`projects/streetsmart-hermes-poc/notificationChannels/7011715453478665377`)

Source JSON: `deploy/monitoring/budget-hermes-poc.json`

## Apply (billing admin)

```sh
bash scripts/configure-hermes-billing-budget.sh
```

Requires `billing.budgets.create` on the billing account (typically Billing Account
Administrator).

## Verify

```sh
gcloud billing budgets list --billing-account=01CAD2-76802C-85A6BB --format=json
```

## Adjust monthly cap

```sh
ROBIE_BUDGET_MONTHLY_USD=500 bash scripts/configure-hermes-billing-budget.sh
```

Or edit `units` in `deploy/monitoring/budget-hermes-poc.json` before apply.
