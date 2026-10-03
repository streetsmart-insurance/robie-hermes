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
- Reject fixture applicant bindings for every bound Chat write unless the request itself or an
  authorized injected search supplied exactly one matching applicant, regardless of name parsing. No automatic browser search is installed.
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
- Focused reconciliation/preservation suite: **288 passed**. Includes durable
  generations, reply recovery, ledger concurrency, atomic repost allowance,
  card ownership, Pub/Sub ACK coordination, exact-package promotion and stopped installation.
- Bridge unittest suite: **39 passed**.
- Full 727 base: **4,450 passed, 73 failed, 14 skipped**.
- Full final candidate: **4,530 passed, 73 failed, 14 skipped**; exact
  failing-test-ID comparison found **zero new failures**. Hosted CI evaluates the pushed head.
- Regression battery `--ci`: `ok=true`, no new failures; **INCONCLUSIVE**, with
  existing service-account, secrets, browser and job-db parity gaps. Not a green runtime result.
- Compile and `git diff --check`: passed. Tracked sensitive/runtime-file scan: zero matches.
- Two older address-note fixtures explicitly include the applicant ID in their
  synthetic request. Stop fixtures now provide a job/session task owner, the owned
  HTTP decision fixture names its runtime, and the old waiting-thread fixture now
  requires new-intent separation and duplicate idempotency. No safety expectation
  was weakened to accept stale ownership or fixture applicant provenance.

Commands:

```sh
PYTHONPATH=.:tests ROBIE_ENV=TEST python -m pytest -q tests --junitxml=/tmp/review-final-full3.xml
PYTHONPATH=services/chat-http-bridge python -m unittest discover -s services/chat-http-bridge -p 'test_*.py'
PYTHONPATH=.:tests ROBIE_ENV=TEST python -m robie_job_engine.regression_battery --ci
PYTHONPYCACHEPREFIX=/tmp/robie-reconcile-pyc python -m compileall -q deploy/hermes integrations robie_job_engine src tests
git diff --check
```

## Remaining blockers and QA handoff

The independent review of `04abb29979dbe2ec32ac9315dc1c7156b7b6c84e`
identified seven source-level defects. This follow-up adds bounded corrections:

- Raw thread ownership is read before event building, attachments, commands or jobs.
  Unknown threads are refused; unreadable ownership state propagates for NACK.
  The builder preserves owned reply threads even with a zero local message count.
- All leading mention tokens are handled consistently by bridge and gateway markers.
- Stop authority checks job, durable generation and actual live agent identity.
  A newer shared DM owner protects its task, lease and history from an old stop.
  Task cancellation uses the recorded owner, never an arbitrary background task.
- Terminal/stale questions are filtered before clarification ambiguity checks.
- Explicit new requests use the existing new-intent/defer path and cannot answer or
  rebind an old question; duplicate deliveries retain one fresh job.
- Every bound Chat API write requires one job-specific applicant provenance result.
  Possessive wording and prefilled fixture IDs cannot bypass the boundary.
- Multi-discussion selection requires an exact full title with affirmative context
  or a matching-generation clarification; substrings and negations are refused.
- HTTP card processing now also requires a recognized runtime environment; local
  decision presence cannot bypass environment validation.

**Compatibility:** unthreaded messages, Google `threadReply: false`, and a
missing `threadReply` are top-level Production requests by default. Google
omits the field on a real top-level message. A threaded message with
`threadReply: true` must already be owned by this gateway's durable database;
a present owner environment stamp must match. This deliberately refuses legacy
unmarked Production traffic that supplies an unknown thread but omits the flag.
A `prod` attribute does not override unknown thread ownership. Known legacy local
thread bindings without an environment stamp remain supported in the isolated DB.
Google documents the output-only flag in the
[Message contract](https://developers.google.com/workspace/chat/api/reference/rest/v1/spaces.messages#Message).

Unmarked Test-thread replies are **not automatically delivered to Test**. The
operator-reported Test subscription filters for the Test attribute, and the bridge
has no shared thread-to-environment registry. If a Test reply reaches Production,
it is now refused instead of becoming a Production stop/job. This safe interim
refusal does not solve cross-environment delivery or certify which ingress is live.
No subscription, Cloud Run deployment, runtime state or credentials were changed.
Release remains held for independent runtime QA and the separate operator workflow.

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
