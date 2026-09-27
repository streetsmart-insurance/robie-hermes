# NatGen Pending Cancellation NOC pull

Test-only list and download for National General **Pending Cancellations**
notice-of-cancellation PDFs. Process name is `natgen`. Scope is
`pending_cancellation_noc`. Policy To Dos Additional Information is a Wave A
stub: the scope name is recognized and refuses before any click.

This slice does not upload to EZLynx, file notes, create tasks, apply labels,
mark carrier rows processed, deploy, or enable a timer.

Playbook path (live DOM is **UNVERIFIED** until a hermes-test-01 run):

1. natgenagency.com, already signed in. This pull does not type the password.
2. Agent Dashboard → Your Notifications → Policy To Dos → Pending Cancellations.
3. Open the policy number → Policy History → the most recent Pending
   Cancellation or NOC → Forms View PDF.

There is no process-date filter on that list. Rows are scrubbed locally by
process date. When the requested window contains a Monday, the scrub window
also includes the preceding Saturday and Sunday.

## What runs

`NatGenRetrieval.pull_pending_cancellation_noc` checks `ROBIE_ENV=TEST` and
the existing 32-inclusive-day bound on the **requested** window, expands a
Monday back through Saturday, and downloads each in-window NOC that is not
already in the local ledger. Anything ambiguous holds before download.
`pull_policy_todos_additional_info` raises a TODO hold and does not navigate.

`python -m robie_job_engine.natgen_pending_cancellation` attaches to an
already-open NatGen tab and writes one QA pack per process date in the scrub
window. A receipt with `"status": "PULLED"` means the local pack passed the
gate below. That is not Job Engine `COMPLETE`. `"ezlynx": "not_run"` is
always set. `"additional_info": "TODO"` is always set.

## Naming

`[Policy Number] NatGen NOC non-payment.pdf`

The reason from the list is included. `Non-Payment`, `Non Payment`, and
`nonpayment` all become `non-payment`. A trailing period is stripped. A
different sanitized reason is kept in the same position. Path characters hold.

## Hermes QA pack

Every accepted window writes under the NatGen QA root. `--output` defaults
to that root. CI and laptops pass an explicit private directory so they do
not create the Hermes path.

```text
/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/natgen/{YYYY-MM-DD}/
```

A scrub window that spans days gets one subfolder per process date, including
a day with zero Pending Cancellation rows. Each date folder contains:

1. `pending-cancellations-noc-YYYY-MM-DD.png` — full Pending Cancellations list
2. That date's NOC PDFs
3. `README.md` — what Robie saw, pulled, already had, scrubbed, and held
4. `manifest.json` — carrier `natgen`, windows, `nocs[]`, and `PULLED` or `HELD`

`natgen-noc-ledger.json` stays on the QA root (machine state for replay).
Hash-named source copies stay in `{root}/sources`. Neither file is the
Nicole pack. README and manifest are replaced on each run. A different
existing PDF or PNG is kept.

The list PNG is the full Pending Cancellations page, not a day-filtered
query. When the scrub window spans days, the same PNG is copied into each
date folder. `list_day_filtered` in the manifest is false.
`screenshot_covers_window` says that image covers the scrub window.

## Verification gate

For each process date in the scrub window:

**Pending Cancellation rows for that date = official NOC PDFs saved for that date.**

And, before a PDF is filed:

**Cancel effective date printed in the PDF = cancel effective date on the list row.**

The list screenshot is taken while the Pending Cancellations list is on
screen, before any policy is opened. A count mismatch still writes the pack
with `"status": "HELD"`. A PDF whose cancel effective date does not match
the list is not filed under the official name. Its bytes are kept beside the
pack as `[Policy Number] NatGen NOC [Reason] HELD.pdf` so QA can see the
wrong document. The README records list date versus PDF date. The command
is `HELD`, not `PULLED`, and not a silent partial. An unreadable or
disagreeing date in the PDF is the same hold, without an official file.

A day in the scrub window with no rows must have no official PDFs (`0 == 0`).
Rows whose process date is outside the scrub window are listed under
"Scrubbed outside the window" and are not downloaded. They are expected,
because the carrier list is not day-filtered.

Already written official PDFs are left in place. A conflicting local file
holds the pull and is not replaced. A later pull that captures different
screenshot bytes does not replace the first PNG; it adds a sibling file.

There is no systemd timer. Do not enable one from this slice. Production
is not a target.

## Drive

The documented destination for a day's folder is shared Drive
`Robie Carrier Pull QA (Nicole)/NatGen/{YYYY-MM-DD}/`.

| | |
| --- | --- |
| Parent `Robie Carrier Pull QA (Nicole)` | `1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2` |
| NatGen child folder | **UNVERIFIED** — no child id is invented |

Folder upload is **TODO**. The existing Drive helper uploads one `video/webm`
recording; it is not used here. `--upload-drive` is off by default. When it
is set, the local pack is still written, then the command fails closed and
does not call Google or report the pack as uploaded. The manifest `drive`
status becomes `HELD`. `folder_id` stays null until a real NatGen child
folder is recorded.

The browser steps use exact accessible names from the playbook. Zero or
multiple matches hold. There is no positional click and no date field to
fill. A NOC open accepts one PDF from a download event, a new tab (blob,
natgenagency.com / nationalgeneral.com PDF URL, or embed), or a same-tab PDF
that can be returned to the list. HTML is not printed into a fake PDF. Two
different PDFs hold.

Policy History keeps the single latest Pending Cancellation or NOC. Two
history rows that share that latest date hold. Forms View and View PDF both
being present holds.

## hermes-test-01 later

Do not run this on `hermes-poc-01`. Do not point `ROBIE_ENV` at Production.
This repository change does not deploy itself. After a normal Test release
of this commit is installed on `hermes-test-01` (not done here):

1. Confirm hostname is `hermes-test-01`.
2. Leave Chrome's CDP on loopback (`http://127.0.0.1:9222` or
   `ROBIE_BROWSER_CDP_URL`). Remote CDP is refused.
3. A person completes NatGen login in that Test browser. The pull does not
   type the password. It holds if a login host or a password field is visible.
4. Leave **one** `natgenagency.com` application tab. Extra NatGen tabs hold.
5. The default output root is private (mode `0700`), and each date folder
   is too. The command creates them and holds if either is group- or
   world-accessible. Existing named PDFs that do not match the ledger are
   left in place and the pull holds. Do not pass `--upload-drive` until
   folder upload exists; it fails closed.

```bash
cd /opt/streetsmart-hermes-test/releases/current
ROBIE_ENV=TEST PYTHONPATH=. python3 -m robie_job_engine.natgen_pending_cancellation \
  --start 2026-09-28 \
  --end 2026-09-28
```

`2026-09-28` is a Monday, so the scrub window is `2026-09-26` through
`2026-09-28` (Saturday, Sunday, and Monday). That writes
`/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/natgen/2026-09-26/`
and the Sunday and Monday folders next to it. A Tuesday-only request does
not pull the prior weekend. The requested window cannot exceed 32 inclusive
days. After `PULLED`, each process-date folder must contain the PNG, README,
manifest, and the same number of official NOC PDFs as in-window rows for
that date. A count mismatch or a cancel-effective mismatch writes a `HELD`
pack and is a failed pull, not a partial success. This code has not been run
on `hermes-test-01`.

## UNVERIFIED until that Test run

- Live accessible names: `Agent Dashboard`, `Your Notifications`,
  `Policy To Dos`, `Pending Cancellations`, `Policy History`, `Forms View`,
  and a policy-number link on each row. A mismatch holds; do not widen these
  from Production. The shorter label `Notifications` is not accepted until a
  Test run shows that exact name.
- Whether the grid uses a Type column, and whether a disabled `Next` control
  is present. An enabled `Next` holds so a partial page is not treated as the
  full list.
- Policy History columns (`Date` and `Type` / `Transaction` / `Description`)
  and which control opens the NOC. The latest single Pending Cancellation or
  NOC is the rule. A tie holds.
- The PDF response host. Fetches are limited to `natgenagency.com` and
  `nationalgeneral.com` (and their subdomains), plus `blob:`. A CDN host holds.
- The labeled cancel-effective line inside a real NOC. The reader looks for
  `Cancel Effective`, `Cancellation Effective`, `Effective Date of
  Cancellation`, or `NOC Effective` with one date. A missing or disagreeing
  label holds and does not file the PDF.
- Prior delivery into EZLynx. The ledger is only this output directory.
- Test release digest, pointer flip, and rollback target. No release was
  built or installed for this change.
- N=3 clean Test jobs. Not started.
- Drive folder upload and the NatGen child folder id. The Nicole parent id
  is documented. `--upload-drive` fails closed and does not call Google.
- A run on `hermes-test-01`. The default path above is the contract; it has
  not been created by this change.
- Policy To Dos Additional Information. Wave A does not open that list.
