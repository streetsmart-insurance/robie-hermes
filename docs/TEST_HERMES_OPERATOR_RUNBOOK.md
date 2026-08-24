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

Until that row exists and is checked, the status is not a valid `COMPLETE`.
If evidence is missing, leave the Job `UNVERIFIED`, `WAITING`, or `FAILED`.

## What this runbook does not do

- It does not modify Production.
- It does not claim live Test `COMPLETE` from a Cloud Agent VM that cannot see
  the Razza PDF.
- It does not create or download a fake quote.
