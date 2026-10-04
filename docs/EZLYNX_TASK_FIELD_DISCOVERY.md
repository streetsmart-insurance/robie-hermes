# EZLynx task fields: read-only Test inspection (runbook)

Status: PREPARED, NOT RUN. Running it needs Carlo's separate approval and a Test operator. It is read-only: **no Save, no reassignment, no note, no call, no fault injection.**

## Why
Task reassignment depends on fields the repo has never observed on the real page (instructions, Created By, Assigned Producer, CSR, activity labels). All guessed selectors were removed. A field can be read only if it is declared, with an observation record, in `deploy/ezlynx_task_field_contract.json`. That file ships empty, so **reassignment is disabled everywhere (worker, call route, and the Save method itself) until this inspection fills it** and a reviewer approves the change.

## Preconditions (all must hold; the tool refuses otherwise)
- `ROBIE_ENV=TEST`, on the Test VM, applicant **220250093** ("ROBIE Test LLC") only.
- The designated task exists on that account and its ID is in `ROBIE_TASK_INTAKE_ALLOWED_TASK_IDS`.
- The persistent Test Chrome is signed in as the agency user; no other browser job is running; Chrome is not restarted.
- Nothing else runs at the same time: no intake pass, no deploy.

## Commands (run by a Test operator; read-only)
```
PYTHONPATH=. python scripts/inspect_task_fields.py dom --task-id <TASK> --applicant-id 220250093 --output obs-dom-<date>.json
PYTHONPATH=. python scripts/inspect_task_fields.py api --task-id <TASK> --applicant-id 220250093 --discussion-id <DISC> --output obs-api-<date>.json
```
- `dom` opens the task's Edit dialog (identity-checked, the same helper the reader already uses), records every visible element on the dialog and the account page (label, role, text, value, editable, visible, data-attribute names), then **Cancels**. The source contains no Save, reassign or note call (a test asserts it).
- `api` reads ONE discussion (a GET) and records the key names of its notes (values truncated). It writes nothing.
- Output files are created exclusively (never overwritten), mode 0600. They hold Test-account data only; keep them with the Test evidence, do not commit them unreviewed.
- Stop at once, and Cancel, on any unexpected dialog or control.

## What a person must establish (the tool infers nothing)
For each required field fill one row, from the observation, reviewed by a second person (Ralph or Clara):
| Field | Questions to answer from the observation |
|---|---|
| description | Which element carries the task's current instructions? Is it the same text as the report's `Note` column? |
| created_by | Task-level or account-level? Immutable after creation? Display form (full name vs username) vs the report's `Task Created By` and vs the assignee picker's option text? |
| assigned_producer, csr | Account-level or task-level? Where shown, and what exactly does each mean for routing? Who can change it, and does changing it bump the task's Last Modified in the report? Is a blank a legitimate value? |
| activity_labels | Where shown? Separator, casing, ordering vs the report's `Activity Labels`? |
Also answer from the `api` observation: which note keys carry the task ID, assignee, text, created time (with a time zone?) and last-modified; the time zone of report timestamps; whether a note's created time can be compared with the server clock (this decides whether the adopted-note 120 s tolerance, currently an unmeasured estimate, is right; measure it in a later approved step, not here).

## Turning the observation into the contract
A reviewed PR edits `deploy/ezlynx_task_field_contract.json`. Each field needs: `scope` (`dialog` or `page`), the exact accessible `label`, `read` (`input_value` or `text_content`), its `meaning`, the `report_column`, and a `verified` record (`environment`, `applicant_id`, `observed_on`, `observed_by`, `evidence`). Anything malformed or unobserved is treated as undeclared. The loader accepts only these, and only exact-one-match labels are ever used. Reading a field that cannot be read is an error, never a blank.

## After the contract is established (separate approvals, not part of discovery)
Test steps T1-T5 of the controlled Test plan; the first Save of any round still requires a fresh live proof of every field immediately beforehand.
