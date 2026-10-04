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
- The browser is local to this VM, and no tab shows another client's account.
- The repo's own browser ownership is then taken: the cross-VM driver lease must say this host is IN, and the persistent-profile lock is held for the run. If either is refused, nothing runs.

## Manual confirmations the tool cannot see (the supervisor ticks each before the run)
- [ ] The intake timer/service and every other scheduler are stopped or disabled on the Test VM.
- [ ] No person is using the Test Chrome; it is signed in as the agency user.
- [ ] Nothing is deploying or restarting; Chrome will not be restarted.

## Commands (read-only)
```
PYTHONPATH=. python scripts/inspect_task_fields.py dom --task-id <TASK> --applicant-id 220250093 \
    --operator "<name>" --confirm-browser-owner --confirm-exclusive --output obs-dom-<date>.json
PYTHONPATH=. python scripts/inspect_task_fields.py api --task-id <TASK> --applicant-id 220250093 \
    --discussion-id <DISC> --operator "<name>" --output obs-api-<date>.json
```
- `dom` opens the task's Edit dialog (identity-checked), records the approved fields, **Cancels, and verifies the dialog is gone**, then records approved account-page fields. Add `--approved-fields-only` to omit the names-only list of the dialog's other controls.
- `api` GETs ONE discussion after verifying it belongs to the applicant. It records **key names and types only**. `--include-approved-values` adds values for approved, non-credential keys only; leave it off unless the reviewer asks.
- Output files are created exclusively (never overwritten), mode 0600; Test-account data only; keep them with the Test evidence and do not commit them unreviewed.

## What is and is not captured
- **Never read:** hidden or invisible controls, password/file/credential-like controls (including one-time codes, security answers, cards, tokens). They are excluded from metadata BEFORE any value is requested, so their values are never asked for.
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
