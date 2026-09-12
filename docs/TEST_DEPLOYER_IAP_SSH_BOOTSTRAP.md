# Test deployer IAP / OS Login bootstrap (keyless)

## Goal

GitHub Actions impersonates
`robie-test-deployer@streetsmart-robie-test.iam.gserviceaccount.com` through
Workload Identity Federation. IAM stays durable until revoked. Each runner
private key stays short-lived (`OSLOGIN_SSH_KEY_TTL=1h` by default). No
service-account JSON key. No public SSH port.

## Targets

| Item | Value |
| --- | --- |
| Project | `streetsmart-hermes-poc` |
| VM | `hermes-test-01` |
| Zone | `us-east1-b` |
| Attached VM SA | `robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com` |
| WIF provider | `projects/1036123102831/locations/global/workloadIdentityPools/github-actions/providers/github` |
| WIF trust | repository ID `1343750842`, protected `refs/heads/main` |

## Owner one-time grants

Requires project Owner / IAM admin. Pawel can verify instance IAM and firewall
but cannot mutate project IAM.

```bash
bash scripts/grant-test-deployer-iap-ssh.sh grant
bash scripts/grant-test-deployer-iap-ssh.sh verify | tee /tmp/robie-deployer-iap-verify.json
```

Exact roles:

1. Instance `hermes-test-01`: `roles/compute.osAdminLogin` for the deployer SA
2. Project: `roles/iap.tunnelResourceAccessor` for the deployer SA
3. Project: `roles/compute.viewer` for describe / gcloud SSH metadata reads
4. Attached SA only: `roles/iam.serviceAccountUser` for
   `robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com`

Firewall (already present; do not open `0.0.0.0/0:22`):

- Rule `hermes-allow-iap-ssh`
- Source `35.235.240.0/20`
- TCP 22
- Target tag `hermes-poc`

VM metadata: `enable-oslogin=TRUE`. Do not store `ssh-keys` metadata.

## Rollback

```bash
bash scripts/grant-test-deployer-iap-ssh.sh rollback
```

## Fresh-runner proof

After merge to protected `main`, dispatch
`.github/workflows/diagnose-test-iap-ssh.yml` with confirmation
`PROVE_TEST_IAP_SSH`.

Required green outputs:

- `hostname -s` → `hermes-test-01`
- SCP of a harmless temp file + exact SHA-256 read-back on the VM

Tell Carlo when that run ID is green.

## Human laptop path (optional)

```bash
OSLOGIN_SSH_KEY_TTL=0 bash scripts/ensure-gcloud-ssh-key.sh
gcloud compute ssh hermes-test-01 \
  --project=streetsmart-hermes-poc --zone=us-east1-b --tunnel-through-iap \
  --command='hostname -s'
```

## Related

- `docs/SSH_TEAM_ACCESS_RUNBOOK.md` — human team grants
- `docs/GCP_ACCESS_RUNBOOK.md` — WIF auditor vs deployer boundary
- `docs/TASK1_DURABLE_SSH_EVIDENCE.md` — redacted evidence dumps
