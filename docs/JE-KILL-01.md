# JE-KILL-01 — restart safety at the consequential-action boundary

Status: **engine logic merged and deployed to Test; live browser proof remains unverified**
Environment exercised: local/CI synthetic fixtures and the Test release verifier
Production: **not deployed; do not promote without the live proof**

## Safety invariants

1. A dead worker cannot hold the one-active-run slot forever.
2. A live worker's heartbeat prevents another worker from stealing its run.
3. Every mutating EZLynx attempt persists an action intent before calling the browser worker.
4. If an intent survives without an action checkpoint, the worker cannot run again until an authoritative destination read proves the consequence is absent.
5. An authoritative `APPLIED` result records/reconstructs the action checkpoint and continues with independent verification; it does not repeat the write.
6. An authoritative `NOT_APPLIED` result permits one new perform attempt.
7. Missing, non-authoritative, stale, mismatched, or unavailable reconciliation holds the Job and never calls the worker.
8. Independent fresh destination evidence remains the only path to `COMPLETE`.

## Design

### Isolated-run lease

`isolated_runs` now stores `heartbeat_at` and `lease_expires_at`. `JobEngine` renews the isolated-run lease alongside the Job and durable-work leases. Starting a run atomically reconciles expired `ACTIVE` rows to terminal `ABANDONED`, records a `startup-reconciliation` binding, and then competes for the existing database-enforced one-active-run slot. Unexpired runs are still rejected.

The schema change is additive. A pre-change `ACTIVE` row has no lease and is therefore treated as stale at the first post-change startup. Release rules already prohibit changing code while an active Job is running.

### Consequence reconciliation

For `ezlynx.reassign`, `ezlynx.move_document`, and `ezlynx.apply_label`, the engine writes an `action_intent` checkpoint immediately before invoking the worker. A restart that finds an intent but no action checkpoint enters the destination reconciler before any new write.

`EzlynxDestinationVerifier` supplies that read-before-write contract from the existing API/fresh-page readback port:

- exact authoritative match → `APPLIED`;
- authoritative empty destination → `NOT_APPLIED`;
- mismatch, unavailable readback, missing identity, or other ambiguity → `UNKNOWN` and a fail-closed hold.

The durable-work ledger allows a recovery owner to lease an already-recorded but unverified work item only when an action intent exists and its action checkpoint is missing. This covers a death after ledger increment but before checkpoint persistence without weakening the normal at-most-one-action guard.

This does not claim transactional exactly-once behavior across SQLite and EZLynx. It provides an application-level reconciliation barrier: **read authoritative destination state before deciding whether another consequence is safe**.

## Deterministic kill harness

`tests/test_je_kill_01.py` uses a spawned child process and a separate SQLite-backed fake destination. The parent kills the child process; it does not raise a mocked worker exception and does not edit lease rows. One-second leases expire naturally. It also directly exercises the real `EzlynxDestinationVerifier.reconcile` implementation for absent, applied, and unavailable destination states.

Covered boundaries:

1. **Before action:** restart reads `NOT_APPLIED`, performs once, and completes only after verification.
2. **After action / before ledger and action checkpoint:** restart reads `APPLIED`, reconstructs the action checkpoint, performs zero additional writes, and verifies.
3. **During verification:** restart reuses the durable action checkpoint and repeats readback only.
4. **After ledger / before action checkpoint:** restart leases the unverified work item for reconciliation and performs zero additional writes.
5. **No reconciler:** Job holds `NEEDS_CLARIFICATION`; worker call count remains zero.
6. **Non-authoritative `NOT_APPLIED`:** downgraded to `UNKNOWN`; worker call count remains zero.

Every success case asserts exactly one fake destination consequence, one ledger external action, authoritative verification, an `ABANDONED` killed run where applicable, and a terminal `COMPLETE` recovery run.

## Guarded Test-only live browser harness

`python -m robie_job_engine.je_kill_live` is a separate, guarded proof for
`hermes-test-01`. It is not invoked by Chat intake and it never discovers or
guesses a customer record. The protected-main workflow
`.github/workflows/je-kill-test.yml` will run only when all of these hold:

- `ROBIE_ENV=TEST` and the exact hostname is `hermes-test-01`;
- both Test release pointers match the workflow commit;
- `/opt/streetsmart-hermes-test/je-kill/fixture.json` exists;
- the fixture declares `test_only=true`, `disposable=true`, Carlo's
  `JE-KILL-01`-scoped approval from the last seven days, and three distinct
  disposable document IDs;
- every URL is an authenticated HTTPS `ezlynx.com/web/` URL;
- every locator is explicit and non-positional;
- none of the configured accounts is the known forbidden PAWIVA account.

The three scenarios use isolated Job databases under
`/opt/streetsmart-hermes-test/je-kill/runs`. The official remote script
(`scripts/run-je-kill-test-remote.sh`) runs on `hermes-test-01` as
`streetsmart-hermes-test` with `/opt/streetsmart-hermes-test/venv/bin/python`
from a cwd that user can `chdir` into. It does not use Production
`streetsmart-hermes` home or venv paths.

Playwright sync stays on one owner thread for the CDP connection. The Job
Engine timeout pool must not call `page.goto` / locators after reconcile
opened CDP on another greenlet. `port.close()` disconnects that CDP client
only; it must not close the operator-owned Test Chrome the parent still
needs after a killed child.

The parent process kills a real
child before the action, after the browser consequence but before Job Engine
recording, and after authoritative verification but before evidence is
persisted. Each restart must end with one browser-action attempt, one durable
external action, one authoritative verified evidence row, an `ABANDONED`
killed run, and `COMPLETE`. Zero browser tabs, multiple EZLynx tabs, an
expired session, locator ambiguity, unavailable readback, or a missing fixture
fails closed before a repeat action.

The fixture example is `docs/JE-KILL-01.fixture.example.json`. Placeholder
values are not executable evidence. A real disposable Test fixture must be
created out of band without putting client identifiers or credentials in Git,
GitHub logs, or chat.

## Local verification record

- `PYTHONPATH=tests:. python3 -m unittest -v tests.test_je_kill_01` — 7 passed.
- Reliability/acceptance selection before the final reconciler-specific addition — 89 passed.
- `git diff --check` — passed.
- Python compilation — passed on the local host.

The repository's complete regression battery cannot be certified on this host: it has Python 3.9.6 while the project requires Python 3.11+ and CI uses Python 3.12; `pytest` and Google Pub/Sub bridge dependencies are also absent. The observed full-battery failures are pre-existing host/runtime incompatibilities (`str | None` evaluation under Python 3.9, missing `pytest`, missing Google Pub/Sub, and one deploy-truth environment assertion). GitHub CI must provide the authoritative full-gate result for this branch.

## Review questions

1. Confirm `ABANDONED` is the desired terminal event name for expired isolated runs.
2. Confirm the existing EZLynx API/fresh-page readback implementations distinguish authoritative absence from an unavailable session in each deployed adapter.
3. Confirm every real mutating EZLynx runtime wires `EzlynxDestinationVerifier` (or an equivalent reconciler); missing wiring intentionally holds rather than repeats.
4. Review whether additional consequential action types should opt into the same action-intent/reconciliation contract in a follow-up.
5. Review lease timing and monitoring before any Test deployment.

## Explicit non-claims

- The engine release was deployed only to Test; no live browser consequence
  has been exercised until the guarded workflow records `TEST VERIFIED`.
- No Production Job or database was read or modified.
- A green synthetic suite or Test deployment is not the live Manual Renewals
  proof. Missing live evidence remains `UNVERIFIED`.
