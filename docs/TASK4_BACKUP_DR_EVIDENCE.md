# Task 4 — Backup/DR evidence (`hermes-poc-01`)

**Requirement:** automated snapshots on schedule + documented recovery + one real
restore drill.

**Project:** `streetsmart-hermes-poc`  
**Instance:** `hermes-poc-01` (`us-east1-b`)  
**Evidence date:** 2026-09-01 UTC

## Snapshot schedule configuration

Resource policy `daily-hermes-poc-snapshot` (region `us-east1`):

```yaml
name: daily-hermes-poc-snapshot
region: us-east1
snapshotSchedulePolicy:
  retentionPolicy:
    maxRetentionDays: 14
    onSourceDiskDelete: KEEP_AUTO_SNAPSHOTS
  schedule:
    dailySchedule:
      daysInCycle: 1
      duration: PT14400S
      startTime: 04:00
  snapshotProperties:
    guestFlush: false
    storageLocations:
    - us
status: READY
```

Boot disk attachment:

```yaml
disk: hermes-poc-01
zone: us-east1-b
resourcePolicies:
- daily-hermes-poc-snapshot
```

Bootstrap script: `scripts/configure-hermes-poc-snapshot-schedule.sh`

## Snapshots (2026-09-01)

| Name | Created | Size | Auto |
| --- | --- | --- | --- |
| `hermes-poc-dr-manual-20260901` | 2026-09-01T08:36:00-07:00 | 75 GB | manual (Task 4 evidence) |
| `hermes-pre-cutover-20260818-033224` | 2026-08-17T20:32:29-07:00 | 75 GB | manual (pre-cutover) |

Policy attached 2026-09-01; first scheduled auto-snapshot expected after next
04:00 UTC window.

## Real restore drill (2026-09-01)

**Method:** non-destructive — production VM untouched.

1. Created snapshot `hermes-poc-dr-manual-20260901` from live disk.
2. Created drill disk `hermes-poc-dr-drill-20260901` from that snapshot.
3. Attached drill disk to `hermes-test-01` (rw, isolated Test VM).
4. Mounted `/dev/sdb1` and verified Production tree:

```text
drwxr-xr-x ... /mnt/hermes-poc-dr-drill/opt/streetsmart-hermes
/mnt/hermes-poc-dr-drill/opt/streetsmart-hermes/releases/current
RESTORE_DRILL_OK
```

5. Unmounted, detached drill disk, deleted drill disk.

Drill script: `scripts/prove-hermes-poc-snapshot-restore.sh`

## Recovery documentation

| Doc | Purpose |
| --- | --- |
| `docs/HERMES_POC_BACKUP_RECOVERY.md` | Schedule, drill, full VM recovery steps |

## Checklist

| Item | Status |
| --- | --- |
| Automated schedule configured | **DONE** (`daily-hermes-poc-snapshot` on disk) |
| Schedule config exported | **DONE** (YAML above) |
| Documented recovery | **DONE** |
| Real restore drill executed | **DONE** (2026-09-01, transcript above) |
| Production VM unchanged | **DONE** |

## Related commits

| Commit | Description |
| --- | --- |
| *(this branch)* | DR runbook, schedule/restore scripts, evidence |
