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
| VM | `hermes-test-01` only |
| Zone | `us-east1-b` |
| Attached VM SA | `robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com` |
| WIF provider | `projects/1036123102831/locations/global/workloadIdentityPools/github-actions/providers/github` |
| WIF trust | repository ID `1343750842`, protected `refs/heads/main` |

## Owner one-time grants (Carlo-scoped)

Test and Production share `streetsmart-hermes-poc`. IAP must be **instance-
scoped** to `hermes-test-01`. Do **not** grant project-level
`roles/iap.tunnelResourceAccessor` (that would also open `hermes-poc-01`).

```bash
bash scripts/grant-test-deployer-iap-ssh.sh grant
bash scripts/grant-test-deployer-iap-ssh.sh verify
```

Exact roles (owner paste-equivalent):

```bash
PROJECT=streetsmart-hermes-poc
ZONE=us-east1-b
VM=hermes-test-01
MEMBER=serviceAccount:robie-test-deployer@streetsmart-robie-test.iam.gserviceaccount.com
ATTACHED_SA=robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com

# OS Login on the test instance
gcloud compute instances add-iam-policy-binding "$VM" \
  --project="$PROJECT" --zone="$ZONE" \
  --member="$MEMBER" --role="roles/compute.osAdminLogin"

# IAP tunnel — SCOPED to hermes-test-01 only
gcloud compute instances add-iam-policy-binding "$VM" \
  --project="$PROJECT" --zone="$ZONE" \
  --member="$MEMBER" --role="roles/iap.tunnelResourceAccessor"

# viewer for readiness describe (project-level, read-only)
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="$MEMBER" --role="roles/compute.viewer" --condition=None

# actAs only on the SA attached to hermes-test-01
gcloud iam service-accounts add-iam-policy-binding "$ATTACHED_SA" \
  --project="$PROJECT" --member="$MEMBER" --role="roles/iam.serviceAccountUser"
```

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

Removes instance `osAdminLogin`, instance-scoped IAP, and attached-SA `actAs`.
Leaves project `compute.viewer` unless removed manually.

## Fresh-runner proof

After merge to protected `main` and Owner grant, dispatch
`.github/workflows/diagnose-test-iap-ssh.yml` with confirmation
`PROVE_TEST_IAP_SSH`.

Required green outputs:

- `hostname -s` → `hermes-test-01`
- SCP of a harmless temp file + exact SHA-256 read-back on the VM

Tell Carlo when that run ID is green.

## Related

- `docs/SSH_TEAM_ACCESS_RUNBOOK.md` — human team grants
- `docs/GCP_ACCESS_RUNBOOK.md` — WIF auditor vs deployer boundary
- `docs/TASK1_DURABLE_SSH_EVIDENCE.md` — redacted evidence dumps
