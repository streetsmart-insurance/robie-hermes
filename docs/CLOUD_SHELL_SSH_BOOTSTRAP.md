# Cloud Shell SSH bootstrap (Task 1)

Use this after a **fresh Cloud Shell session** (new tab or restart). Captures a
literal transcript for Task 1 evidence.

## Steps

```sh
gcloud config set project streetsmart-hermes-poc
git clone https://github.com/streetsmart-insurance/robie-hermes.git
cd robie-hermes
git checkout main   # or the branch with merged Task 1 scripts

bash scripts/cloud-shell-restart-ssh-proof.sh | tee /tmp/robie-cloud-shell-ssh-proof.log
```

Expected tail:

```text
ssh_key_passphrase_check=empty_ok
SSH_OK host=hermes-test-01 user=ext_...
ROBIE_CLOUD_SHELL_SSH_PROOF_END
```

**No passphrase prompts.** If an old passphrase-protected
`~/.ssh/google_compute_engine` exists, `ensure-gcloud-ssh-key.sh` renames it to
`google_compute_engine.passphrase-backup.<timestamp>` and creates a fresh empty
passphrase key at the standard path.

## What the proof script does

1. Prints session metadata (host, user, gcloud account).
2. Runs `scripts/ensure-gcloud-ssh-key.sh` (OS Login registration).
3. Verifies empty passphrase with `ssh-keygen -y -P ""`.
4. SSH via IAP with `BatchMode=yes` (fails instead of prompting).

Save `/tmp/robie-cloud-shell-ssh-proof.log` into `docs/TASK1_DURABLE_SSH_EVIDENCE.md`
or attach to the PR.
