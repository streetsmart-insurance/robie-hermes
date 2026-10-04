# EZLynx task fields: supervised read-only Test inspection (runbook)

Status: PREPARED, NOT RUN. Needs Carlo's separate approval. It is read-only: **no Save, no reassignment, no note, no call, no fault injection, no intake, no deployment.**

## Why
Task reassignment depends on fields the repo has never observed on the real page (instructions, Created By, Assigned Producer, CSR, activity labels). All guessed selectors were removed. A field can be read only if it is declared, with an observation record, in `deploy/ezlynx_task_field_contract.json`. That file ships empty, so **reassignment is disabled everywhere (worker, call route, and the Save method) until this inspection fills it** and a second person reviews the change.

## Separate from any effectful deployment
Do NOT install a release, flip release pointers, restart a service, or run the deploy script for this. Instead:
1. On the Test VM, verify the archive's SHA-256 against the approved digest, then extract it into a throwaway directory (for example `/tmp/inspect-<sha>`), outside the live release tree.
2. Run the scripts below from that directory with the Test VM's Python. Nothing is installed, no pointer or service changes, the intake and every scheduler stay as they are (off).
3. Delete the throwaway directory afterwards. The observation files are the only output.

## Roles
- **Operator**: runs the commands (named in `--operator`; recorded in the output).
- **Supervisor**: Carlo or a delegate; present for the whole run; can stop it at any time.
- **Reviewer**: a second person (Ralph or Clara) who reviews the observation and the contract PR; not the operator.

## One identified Test task
Record before starting, and keep with the evidence: the task ID, its discussion ID, the person who created it, applicant **220250093** ("ROBIE Test LLC"), and confirmation that the task was created for this purpose and that no other task is assigned to Robie AI on the account.

## Automatic preflight (the tool refuses, opening nothing, unless ALL hold)
- On the Test VM (`hermes-test-01`), `ROBIE_ENV=TEST`, applicant 220250093.
- `ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS` lists **exactly this one task** (so the intake could not touch another).
- Reassignment (`EZLYNX_TASK_REASSIGN_ENABLED`) and live calls (`ROBIE_PHONE_LIVE_CALLS`) are not on.
- A named `--operator`, who also passes `--confirm-browser-owner` and `--confirm-exclusive`.
- The Test jobs database shows no RUNNING/VERIFYING job and no held lease (read-only look, as the Test deploy takes).
- The browser is local to this VM, no tab shows another client's account, and the job inventory shows it idle (no RUNNING/VERIFYING job, no held lease).
- **Test must ALREADY hold the valid shared driver lease** (state IN, holder TEST, unexpired), read through the repo's own gate; the result is recorded in the output. The inspection only **checks** that lease. It does not obtain or renew it, and `exclusive_session` does the same (it checks the lease and holds the profile lock for the run). If the lease is not already Test's, nothing runs.

## Manual confirmations the tool cannot see (the supervisor ticks each before the run)
- [ ] Before the run, the operator confirms the lease on the Test VM (read-only; it changes nothing): `PYTHONPATH=. python -m robie_job_engine.ezlynx_driver_gate check` must print `ALLOWED holder=TEST reason=driver is IN`. Obtaining or renewing the lease is a separate step owned by whoever runs the driver coordinator, not part of this inspection.
- [ ] The Test Chrome is idle: no job is running, nobody is using it.
- [ ] The intake timer/service and every other scheduler are stopped or disabled on the Test VM.
- [ ] No person is using the Test Chrome; it is signed in as the agency user.
- [ ] Nothing is deploying or restarting; Chrome will not be restarted.

## Commands (read-only)
```
PYTHONPATH=. python scripts/inspect_task_fields.py dom --task-id <TASK> --applicant-id 220250093 \
    --operator "<name>" --confirm-browser-owner --confirm-exclusive --output obs-dom-<date>.json
PYTHONPATH=. ROBIE_TASK_DISCUSSION_ROUTE=<uat|live> python scripts/inspect_task_fields.py api --task-id <TASK> --applicant-id 220250093 \
    --discussion-id <DISC> --api-route <uat|live> --operator "<name>" --output obs-api-<date>.json
```
- `dom` opens the task's Edit dialog (identity-checked), records the approved fields, **Cancels, and verifies the dialog is gone**, then records approved account-page fields. Add `--approved-fields-only` to omit the names-only list of the dialog's other controls.
- **Discussion API route is explicit.** `ROBIE_TASK_DISCUSSION_ROUTE` (TEST has no default) and `--api-route` must name the same route, and the route must agree with `ROBIE_EZLYNX_DISCUSSION_API` (`live` iff live). The secret is chosen by the route alone (`uat` = UAT secret, `live` = PRODUCTION secret), its host must match, and any mismatch refuses without trying the other secret. The task flow never uses the shared SSRobie username/password (no password in the token request). The output records the route, host and secret *name*, never a value.
- `api` GETs ONE discussion after verifying it belongs to the applicant. It records **key names and types only**. `--include-approved-values` adds values for approved, non-credential keys only; leave it off unless the reviewer asks.
- Output files are created exclusively (never overwritten), mode 0600; Test-account data only; keep them with the Test evidence and do not commit them unreviewed.

## What is and is not captured
- **Never read:** hidden or invisible controls, password/file/credential-like controls (including one-time codes, security answers, cards, tokens). They are excluded from metadata BEFORE any value is requested. Because the page can change between the two stages, the second-stage script re-derives the ACTUAL element's identity, visibility and credential status first, and reads nothing if any check fails (counted as `revalidation_failed`).
- **Never captured:** neighbouring or sibling elements. Only the approved element's own value (inputs) or visible text (other elements) is read.
- **Values recorded only for** visible controls whose label matches an approved task-field pattern (instructions, created, producer, CSR, labels, assign). Those patterns decide what may be RECORDED; they are not selectors and never become the contract.
- **Names only** (no values) for the other visible controls in the Edit dialog, unless `--approved-fields-only`. Nothing else on the account page is recorded.

## Stop conditions (the tool stops and writes why; exit codes 3 and 4)
- **Unexpected page**: the activity page is not the Test account's, a login/expired-session page appears, or the task row or Edit dialog is missing or ambiguous. Nothing further is done.
- **Cancel not verified**: after Cancel the Edit Task dialog is still present. The tool stops at once and records it. **The operator closes the dialog BY HAND WITHOUT SAVING**, the supervisor notes what was on screen, and the inspection is not rerun until a reviewer agrees.
- The supervisor may stop at any time. Stopping needs no cleanup beyond closing the dialog without saving.

## What a person must establish (the tool infers nothing)
For each required field, from the observation, reviewed by the second person:
| Field | Questions |
|---|---|
| description | Which element carries the task's current instructions? Is it the same text as the report's `Note`? |
| created_by | Task-level or account-level? Immutable? Display form vs the report's `Task Created By` and the assignee picker's option text? |
| assigned_producer, csr | Account-level or task-level? Where shown and what does each mean for routing? Who changes it, and does that bump the task's Last Modified in the report? Is blank legitimate? |
| activity_labels | Where? Separator, casing, ordering vs the report? |
From the `api` observation: which note keys (and types) carry the task ID, assignee, text, created time, last-modified; whether created times carry a time zone; the report's time zone. The adopted-note 120 s tolerance is an unmeasured estimate and is measured in a later approved step, not here.

## Turning the observation into the contract
A reviewed PR edits `deploy/ezlynx_task_field_contract.json`: for each field, `scope` (`dialog` or `page`), the exact accessible `label`, `read` (`input_value` or `text_content`), its `meaning`, the `report_column`, and a `verified` record (`environment`, `applicant_id`, `observed_on`, `observed_by`, `evidence`). The loader accepts only valid entries; reading a field that cannot be read is an error, never a blank. If the observation shows a field cannot be read by an exact-label lookup, the reader must be extended in a separate reviewed change; do not guess.

## After the contract is established (separate approvals)
Test steps T1-T5 of the controlled Test plan. The contract file is inside the release, so filling it changes the digest; T3 onward needs the re-stamped release and a fresh approval.

## Separate, narrower lookup of discussion IDs (`scripts/lookup_applicant_discussions.py`)
Different code from the inspector; its review is separate. Test VM only, applicant 220250093 only, approved live route only, and a client with no user-login grant and no browser-session access (it refuses otherwise). It makes the client's vendor token request and two GETs (`ids-by-applicant`, `by-applicant`), writes nothing, and prints to stdout only: discussion IDs, note counts and note key names with value types (credential-named keys omitted). No titles, no note text, no values. `--task-id <id>` adds a true/false per discussion for whether that exact ID is held under a task-named key; the ID is not echoed.

## Authentication boundaries of the task flow's Discussion API client
The authentication endpoint and API origin must be exactly the approved HTTPS pair for the route (live: `https://app.ezlynx.com/auth/connect/token` and origin `app.ezlynx.com`; uat: the `app.uatezlynx.com` equivalents), taken from the repo's own constants; anything else (a different scheme, host, port, userinfo, path, query, or a mixed live/UAT pair) refuses before any request. Every request is re-checked against that origin and redirects are never followed. The client reads and attaches no browser cookies unless a caller explicitly passes `browser_session=True` (the intake's existing wrapper does, to keep its behaviour unchanged; the inspector and the lookup refuse such a client). The production secret's real endpoint values were NOT read to confirm they equal the approved pair; if they differ the code refuses rather than misroutes, and the approved pair must then be reviewed.

## Discovery is read-only, including by side effect, and prints only safe failures
- `DiscussionApiClient.get_discussions()` writes a `discussion_choices` checkpoint when a job context is inherited (`ROBIE_JOB_ID`/`JOB_ID`, a turn owner or a resumed job). Discovery therefore (a) passes `remember_choices=False` and (b) REFUSES to run at all when a job context is inherited, before any secret is loaded or request made (`assert_no_inherited_job_context`). A `ROBIE_JOB_DB` path alone is not a job context.
- Failures print only a category and HTTP status, for example `FAILED: discussion_api_error status=403`, `discussion_api_error category=transport_or_parse`, `unexpected_error (RuntimeError)` or `REFUSED: ...` with our own fixed text. Response bodies, exception text and tracebacks are never printed or written (exit code 1 for an unexpected failure, 2 for a refusal, 3/4 for the inspector's stop conditions).
