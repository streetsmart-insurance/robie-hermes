# Test Hermes operator runbook — Razza quote replay

Use this only while you are sitting on the **isolated Test Hermes host**.
Do not run these steps on Production. Do not install the Test drop-in on
Production. Do not change IAM or secrets. Do not touch the live Hermes host
from this Cloud Agent.

This runbook does **not** claim live Test `COMPLETE`. A Job may finish locally
on Test only after independent verifier evidence is stored and a second person
confirms that evidence.

## What this is for

Replay Jake’s Hartford / Razza workers-compensation quote through the bounded
carrier-proposal worker on Test, then stop until a verifier can see stored
evidence.

## What you must already have

- You are on the isolated Test host, not Production.
- The real quote file is already on this host. Drive and Gmail do **not** have
  it. Do not invent a substitute PDF.
- Filename:

  `Razza Renewal - The Hartford Workers Compensation Quote.pdf`

- Known location on the live Hermes host (read-only reference; copy only if
  that file is already visible to you on Test):

  `/opt/streetsmart-hermes/robie-job-engine/data/artifacts/5d88bd97-d820-4c26-b1a8-888299aaf3b1/Razza Renewal - The Hartford Workers Compensation Quote.pdf`

If that file is not on this Test host, **stop**. Do not download a stand-in.
Do not generate a fake quote.

## 1. Confirm you are on Test

```sh
hostname
echo "ROBIE_ENV=${ROBIE_ENV-<unset>}"
test -d /opt/streetsmart-hermes-test && echo "Test root present" || echo "STOP: Test root missing"
```

Stop if the host looks like Production, if `/opt/streetsmart-hermes-test` is
missing, or if anyone asks you to write under `/opt/streetsmart-hermes/`
(the live path, not the `-test` path).

## 2. Copy the Razza quote into the Test artifact store

```sh
QUOTE_NAME='Razza Renewal - The Hartford Workers Compensation Quote.pdf'
TEST_INBOUND=/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/inbound
SOURCE="${QUOTE_SOURCE:?set QUOTE_SOURCE to the real PDF already on this host}"

mkdir -p "$TEST_INBOUND"
test -f "$SOURCE" || { echo "STOP: real quote PDF not found at $SOURCE"; exit 1; }
cp -n "$SOURCE" "$TEST_INBOUND/$QUOTE_NAME"
ls -l "$TEST_INBOUND/$QUOTE_NAME"
sha256sum "$TEST_INBOUND/$QUOTE_NAME"
```

`cp -n` refuses to overwrite. If the copy already exists, keep it and compare
checksums instead of replacing it.

## 3. Set ROBIE_ENV=TEST

```sh
export ROBIE_ENV=TEST
export ROBIE_JOB_DB=/opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db
export ROBIE_ARTIFACT_ROOT=/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts
```

If `ROBIE_ENV` is unset, `PRODUCTION`, `PROD`, or `LIVE`, the Test wiring must
refuse to run. Do not override that guard.

## 4. Install the Test-only systemd drop-in (never on Production)

The file in this repo is `deploy/systemd/hermes-gateway-test-only.conf`.
It sets `ROBIE_ENV=TEST` and Test Job/artifact paths.

```sh
# STOP if this host is Production.
install -d /etc/systemd/system/hermes-gateway.service.d
install -m 0644 deploy/systemd/hermes-gateway-test-only.conf \
  /etc/systemd/system/hermes-gateway.service.d/robie-test-only.conf
grep ROBIE_ENV /etc/systemd/system/hermes-gateway.service.d/robie-test-only.conf
systemctl daemon-reload
systemctl restart hermes-gateway
systemctl show hermes-gateway -p Environment --no-pager
```

Confirm the running unit shows `ROBIE_ENV=TEST` and
`/opt/streetsmart-hermes-test/...`. If you see live
`/opt/streetsmart-hermes/` Job paths, revert the drop-in and stop.

## 5. Run the bounded proposal job

From the checkout of `fix/reliability-mvp-grok` on the Test host:

```sh
PYTHONPATH=. python3 scripts/replay-quote-proposal.py \
  --quote-pdf "$TEST_INBOUND/$QUOTE_NAME" \
  --db "$ROBIE_JOB_DB" \
  --artifact-root "$ROBIE_ARTIFACT_ROOT"
```

The harness:

- refuses to invent a PDF if `--quote-pdf` is missing or unreadable
- creates a durable Job first
- runs the bounded carrier-proposal worker and the independent verifier
- refuses `COMPLETE` unless authoritative evidence is already stored
- always prints `live_test_complete=false` (this script is not a promotion)

## 6. Require independent verifier evidence before COMPLETE

Do not treat the Job as done because the worker printed a receipt.

A second person (not the operator who started the job) must open the Test Job
database and confirm a `verification_evidence` row for that Job with:

- `verified = 1`
- `authoritative = 1`
- `method = FRESH_PROPOSAL_READBACK`
- observed agency fee `$350` appearing **exactly once**
- observed page count matching the proposal (default 10 if the PDF page count
  cannot be read)
- expected versus observed postconditions match exactly
- `captured_at` is from the current action attempt (stale / year-2000 timestamps
  must not authorize `COMPLETE`)

Until that row exists and is checked, the status is not a valid `COMPLETE`.
If evidence is missing, leave the Job `UNVERIFIED`, `WAITING`, or `FAILED`.

## Durable Test deploy and rollback

The supported remote path is the GitHub Actions workflow
`.github/workflows/deploy-test.yml`. It uses GitHub OIDC workload identity to
impersonate
`robie-test-deployer@streetsmart-robie-test.iam.gserviceaccount.com`; it does
not use a downloaded service-account key and does not require a recurring
browser login. The provider admits only repository ID `1343750842`, owner ID
`320189022`, and `refs/heads/main`. Repository names alone are not the trust
boundary.

The workflow is manual and accepts only the exact confirmation
`DEPLOY_TO_HERMES_TEST_01`. Run it from protected `main`. It builds the exact
`github.sha`, verifies the release archive and checksum, and targets only:

- project `streetsmart-hermes-poc`
- zone `us-east1-b`
- VM `hermes-test-01`
- root `/opt/streetsmart-hermes-test`
- service `robie-gateway`

The remote installer independently refuses a different hostname, a missing or
divergent rollback pointer, an archive/commit mismatch, a missing Test
`jobs.db`, or any `RUNNING`/`VERIFYING` job or live lease. It flips both Test
pointers atomically, restarts only `robie-gateway`, refreshes the official
proof, and requires `done=true`, `live=true`, and
`authorizes_complete=false`. On failed post-flip proof, it restores both old
pointers and restarts the prior Test release.

Durable deployment evidence is written to
`/opt/streetsmart-hermes-test/deployments/<12-char-sha>/test-deploy-evidence.json`.
It includes the full commit, archive SHA-256, previous release, gateway start
timestamp, proof path, inactive-job inventory, and `production_touched=false`.
The previous release path in that file is the exact rollback target.

### One-time GCP bootstrap

An owner must grant the deployer only the access needed to reach and administer
`hermes-test-01`: IAP tunnel access for that specific VM, OS Login admin on
that instance, Compute read access, and service-account `actAs` only if the VM
uses an attached service account and Google Cloud requires it for SSH. Do not
grant access on `hermes-poc-01`, do not create a service-account key, and do not
weaken the workload-identity provider condition. After this one-time bootstrap,
normal Test deployments do not require Carlo to authenticate in a browser.

Follow `.agents/workflows/deploy-to-test.md` for the release gate and
`.agents/workflows/rollback.md` for manual rollback. Preserve durable Job data;
never delete Job state during deploy or rollback.

`live_test_complete` stays `false` until an independent reviewer has stored live
Test evidence on the Test host.

## Discussion notes on Test

Buster Brown `26356199` exists in live EZLynx, not in UAT. The note tool
follows `ROBIE_ENV` unless you point it at live. On hermes-test-01, set
these on the gateway service environment (`robie-gateway` /
`hermes-gateway`). Do not raise `GOOGLE_CHAT_MAX_MESSAGES`.

```
ROBIE_EZLYNX_DISCUSSION_API=live
ROBIE_EZLYNX_WRITE_APPLICANT_IDS=26356199
ROBIE_EZLYNX_API_PROD_SECRET=projects/<project-id>/secrets/ezlynx-api-prod/versions/latest
ROBIE_EZLYNX_USERNAME_SECRET=projects/<project-id>/secrets/ezlynx-username/versions/latest
ROBIE_EZLYNX_PASSWORD_SECRET=projects/<project-id>/secrets/ezlynx-password/versions/latest
```

`ROBIE_EZLYNX_DISCUSSION_API=live` loads the live API host from
`ROBIE_EZLYNX_API_PROD_SECRET` and the SSRobie login from
`ezlynx-username` / `ezlynx-password`, the same secrets the Test browser
already uses. Store resource names only. Never put the password in this
file, in Chat, or in the job ledger.

If any of those secrets is missing, or the production secret points at
`app.uatezlynx.com`, the note tool stops. It does not fall back to UAT.
An applicant id that is not in `ROBIE_EZLYNX_WRITE_APPLICANT_IDS` is
refused before any EZLynx request. Leave this unset on Production. Prod
already uses the live API through `ROBIE_ENV=PRODUCTION`.

## What this runbook does not do

- It does not modify Production.
- It does not claim live Test `COMPLETE` from a Cloud Agent VM that cannot see
  the Razza PDF.
- It does not create or download a fake quote.
- It does not deploy or roll back Test outside the protected GitHub workflow.
