# Document Retrieval filing

Shared stage for carrier pull workers. Progressive FAO Communications memos
call it first. Geico, Travelers, Progressive BOP, and NatGen keep their own
pull workers and can call the same helpers later.

This does not deploy, merge, or install a timer.

## Standing window

`retrieval_date_window` is yesterday and today in `America/New_York`. On
Monday the window starts the previous Friday, so Friday through Monday are
included. The FAO memo portal itself has no Monday rule; the CLI applies
this window before attach. An explicit `--start` / `--end` outside that
window holds. `--as-of YYYY-MM-DD` pins "today" for a Test prove.

## Writes

Live EZLynx writes run only when all three are true:

1. `ROBIE_ENV=TEST`
2. Hostname `hermes-test-01` (not `hermes-poc-01`)
3. `ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX=1`

Anything else returns `disabled` or holds, and does not build an API client.
The switch defaults off. Production is refused even if the switch is set.
There is no Production timer.

Documents use `upload_document_via_api` (DocumentApi, read back `document_id`).
Notes use `add_note_to_discussion` (DiscussionApi, read back `note_id`).
Browser automation is not used for either write.

Before upload, the stage searches that applicant's documents. The same policy
and a similar file name is `skipped_duplicate`. A different memo reason is
not treated as the same file. An Activities callable is checked only when a
caller passes one. No existing retrieval worker has an Activities API client,
so Progressive memos record `activities_check: not_used`.

Notes are one short sentence and end with `ROBIE was here`. They do not
include the policy number.

## Workflow and folder

Progressive memos attach a note to an existing discussion titled
`Additional Information - Progressive Memo` when that discussion is already
on the applicant. DiscussionApi can append to an existing titled discussion
and has no create call. This stage does not invent one.

When that titled discussion is missing, ambiguous, or the filing rule has no
confirmed title, the stage still uploads the PDF (after the document dedupe),
skips Notes, and calls `robie_job_engine.zapier_tasks.fire_task`. The webhook
stays in the vault as `custom.zapier-webhook` and is loaded by `bin/zap-trigger`.
The payload is:

- `applicant_id`
- `assignee`: `Nicole Segovia`
- `source`: `document-retrieval`
- `due_date`: the Eastern filing day, ISO `YYYY-MM-DD`
- `task_title`: `Document Retrieval review — {Carrier} {DocType} — {Insured} — {PolicyNumber}`

The status-sheet comment on that path is exactly:

`Doc filed (no WF); Nicole EZLynx task created for review`

A task is created only after DocumentApi returns a numeric read-back
`document_id`, and that comment is written only after `fire_task` returns
`ok: true`. Unit tests pass `zapier_dry_run=True`, which forwards `--dry-run`
to `zap-trigger`. The live Test path uses `dry_run=False` and is still behind
the kill switch. There is no Production timer.

Applicant search that is not exactly one applicant holds before upload.
A DocumentApi error, or an upload with no read-back document id, holds and
does not fire a task. A missing daily tab or carrier section still holds the
batch before any EZLynx write and does not invent a row.

The proven DocumentApi upload fields are document name, file bytes, and
policy master id. There is no folder id on that call. A filed result sets
`folder_field` to `not_in_proven_document_upload`. When the workflow exists,
the Nicole comment is written only after both the document id and the note id
read back:

`Added to the Additional Information folder and WF: Additional Information - Progressive Memo`

## Status sheet

Spreadsheet `1HL6Uw5nAJjZ3qtCleUzXUtOC_xmhFPmy0LPbz89v7vw`, daily tab
`M/D.` (for example `9/26.`). Columns are carrier section, insured name,
policy number, department, document type, memo date, and comment. A new
Progressive row is inserted in the Progressive section. The same policy,
document type, and memo date updates the comment.

The sheet client uses Application Default Credentials and the Sheets scope.
A missing library, credential, daily tab, or carrier section holds the batch
before any EZLynx write. The stage does not create a tab, does not invent a
carrier section, and does not report a row it did not write.

Department is whatever the pull supplied. FAO Communications rows do not
include department, so that cell stays blank rather than guessed.

Items are filed one at a time. A policy search that returns zero or two
applicant ids holds that item.

## Follow-ups

- Geico pending-cancellation NOC (`geico_pending_cancellation_noc`)
- Travelers PL and CL policy activity (`travelers_retrieval`)
- Progressive BOP pending-cancel NOC (`progressive_bop`)
- NatGen pending-cancellation NOC (`natgen_pending_cancellation`)

Each should call `file_carrier_batch` with a `FilingRule`. This branch
sketches those hooks and does not rewrite the carrier pull PRs:

- `GEICO_NOC_RULE` — section `GEICO`, type `Cancellation`
- `PROGRESSIVE_BOP_RULE` — section `Progressive BOP/CGL`, type `Cancellation`
- `NATGEN_NOC_RULE` — section `NatGen`, type `NOC`
- `TRAVELERS_ACTIVITY_RULE` — section `Travelers`, type `Policy Activity`

`sketch_carrier_rule` leaves `workflow_title` and `folder` empty until that
carrier's Mail Sorting row is known. An empty title uploads the PDF, skips
Notes, and opens the same Nicole review task. Fill the title later to attach
the note when the discussion already exists. Do not copy a second EZLynx
client.

## hermes-test-01

Pull only, after a normal Test release of this commit is installed (not
done from this change):

```bash
cd /opt/streetsmart-hermes-test/releases/current
ROBIE_ENV=TEST PYTHONPATH=. python3 -m robie_job_engine.progressive_fao_memo \
  --pull-only
```

Filing, only after Carlo turns the switch on for this Test host:

```bash
cd /opt/streetsmart-hermes-test/releases/current
ROBIE_ENV=TEST ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX=1 PYTHONPATH=. \
  python3 -m robie_job_engine.progressive_fao_memo --file-ezlynx
```

Confirm hostname `hermes-test-01` first. Do not run this on
`hermes-poc-01`. Do not merge or deploy without Carlo's GO.
