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
| VM | `hermes-test-01` only (id `6971056864475829887`) |
| Zone | `us-east1-b` |
| Attached VM SA | `robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com` |
| WIF provider | `projects/1036123102831/locations/global/workloadIdentityPools/github-actions/providers/github` |
| WIF trust | repository ID `1343750842`, protected `refs/heads/main` |

## Owner one-time grants

Test and Production share `streetsmart-hermes-poc`. IAP must stay locked to
`hermes-test-01`.

**Do not** grant unbound project-level `roles/iap.tunnelResourceAccessor`
(that would also open `hermes-poc-01`).

**Do not** use `gcloud compute instances add-iam-policy-binding ... roles/iap.tunnelResourceAccessor`
— that role is not supported on Compute instance IAM and returns HTTP 400.

Correct IAP scope (Google "Grant access to a specific VM"):

- IAP tunnel instance IAM via
  `https://iap.googleapis.com/v1/projects/PROJECT_NUMBER/iap_tunnel/zones/ZONE/instances/INSTANCE`
- Or an Owner-managed project conditional binding titled
  `github-test-deployer-ssh` that resolves only to instance id
  `6971056864475829887` / `hermes-test-01`

```bash
bash scripts/grant-test-deployer-iap-ssh.sh grant
bash scripts/grant-test-deployer-iap-ssh.sh verify
```

Exact roles:

1. Instance `hermes-test-01`: `roles/compute.osAdminLogin` for the deployer SA
2. IAP tunnel instance `hermes-test-01`: `roles/iap.tunnelResourceAccessor`
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

Removes instance `osAdminLogin`, IAP tunnel-instance binding, and attached-SA
`actAs`. Leaves project `compute.viewer` unless removed manually. If an Owner
also created project conditional binding `github-test-deployer-ssh`, remove that
manually.

## Fresh-runner proof

Dispatch `.github/workflows/diagnose-test-iap-ssh.yml` with confirmation
`PROVE_TEST_IAP_SSH` from protected `main`.

Required green outputs:

- `hostname -s` → `hermes-test-01`
- SCP of a harmless temp file + exact SHA-256 read-back on the VM

Verified green run: `34745695554`
(https://github.com/streetsmart-insurance/robie-hermes/actions/runs/34745695554)
— SHA-256 `e262566c57a3f65ac08c09286239500c95fcf659480bb4f50ace605ed37bb923`.

## Related

- `docs/SSH_TEAM_ACCESS_RUNBOOK.md` — human team grants
- `docs/GCP_ACCESS_RUNBOOK.md` — WIF auditor vs deployer boundary
- `docs/TASK1_DURABLE_SSH_EVIDENCE.md` — redacted evidence dumps
