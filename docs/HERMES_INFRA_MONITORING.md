# Hermes infrastructure monitoring (GCP)

Infrastructure alerts are **separate** from ROBIE application health
(`production_preflight`, `login_secret_health`, regression battery). They cover
VM downtime, disk pressure, and gateway service crashes at the GCP/host layer.

## Scope

| VM | Alerts |
| --- | --- |
| `hermes-poc-01` | Instance down (no CPU metric 5m), root disk > 85%, `robie-gateway` systemd failure |
| `hermes-test-01` | Instance down (no CPU metric 5m) |

## Prerequisites

- Notification channel (email) in `streetsmart-hermes-poc`
- Caller has `roles/monitoring.admin` (or equivalent create on alert policies)
- Google Cloud Ops Agent on VMs for disk utilization alert

## Bootstrap

```sh
export ROBIE_MONITOR_PROJECT=streetsmart-hermes-poc
bash scripts/configure-hermes-infra-alerts.sh
```

Policy JSON: `deploy/monitoring/alert-*.json`

## Prove an alert reached operators

```sh
bash scripts/fire-hermes-infra-alert-drill.sh
```

Creates a short-lived drill policy (always-true CPU threshold), waits 3 minutes
for email delivery, then deletes the drill policy. Save the email screenshot or
forward in evidence.

## What is intentionally excluded

- EZLynx login health, Chat intake, job engine metrics (application layer)
- Secret Manager version states
- GitHub Actions / deploy workflow status

Evidence: `docs/TASK5_INFRA_MONITORING_EVIDENCE.md`.
