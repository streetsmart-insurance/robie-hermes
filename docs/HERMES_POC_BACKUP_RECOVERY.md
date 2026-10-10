# hermes-poc-01 backup and disaster recovery

Production POC VM: `hermes-poc-01` (`streetsmart-hermes-poc`, `us-east1-b`).

## Automated snapshots

| Setting | Value |
| --- | --- |
| Resource policy | `daily-hermes-poc-snapshot` (`us-east1`) |
| Schedule | Daily at **04:00 UTC** |
| Retention | **14 days** |
| On source disk delete | `KEEP_AUTO_SNAPSHOTS` |
| Attached disk | `hermes-poc-01` (boot, 75 GB) |
| Storage location | `us` |

Bootstrap or verify:

```sh
bash scripts/configure-hermes-poc-snapshot-schedule.sh
```

Manual snapshot before risky change:

```sh
gcloud compute disks snapshot hermes-poc-01 \
  --project=streetsmart-hermes-poc \
  --zone=us-east1-b \
  --snapshot-names=hermes-poc-manual-$(date -u +%Y%m%d)
```

## Restore drill (non-destructive)

Does **not** modify `hermes-poc-01`. Creates a drill disk from a snapshot,
mounts it on `hermes-test-01`, verifies `/opt/streetsmart-hermes`, then deletes
the drill disk.

```sh
bash scripts/prove-hermes-poc-snapshot-restore.sh hermes-poc-dr-manual-20260901
```

## Full VM recovery (destructive — Carlo approval required)

1. Stop traffic: disable gateway scheduler / notify operators.
2. Pick snapshot: `gcloud compute snapshots list --filter='sourceDisk~hermes-poc-01'`
3. Stop instance: `gcloud compute instances stop hermes-poc-01 --zone=us-east1-b`
4. Replace boot disk from snapshot (example names — adjust):

```sh
gcloud compute disks delete hermes-poc-01 --zone=us-east1-b --quiet
gcloud compute disks create hermes-poc-01 \
  --zone=us-east1-b \
  --source-snapshot=<snapshot-name> \
  --type=pd-standard
gcloud compute instances attach-disk hermes-poc-01 \
  --disk=hermes-poc-01 --boot --zone=us-east1-b
gcloud compute instances start hermes-poc-01 --zone=us-east1-b
```

5. Verify: SSH via IAP, `systemctl is-active robie-gateway`, production preflight.
6. Record snapshot name, time, and verification in deployment evidence.

Application rollback (code only, not disk) remains in release rollback runbooks.

Evidence: `docs/TASK4_BACKUP_DR_EVIDENCE.md`.
