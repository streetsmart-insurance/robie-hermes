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

Progressive memos attach to an existing discussion titled
`Additional Information - Progressive Memo`. DiscussionApi can append to an
existing titled discussion and has no create call. When Mail Sorting requires
that workflow and it is not already on the applicant, the item holds **before
upload**. This stage does not invent a create-discussion endpoint.

The proven DocumentApi upload fields are document name, file bytes, and
policy master id. There is no folder id on that call. A filed result sets
`folder_field` to `not_in_proven_document_upload`. The Nicole comment is
still the Mail Sorting sentence, and it is written only after both the
document id and the note id read back:

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

Each should call `file_carrier_batch` with its own `FilingRule` (folder,
workflow title, carrier section, document type). Do not copy a second
EZLynx client.

A proven workflow-create API is still required before a missing
`Additional Information - Progressive Memo` discussion can be opened.
Until then the item holds.

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
