# Task 8 — Infrastructure runbook evidence

**Requirement:** Written infra runbook — recovery, secret rotation, deploy end to end.

**Deliverable:** `docs/HERMES_INFRA_RUNBOOK.md`  
**Evidence date:** 2026-09-01 UTC

## Document map

| Section | Coverage |
| --- | --- |
| §1 Environments | Test vs Production VMs, paths, separation |
| §2 Access | IAP + OS Login, WIF identities |
| §3 Deploy E2E | Build → verify → Test workflow → Production workflow → rollback |
| §4 Secret rotation | EZLynx procedure, other secrets, version discipline |
| §5 Recovery | Snapshots, restore drill, full VM recovery, access/service recovery |
| §6 Monitoring | Infra alerts, billing, preflight |
| §7 IAM | Audit scripts cross-reference |
| §8 Evidence checklist | Required fields per change |
| §9 Script index | All operator scripts |

## Checklist

| Item | Status |
| --- | --- |
| Single written runbook document | **DONE** |
| Recovery procedures | **DONE** (§5) |
| Secret rotation | **DONE** (§4) |
| Deploy process end to end | **DONE** (§3) |
| Cross-links to specialized runbooks | **DONE** (§10) |

## Related commits

| Commit | Description |
| --- | --- |
| *(this branch)* | `docs/HERMES_INFRA_RUNBOOK.md`, evidence, contract test |
