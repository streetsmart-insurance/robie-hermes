# Durable SSH team access (Task 1)

Human operators reach `hermes-test-01` and `hermes-poc-01` with **IAP tunneling +
OS Login**. No public SSH port on the Hermes network. No Cloud Shell round-trip
required after one-time bootstrap.

## One-time bootstrap (operator laptop)

```sh
gcloud auth login your@email.com
gcloud config set project streetsmart-hermes-poc

bash scripts/ensure-gcloud-ssh-key.sh

gcloud compute ssh hermes-test-01 \
  --zone=us-east1-b \
  --project=streetsmart-hermes-poc \
  --tunnel-through-iap \
  --command='hostname && whoami'
```

Keys are stored at `$HOME/.ssh/google_compute_engine` (no passphrase). They are
registered with `gcloud compute os-login ssh-keys add`, not VM `ssh-keys` metadata.

## One-time bootstrap (admin grants access)

```sh
bash scripts/grant-team-ssh-access.sh user:person@example.com hermes-test-01
bash scripts/grant-team-ssh-access.sh user:person@example.com hermes-poc-01
```

Instance IAM: `roles/compute.osLogin` only on the named VM. Project IAM:
`roles/iap.tunnelResourceAccessor` (IAP TCP forwarding). Do **not** grant
`roles/compute.osAdminLogin` or Owner/Editor to builders unless explicitly required.

Revoke one person without affecting others:

```sh
gcloud compute instances remove-iam-policy-binding hermes-test-01 \
  --zone=us-east1-b --project=streetsmart-hermes-poc \
  --member='user:person@example.com' --role='roles/compute.osLogin'

gcloud compute os-login ssh-keys remove --key-file="$HOME/.ssh/google_compute_engine.pub"
```

## GitHub Actions deploy (non-interactive)

`.github/workflows/deploy-test.yml` calls `scripts/ensure-gcloud-ssh-key.sh` after
WIF auth so every deploy uses OS Login material under `/home/runner/.ssh/` with
zero passphrase prompts.

## Firewall posture

Hermes VMs attach to `hermes-poc-net`. Port 22 ingress is allowed only from IAP
range `35.235.240.0/20` (`hermes-allow-iap-ssh`). Legacy `default-allow-ssh` on
the unused `default` network is disabled.

Evidence and dumps: `docs/TASK1_DURABLE_SSH_EVIDENCE.md`.
