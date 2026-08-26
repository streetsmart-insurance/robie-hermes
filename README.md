# ROBIE durable Job Engine

See `RECORDINGS.md` for per-Job EZLynx tab recording, Drive storage,
evidence-based confidence, and the human-gated reference/training policy.

## Context isolation and cost control

Interactive Google Chat DM context expires after 120 minutes of inactivity by
default. Expiration archives the structured job summary, clears account/policy/
submission identifiers, and pauses unfinished work without deleting it or
claiming completion. `/new`, `/reset`, `new job`, and `start fresh` explicitly
archive the current prompt context. Old Jobs remain searchable but are not
automatically loaded; explicit `continue` or `resume` wording is required.

Each model prompt is assembled from permanent rules, the compact active Job
summary, one relevant Skill, the relevant SOP section, and the current trimmed
tool result. `ROBIE_CONTEXT_CHAR_BUDGET` defaults to 12,000 characters and
forces compaction before additional history can be carried forward.

Every provider attempt must call `OperationsStore.enforce_model_budget` through
the fallback executor's `authorize_call` hook before network execution, then
store the returned provider usage with `record_model_attempt`. The durable Job
ledger reports input, output, cache-read, and total tokens plus estimated cost.
Budget exhaustion is a non-retryable guard condition: it pauses/escalates the
Job rather than silently falling through to another model and spending more.

The scheduler enforces expiration using `ROBIE_DM_CONTEXT_TTL_MINUTES`
(default `120`). Scheduled and monitored Jobs remain durable Jobs and are not
silently deleted when an interactive DM context expires.

This package adds a server-owned completion authority beside the existing Hermes runtime.
The Computer Worker can only return an action receipt. `JobEngine` independently reads the
destination, stores tamper-evident evidence, and is the only code path that may set `COMPLETE`.

Lifecycle:

`PENDING -> RUNNING -> VERIFYING -> COMPLETE | UNVERIFIED | FAILED`

Uncertain outcomes become `WAITING`, `NEEDS_CLARIFICATION`, `NEEDS_SKILL`,
`UNVERIFIED`, or `FAILED`. They never become silent success. Only
`JobEngine._verify` may set `COMPLETE`, and only after it reads fresh
destination state and stores authoritative evidence. Action workers cannot
authorize `COMPLETE`.

Retries use persisted `RETRY_WAIT` wakeups with exponential backoff and jitter. The action
checkpoint prevents a successful action from being repeated when only verification needs a
retry. Idempotency keys deduplicate repeated Gmail/Google Chat delivery.

Integration boundaries:

- Keep the current `hermes-gateway`, Gmail watcher, Gmail/Google Chat reporting, and Drive skill
  sync services unchanged.
- Enqueue each inbound request with the Gmail message ID or Google Chat event ID as the
  `idempotency_key`.
- Register the deterministic Playwright-backed browser port as `hermes-cua`;
  the Job Engine remains the sole execution and completion authority.
- Route carrier-proposal and browser-only read jobs through the bounded workers
  in `carrier_proposal.py` and `browser_read.py`. Those workers never expose
  terminal, raw-file, or code execution in staff Google Chat.
- Route the three hardened EZLynx actions through `HermesCuaEzlynxWorker` using
  the approved Playwright port described in `docs/PLAYWRIGHT_RUNTIME.md`.
- Implement `EzlynxReadback.api_state` from an observed EZLynx network endpoint when available.
  Return `None` when it is not; the adapter must then create a fresh navigation/session and read
  the server-backed UI state.
- Reporting reads the persisted status/evidence. It must never infer success from worker text.
- A future Gemini native Computer Use worker can implement the same `ComputerWorker` protocol;
  it is intentionally not required for this reliability gate.

Run the acceptance suite with:

```sh
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

Test Hermes operators: see `docs/TEST_HERMES_OPERATOR_RUNBOOK.md` for copying
the Razza quote into the Test artifact store, setting `ROBIE_ENV=TEST`,
installing the Test-only systemd drop-in, and running
`scripts/replay-quote-proposal.py`. That harness needs a real `--quote-pdf`
path, refuses `COMPLETE` without stored verifier evidence, and never claims
live Test `COMPLETE`.

The optional `skills/ezlynx-session-login` skill keeps the server-owned Chrome
profile authenticated using Google Secret Manager references. Credential values
are never stored in this repository. Its systemd timer runs the session check at
9:00 a.m. Eastern and stops for operator action when EZLynx requires MFA or a
CAPTCHA. See the skill's provisioning reference for deployment and rotation.
