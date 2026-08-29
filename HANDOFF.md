# Hermes handoff — Friday 28 Aug 2026 after 2:32pm ET

## MUST-CALL — diagnose any Hermes job

Do **not** diagnose from Google Chat text, the published recording, or by
clicking Production/Test Chrome from a laptop. Do not invent leftover
RETRY. Production is not the first test. Do not deploy to
`hermes-poc-01` / `hermes-test-01` from this lookup.

Copy-paste on the VM (or with `ROBIE_JOB_DB` pointed at an isolated copy):

```bash
PYTHONPATH=. python3 -m robie_job_engine.playwright_observability <job-id>
# or
PYTHONPATH=. python3 scripts/lookup-playwright-job.py <job-id>
```

That prints `playwright_exec` rows, CDP `/json/list` url+title snapshots,
and the `playwright-trace.zip` path if present. Zero tool rows on an
EZLynx/Playwright Chat job is FAILED (1df9740b silent-gap), not
UNVERIFIED. No cookies, secrets, or passwords.

Start here if you are ChatGPT, Claude, Cursor, Grok, Jake, or another
operator continuing StreetSmart Hermes. The Ascend API implementation is
merged and live on Test, but API execution is intentionally disabled until a
sandbox credential is stored in an isolated Test secret.

Detailed Ascend handoff: [docs/ascend-api-handoff-2026-08-28.md](docs/ascend-api-handoff-2026-08-28.md)

Tap-in card: [CLOUD_DESK.md](CLOUD_DESK.md)

## Non-negotiable safety boundary

1. Never use PAWIVA or account `221398001` for a test.
2. Do not make a Production Ascend program the first API write test.
3. Stop before Save, email, checkout, payment, or bind in browser audits.
4. Never paste an Ascend API key, password, or email 2SV code into chat,
   commands, payloads, logs, screenshots, or GitHub.
5. Ascend login is Robie (`robie@streetsmart.insurance` + email 2SV). A human
   enters the 2SV code.
6. Test/Production Secret Manager isolation is not proven. Treat secrets as
   possibly shared until an operator proves otherwise.
7. Do not deploy to Production (`hermes-poc-01`) until the API path records a
   successful sandbox create plus independent read-back.

## Current GitHub state

Deployed application baseline:
`4df60a0955f3500bdf51de2e81526dbe15a5a802`. Test matches this application
commit. Documentation-only handoff commits may advance GitHub `main` without
requiring a VM redeploy; compare application files before reporting drift.

Merged work:

- PR 59 — guarded `ascend.create_program` API worker, verifier, planning CLI,
  and documentation.
- PR 60 — mark the synthetic missing-PDF release replay as Test.
- PR 61 — scope release logic subprocesses to Test.
- PR 62 — isolate release tests from the VM's live Drive-synced skill snapshot.

The API worker creates a program and one or more billables, then performs
fresh GET requests for the program and every returned billable ID. The Job
Engine cannot mark COMPLETE without authoritative matching read-back.

In-flight PRs:

- **PR 57 (`feat/playwright-hardening-punchlist`)**: Playwright hardening
  suite (tracing, locator registry, route-interception fixtures, split retry
  policy, visual snapshot diffing, idle-gated Chrome refresh). Not merged.
  Not deployed. No live job.

## Proven Test deployment

Host: `hermes-test-01.c.streetsmart-hermes-poc.internal`

- Release: `4df60a0955f3`
- Archive SHA-256:
  `9c9f27e754beff6c84d9c616f861d49406e8ae0a413b61295ec14e22081ce308`
- `/opt/streetsmart-hermes-test/current` points to
  `/opt/streetsmart-hermes-test/releases/4df60a0955f3/robie-hermes-4df60a0955f3`.
- `/opt/streetsmart-hermes-test/releases/current` points to the same release.
- Actual gateway service: `robie-gateway.service`.
- Pointer flip: `2026-08-28T18:32:05.061477+00:00`.
- Gateway `ActiveEnterTimestamp`: `2026-08-28T18:32:18+00:00`.
- Official install proof: `done=true`, `live=true`, `pointer_only=false`.
- Install-proof Job: `be57b415-58bd-4e3c-96c1-6f907a227f64`.
- All 10 deployed `test_ascend_api.py` tests passed on the Test VM.

The release verifier reported no new failures. Its four known parity gaps
(service account, secrets, browser, and job database) remain INCONCLUSIVE and
do not constitute a Production all-clear.

## Ascend API execution state

No Ascend API POST has been made. The Test gateway currently has only
`ROBIE_ENV=TEST`; no `ROBIE_ASCEND_API_*` execution settings are configured.
This is intentional.

The credential supplied during the session authenticated only against the
Production API and was pasted into chat. Do not reuse or copy it. Rotate it
before any future Production enablement.

Required before the first real API write:

1. Obtain an Ascend sandbox API key.
2. Confirm Test/Production Secret Manager isolation.
3. Store the sandbox key directly in a versioned Test Secret Manager secret.
4. Configure Test with the secret resource name, sandbox base URL, and API
   enable flag. Do not expose the secret value.
5. Use read-only sandbox endpoints to resolve the existing Test insured,
   Producer/Account Manager users, carrier identifier, and coverage identifier.
6. Run one non-PAWIVA sandbox `ascend.create_program` Job.
7. Confirm the Job is COMPLETE only after fresh program and billable GETs.
8. Record the clean Test pass before considering Production configuration or
   deployment.

## Browser audit status

`ascend.locator_artifact_audit` remains separate because it tests Ascend's
Import document UI and stops before Save. It is not the normal operational
creation path.

The last live UI audit used the ANC Express / Progressive / California Test
quote and reached carrier selection after passing Import document, roles,
customer type, customer name, address handling, and quote number. It then
failed because Ascend's carrier option accessibility name differed from the
visible carrier text. PR 58 attempted a searched-option Enter fix but was not
merged. Do not treat that UI audit as an API create/read-back pass.

## Playwright hardening status (PR 57, not live)

Fail-closed rules stay in force: no positional locator guesses; locator
ambiguity is HITL, not retry; no Production deploy; no live job.

- **PW-01 Tracing**: `PlaywrightTraceManager` wraps CDP execution with
  `ROBIE_JOB_ID` and writes `playwright-trace.zip` to the job artifact folder.
- **PW-02 Locator Registry**: declarative JSON (`locators/ascend.json`,
  `locators/ezlynx.json`); positional selectors are rejected.
- **PW-03 Retry Policy**: `LOCATOR_AMBIGUITY`, `EMPTY_OR_CORRUPT_ARTIFACT`,
  and `AUTH_CHALLENGE` are non-retryable; `TRANSIENT_NETWORK` is retryable;
  other errors keep the worker flag.
- **PW-04 Route fixtures**: offline Playwright tests in
  `tests/test_playwright_route_fixtures.py`.
- **PW-05 Snapshot diffing**: `SnapshotDiffManager` / `scripts/run-snapshot-diff.py`.
- **PW-06 Idle Chrome refresh**: `scripts/chrome_refresh_if_idle.py` refuses
  to restart while jobs are `RUNNING` or `VERIFYING`.

## What “live” means

1. GitHub `main` SHA matches the release archive.
2. Both Test release pointers match that 12-character release.
3. `robie-gateway.service` `ActiveEnterTimestamp` is after the pointer flip.
4. The install-proof checkpoint exists.
5. For a Chat job, a `checkpoints.kind=gateway_progress` row exists in
   `jobs.db`.

Pointer-only is not live. A green simulator is not a Production all-clear.

## Operator entry

SSH from any browser:
https://shell.cloud.google.com/?show=terminal&project=streetsmart-hermes-poc

```bash
gcloud compute ssh hermes-test-01 \
  --project=streetsmart-hermes-poc \
  --zone=us-east1-b \
  --tunnel-through-iap \
  --ssh-key-file=$HOME/.ssh/hermes-nopass
```

First command after login: `hostname`.

Do not RETRY: `09d69760` `40ccc0d6` `da53765b` `6cf6f6ae` `468d1575`
`de9c530a` `7f297e56` `66266d62` `c31f9c69` `8a11d28c` `6662f894`
`eb96f620` `ffbfa109` `640834a4` `807f8920` `38c0fa79` `1df9740b`.
