# Ascend API implementation and Test deployment handoff

Date: 2026-08-28

## Outcome

Hermes now contains a bounded API-based path for creating Ascend programs and
billables. The code is merged to GitHub and officially installed on
`hermes-test-01`. The integration remains disabled because there is no sandbox
credential configured, so no external Ascend record was created during this
work.

The Playwright locator audit is retired from the operational and release gate.
New Ascend program work uses only the bounded API path.

## Implementation

Primary module: `robie_job_engine/ascend_api.py`

Operator entry: `scripts/run-ascend-api-program.py`

Job type: `ascend.create_program`

Worker: `ascend-api`

The worker:

1. Validates a strictly allowlisted program and billable payload without
   loading a credential.
2. Rejects PAWIVA and `221398001` recursively before network access.
3. Requires explicit execution enablement and an API origin matching
   `ROBIE_ENV`.
4. Loads the bearer credential only from a full Secret Manager version
   resource.
5. Creates the program with `POST /v1/programs`.
6. Creates each quote billable with `POST /v1/billables`.
7. Persists the program identity if a later billable fails and refuses an
   automatic create retry, preventing an untracked duplicate program.
8. Uses a separate verifier to perform fresh GET requests for the program and
   every created billable.

The Job Engine permits COMPLETE only when the fresh API state matches the
requested insured, Producer, Account Manager, and billable count.

## Configuration gates

Common enable:

```text
ROBIE_ASCEND_API_ENABLED=1
ROBIE_ASCEND_API_KEY_SECRET=projects/PROJECT/secrets/SECRET/versions/VERSION
```

Test accepts only:

```text
ROBIE_ENV=TEST
ROBIE_ASCEND_API_BASE_URL=https://sandbox.api.useascend.com
```

Production accepts only:

```text
ROBIE_ENV=PRODUCTION
ROBIE_ASCEND_API_BASE_URL=https://api.useascend.com
ROBIE_ASCEND_API_PRODUCTION_ENABLED=1
```

Production also remains subject to the existing `ascend.create_program`
action gate. The Test operator CLI currently refuses execution outside Test.

Never put an API key in these documents, a payload, command line, GitHub, or
chat. Only the Secret Manager resource name belongs in configuration.

## GitHub history

- PR 59: API program/billable worker, authoritative verifier, schemas, runtime
  wiring, no-network planning CLI, tests, and operator documentation.
- PR 60: fixed the Test release replay's missing environment marker.
- PR 61: explicitly scoped synthetic release logic subprocesses to Test.
- PR 62: isolated release verification from live Drive-synced core rules.

Application baseline after these changes:
`4df60a0955f3500bdf51de2e81526dbe15a5a802`. Documentation-only handoff
commits may advance GitHub `main`; Test intentionally remains on this
application release until another application change is verified.

## Test deployment proof

Release archive: `robie-hermes-4df60a0955f3.tgz`

SHA-256:
`9c9f27e754beff6c84d9c616f861d49406e8ae0a413b61295ec14e22081ce308`

Both pointers resolve to:

```text
/opt/streetsmart-hermes-test/releases/4df60a0955f3/robie-hermes-4df60a0955f3
```

The pointer flipped at `2026-08-28T18:32:05.061477+00:00`.
`robie-gateway.service` entered active at
`2026-08-28T18:32:18+00:00`, after the flip.

The official proof reported:

```text
done=true
live=true
pointer_only=false
```

Install-proof Job: `be57b415-58bd-4e3c-96c1-6f907a227f64`.

The deployed VM then passed all 10 Ascend API unit and Job Engine tests.

## What was not done

- No Ascend API POST request.
- No Ascend sandbox record creation because no sandbox key is configured.
- No Production deployment to `hermes-poc-01`.
- No Production program creation.
- No PAWIVA or `221398001` access.
- No email, checkout, payment, or bind.
- No API credential stored in the repository or Test VM.

The previously supplied credential was Production-scoped and appeared in
chat. Treat it as exposed and rotate it. Do not use it as a sandbox substitute.

## Next safe runbook

1. Obtain a sandbox API key from Ascend.
2. Prove that the Test and Production secret references are isolated.
3. Store the sandbox key directly as a versioned Test Secret Manager secret.
4. Configure the three Test settings documented above and restart
   `robie-gateway.service`.
5. Use read-only sandbox requests to resolve stable Test UUIDs and identifiers.
6. Prepare a payload for an existing sandbox Test insured. Money must be
   integer cents and dates must be `YYYY-MM-DD`.
7. Run planning mode first. Confirm `network_performed=false`.
8. Run exactly one Test execution through the Job Engine.
9. Confirm the returned program and every billable through fresh API GETs and
   inspect the durable verification evidence in Test `jobs.db`.
10. Record the sandbox Test pass. Only then evaluate Production deployment and
    separately stored Production credentials.

See [ascend-api-programs.md](ascend-api-programs.md) for the payload schema and
operator command.
