# Overdue Submission Center — durable Test fixes

Follow-on to the merged `#505` job. These notes lock the
`hermes-test-01` one-shot lessons into release code. Process-local
hacks (hardcoded producer→carlo maps, skipped roster reads, keyboard-only
MDC clicks) must not ship.

No Production zip or live producer email from this document.

## 1. Live MDC / Material UI controls

Space and Enter do not toggle live `mat-mdc-checkbox`, open the
paginator combobox, or change Status sort. The runner force-clicks:

- agency scope: nested `input[type=checkbox]` / `input.mdc-checkbox__native-control`
- page size 100: paginator combobox + option `100`, then verifies rendered
  `mat-row` count and the range label
- Status sort: `.mat-sort-header-container` when Enter is a no-op

A failed verify stays `PLAYWRIGHT_BLOCKED`. Helpers live in
`robie_job_engine/submission_center_controls.py`.

## 2. Google Sheets roster — Carlo grant (code cannot fix IAM)

The Test job reads the approved active-employee roster through Application
Default Credentials on the VM-attached service account. The one-shot saw
`ACCESS_TOKEN_SCOPE_INSUFFICIENT` / HTTP 403. That is an IAM / Workspace
share miss, not a missing producer map.

**Identity (typical Test VM):**
`robie-test-drive-reader@streetsmart-hermes-poc.iam.gserviceaccount.com`

Use whichever identity the accountability manifest / `hermes-test-01`
ADC actually presents if it differs. Code never hardcodes a
producers→carlo@ directory on Production paths.

**Required OAuth scopes on that SA / instance access scopes:**

- `https://www.googleapis.com/auth/spreadsheets.readonly`
- `https://www.googleapis.com/auth/drive.readonly` (Shared Drive-hosted
  roster sheets)

Either grant those two scopes explicitly or set the VM access scopes to
`cloud-platform` so ADC can mint them. Enable the Google Sheets API on
`streetsmart-hermes-poc`. Share the allowlisted roster spreadsheet with
the same SA as **Viewer**.

Until that grant exists, roster load is **fail-closed**. Test sink
`ROBIE_OVERDUE_SUBMISSION_TEST_RECIPIENT` remaps `To:` to Carlo **only
after** every producer resolves against the live roster. A Sheets miss
does not invent emails and does not send. Test still never emails
producers.

## 3. COMPLETE postcondition

A successful Gmail sent-mailbox read-back must include workflow-relevant
keys (`resource_id`, `exists_in_sent_mailbox`, plus the send counts).
`producer_count` / `qualifying_count` / `gmail_receipt_count` alone used
to trip “expected postcondition has no workflow-relevant keys” and leave
the ledger `UNVERIFIED`.

## 4. Gmail sent read-back under `gmail.metadata`

`gmail.metadata` cannot use `messages.list?q=` (403: Metadata scope does
not support the `q` parameter). Standing design is fail-open **ID
existence**: `users.messages.get` by the send receipt id with
`format=minimal`. That proves the id exists in Robie's delegated sent
mailbox. It does not re-read subject, body, or recipients.

COMPLETE still requires `exists_in_sent_mailbox` from that get. The send
API response alone is not evidence.
