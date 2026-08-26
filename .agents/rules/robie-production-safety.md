# ROBIE production safety constitution

These rules are mandatory for every Antigravity agent and workflow in this project.

## Completion authority

- The Computer Worker may perform actions and return receipts, but it may never authorize `COMPLETE`.
- Only the durable Job Engine may mark `COMPLETE`, and only after independent destination read-back and persisted authoritative evidence.
- Missing or non-authoritative proof produces `UNVERIFIED`; a safely terminated action produces `FAILED`. Never convert either status to success based on worker language, screenshots of local DOM state, or an action receipt.

## Production protection

- Never edit Production code, Skills, systemd units, configuration, or runtime data directly.
- Use `feature branch -> checks -> Test -> verified evidence -> approval -> immutable Production promotion`.
- Promote the exact release digest verified in Test. Never rebuild between Test and Production.
- Preserve a one-action rollback to the previously verified Production digest.
- Before deployment, inventory open Jobs and active leases. Do not invalidate, duplicate, delete, or silently abandon them.
- Do not disrupt Hermes/cua-driver, the persistent EZLynx browser profile, Google Chat, Gmail, Drive, schedules, Job state, or Jake's existing access.

## Secrets and data

- Never display, copy into prompts, commit, upload, or store raw credentials, OAuth tokens, cookies, browser profiles, client artifacts, recordings, logs, or Job databases.
- Use scoped identities and Secret Manager references. Do not create long-lived keys when workload identity or service-account impersonation is available.
- Treat repository scans and deployment logs as potentially sensitive; store only the minimum evidence needed.

## Reliability and cost

- Preserve checkpoints, idempotency, retries/backoff, pause/resume/wakeup, durable polling, attachment ingestion, source recovery, two-hour interactive context expiration, token budgets, cost accounting, and deliberate failure-mode tests.
- Cached state may reduce token and API usage, but cached EZLynx page state is never completion evidence.
- Exceeding retry, token, time, or cost limits pauses or escalates the Job; it never causes false `COMPLETE`.

## Truthfulness

- Do not state that a connection, deployment, verification, promotion, or rollback succeeded until the live StreetSmart environment independently proves it and the evidence location is recorded.

