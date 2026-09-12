# Cloud Shell SSH bootstrap (Task 1)

Use after a **fresh Cloud Shell session** (new tab or restart). Produces a literal
transcript for Task 1 evidence.

## Steps

```sh
gcloud config set project streetsmart-hermes-poc
git clone https://github.com/streetsmart-insurance/robie-hermes.git
cd robie-hermes
git checkout pawel-task1-ssh   # or main after merge

bash scripts/cloud-shell-restart-ssh-proof.sh | tee /tmp/robie-cloud-shell-ssh-proof.log
```

Expected tail (no passphrase prompts):

```text
ssh_key_passphrase_check=empty_ok
SSH_OK host=hermes-test-01 user=ext_...
ROBIE_CLOUD_SHELL_SSH_PROOF_END
```

If `~/.ssh/google_compute_engine` has a passphrase, `ensure-gcloud-ssh-key.sh`
renames it to `google_compute_engine.passphrase-backup.<timestamp>` and creates a
fresh no-passphrase key at the standard path.

## What the proof script does

1. Session metadata (host, user, gcloud account).
2. `scripts/ensure-gcloud-ssh-key.sh` (OS Login registration).
3. `ssh-keygen -y -P ""` passphrase check.
4. IAP SSH with `BatchMode=yes` (fails instead of prompting).

Save `/tmp/robie-cloud-shell-ssh-proof.log` in `docs/TASK1_DURABLE_SSH_EVIDENCE.md`.
