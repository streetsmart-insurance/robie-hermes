# Task 5 — Infrastructure monitoring evidence

**Requirement:** GCP alerting for disk, crashes, downtime — separate from app health.

**Project:** `streetsmart-hermes-poc`  
**Evidence date:** 2026-09-01 UTC

## Current GCP state (API read)

### Notification channel — **exists**

```json
{
  "displayName": "Carlo Ferrara",
  "type": "email",
  "labels": { "email_address": "carlo@streetsmart.insurance" },
  "name": "projects/streetsmart-hermes-poc/notificationChannels/7011715453478665377",
  "enabled": true,
  "creationRecord": { "mutateTime": "2026-09-01T11:42:31.196845952Z" }
}
```

### Alert policies — **none configured yet**

```http
GET /v3/projects/streetsmart-hermes-poc/alertPolicies
→ {}   (empty; HTTP 200)
```

`pawelstasinskiuk@gmail.com` can **read** channels but cannot **create** policies
(`403 PERMISSION_DENIED` on create/verify).

## Alert policies to apply (repo)

| File | Purpose |
| --- | --- |
| `deploy/monitoring/alert-hermes-poc-downtime.json` | `hermes-poc-01` no CPU metric 5m |
| `deploy/monitoring/alert-hermes-poc-disk.json` | Root disk > 85% (Ops Agent) |
| `deploy/monitoring/alert-hermes-poc-gateway-crash.json` | `hermes-gateway` systemd failure in syslog |
| `deploy/monitoring/alert-hermes-test-downtime.json` | `hermes-test-01` no CPU metric 5m |

Instance IDs used in filters:

| VM | `instance_id` | IP |
| --- | --- | --- |
| `hermes-poc-01` | `5649534881067121807` | `10.50.0.2` |
| `hermes-test-01` | `6971056864475829887` | `10.50.0.4` |

Bootstrap: `bash scripts/configure-hermes-infra-alerts.sh` (requires monitoring.admin)

## Separation from application health

| Layer | Examples | Not infra alerts |
| --- | --- | --- |
| **Infra (this task)** | VM stopped, disk full, gateway systemd crash | — |
| **Application** | `production_preflight`, `login_secret_health`, job metrics | Separate systemd timers |

## Proof one alert fired and reached us

### Status: **PENDING admin run**

Reviewer cannot create policies or fire drill (`403`). Carlo (or admin with
`monitoring.admin`) must run:

```sh
bash scripts/configure-hermes-infra-alerts.sh
bash scripts/fire-hermes-infra-alert-drill.sh
```

Expected drill transcript:

```text
drill_policy_created=projects/streetsmart-hermes-poc/alertPolicies/<id>
Wait 2-5 minutes for email...
drill_policy_deleted=projects/.../alertPolicies/<id>
INFRA_ALERT_DRILL_COMPLETE
```

Expected operator receipt: email to `carlo@streetsmart.insurance` with subject
containing **INFRA ALERT DRILL** (Cloud Monitoring alert notification).

**After admin run:** paste email headers/screenshot reference here and list
policies:

```sh
curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://monitoring.googleapis.com/v3/projects/streetsmart-hermes-poc/alertPolicies" \
  | python3 -m json.tool
```

### Ops Agent note (disk alert)

2026-09-01: `google-cloud-ops-agent` was **inactive** on `hermes-poc-01`.
`configure-hermes-infra-alerts.sh` installs Ops Agent before applying disk policy.

Live disk at drill time: `/dev/root` **59%** used (below 85% threshold).

## Checklist

| Item | Status |
| --- | --- |
| Alert policy definitions in repo | **DONE** |
| Notification channel exists | **DONE** (Carlo email) |
| Policies live in GCP | **PARTIAL** — 3/4 live (see below); gateway apply ready |
| Proof alert fired & received | **PENDING** (`fire-hermes-infra-alert-drill.sh`) |

### Live policies (2026-09-02 API read)

```text
INFRA hermes-test-01 instance down
INFRA hermes-poc-01 root disk > 85%
INFRA hermes-poc-01 instance down
(missing: INFRA hermes-poc-01 hermes-gateway service failed)
```

Filter validation (reviewer):

```text
$ bash scripts/apply-hermes-gateway-crash-alert.sh
log_filter_valid=ok
create_failed Permission denied   # reviewer lacks monitoring.admin; Carlo can create
```

### Gateway crash policy fix (2026-09-02)

Original policy failed API create. Root causes:

1. Wrong unit name: `robie-gateway` on `hermes-poc-01` (Production uses `hermes-gateway`).
2. Invalid log filter: missing `AND` between clauses; used `logName=projects/.../logs/syslog`
   instead of `log_id("syslog")`.

Carlo apply (only the missing policy):

```sh
bash scripts/apply-hermes-gateway-crash-alert.sh
```

## Related commits

| Commit | Description |
| --- | --- |
| *(this branch)* | Infra alert JSON, configure/drill scripts, runbook, evidence |
