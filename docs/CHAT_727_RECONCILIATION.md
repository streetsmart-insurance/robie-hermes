# PR 727 selective Chat reconciliation — 2026-10-02

## Candidate and authorization boundary

Release candidate: PR [727](https://github.com/streetsmart-insurance/robie-hermes/pull/727),
branch `codex/chat-lifecycle-reviewed-3347e65`.
Base: `5a7acd31be0e69178ca0ace0c5d6a6738322de7c` (includes PR 731 stopped-install safeguards).
Main at task start: `77a9facbd87dca37321f1bf2de9b131414a45a43`.
Selective source: PR 725 snapshot `4230ae87af7738547558f83d2ca0df635a9d5e67`.
The moving PR 725 head was not merged or copied wholesale. PR 725 metadata was not changed.

Carlo's delegated assignment covers both PRs and a tested, normal push to 727.
This work changes source and synthetic tests only. No credentials, client systems,
runtime state, subscriptions, deployments, merges, or external notifications were used.
The Mac operator owns runtime inventory and authentication separately.

## Implemented slice

- Interrupt the actual thread agent even when its durable job already says CANCELLED.
  Resolve top-level stop using the stored job thread; do not substring-match another thread.
- Deactivate cancelled conversation links while preserving job and link history.
- Refuse tools for terminal/unreadable jobs and superseded generations; retain normal final-reply delivery.
- Suppress long tool traces, mixed heartbeat traces and intermediate navigation refusals as final replies.
  Preserve legacy single-line progress notices without completing jobs.
- Persist a live clarification's actor, environment, question ID and generation.
  An authorized reply resolves only that question, once, without opening a second session.
  Store errors propagate for NACK; cancelled jobs cannot be woken by a stale card.
- Enforce `prod`/`test` selection in the actual VM message dispatch before attachment,
  command or job processing. Pass Pub/Sub attributes through; reject invalid/conflicting
  stamps; normalize Test markers after mentions and in `argumentText`.
  Explicit card environment cannot be overridden by a matching local database row.
- Recheck the driver at the DiscussionApi, JSON and multipart transport boundaries.
  Bound Chat writes require the same RUNNING durable generation and database.
  Process-level job variables cannot substitute a newer job for the bound owner.
- Reject fixture applicant bindings for named requests unless the request itself or an
  authorized injected search supplied that applicant. No automatic browser search is installed.
  A model-invented discussion title cannot select among several discussions.
- Attribute note-tool receipts to the bound turn instead of mutable job variables.

No changes to 727's durable UUID generation, exact nonempty note-ID receipts,
independent note readback, POST-exception uncertainty, cross-process ledger locking,
atomic repost approval, cross-turn session protection, outbox request IDs,
exact-package promotion, skip-policy-setup, or stopped-install safeguards.

## Validation

Use Python 3.12 with the dependencies from `.github/workflows/ci.yml`.
All destinations and API transports in reconciliation tests are synthetic.

- New regressions: `tests/test_chat_reconciliation.py`, selectively adapted from
  725's live-round, progress/signout, and round-27 fixtures. These do not import
  the unsafe seal or blanket sending-lease recovery.
- Focused reconciliation/preservation suite: **254 passed**. Includes durable
  generations, reply recovery, ledger concurrency, atomic repost allowance,
  card ownership, Pub/Sub ACK coordination, exact-package promotion and stopped installation.
- Bridge unittest suite: **39 passed**.
- Full 727 base: **4,450 passed, 73 failed, 14 skipped**.
- Full final candidate: **4,496 passed, 73 failed, 14 skipped**; exact
  failing-test-ID comparison found **zero new failures**. Hosted CI evaluates the pushed head.
- Regression battery `--ci`: `ok=true`, no new failures; **INCONCLUSIVE**, with
  existing service-account, secrets, browser and job-db parity gaps. Not a green runtime result.
- Compile and `git diff --check`: passed. Tracked sensitive/runtime-file scan: zero matches.
- Two older address-note fixtures now explicitly include the applicant ID in their
  synthetic request, rather than relying solely on a prefilled payload ID.

Commands:

```sh
PYTHONPATH=.:tests ROBIE_ENV=TEST python -m pytest -q tests --junitxml=/tmp/reconcile-final-head.xml
PYTHONPATH=services/chat-http-bridge python -m unittest discover -s services/chat-http-bridge -p 'test_*.py'
PYTHONPATH=.:tests ROBIE_ENV=TEST python -m robie_job_engine.regression_battery --ci
PYTHONPYCACHEPREFIX=/tmp/robie-reconcile-pyc python -m compileall -q deploy/hermes integrations robie_job_engine src tests
git diff --check
```

## Remaining blockers and QA handoff

**Unmarked Test-thread replies are not automatically routed to Test.** The operator-reported
Test subscription accepts only `attributes.robie_env = "test"`. The HTTP bridge has no
shared durable thread-to-environment registry. Gateway code cannot process a message that
its subscription never delivers. Candidate bridge marker stamping and VM checks do not prove
which ingress is live, and this task does not assume a Cloud Run redeploy fixes that gap.
Until a reviewed ingress design exists, Test replies/stop must arrive with an explicit Test
attribute (or a marker through an ingress that actually stamps that attribute).

The local guards prevent foreign/invalid events that reach a gateway from executing there;
they do not establish cross-environment thread ownership for unmarked events. No subscription
or routing infrastructure was changed. This unresolved requirement blocks a claim that the
whole reconciliation is runtime-ready.

Deferred: stuck-tab recovery, live name-search installation, safety-seal implementation,
blanket sending-lease recovery (unsafe for live owners), phone/Bland, PFA, workers/services,
Playground/SOP expansion, email-agent, login bootstrap and driver-management changes.
Existing reply recovery remains unchanged; no live sending lease is reset by this patch.

Independent Reliability / QA must verify: actual ingress/subscription delivery; one job and
one reply under duplicate delivery; Test and Prod separation; marker removal; stop during a
live clarification; unauthorized/stale/duplicate clarification replies; final reply after
terminal-tool refusal; driver OUT/mismatched/expired immediately before POST; exact note-ID
readback and a later unrelated failure; missing/unreadable generation state; no fixture-ID
inheritance; and no weakening of stopped installation or exact-byte promotion.

Test release/digest: **not deployed by this task**. Runtime QA: **not performed**.
Applicant 26356199 / Buster Brown restrictions remain operator-owned; no client access occurred.
Open-job hold and a current rollback artifact must be independently established before any
stopped Test installation. Operator reports of changing Test pointers are context, not a
rollback certification by this coding task. Production promotion remains separately gated.
