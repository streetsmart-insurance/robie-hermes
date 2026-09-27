# Progressive FAO Communications memo pull

Test-only list and download for For Agents Only **Communications → Memo**
rows. Process name stays `progressive`. Scope stays `fao_communications`.
The pull itself does not upload to EZLynx. The optional filing stage, behind
the Test kill switch, can upload a document, append a note when the titled
workflow already exists, or open a Nicole review task when it does not.
This change does not apply labels, mark carrier rows processed, deploy, or
enable a timer.

Manual prove (Dusty, 2026-09-26, not re-run by this code): login at
foragentsonly.com → foragentsonlylogin.progressive.com (user id, password,
SMS OTP) → Manage Policies → Policy Activity → processed date 2026-09-25 →
Communications → Memo rows. Agent context on that session was **CA33617**.
Four PDFs were saved by hand:

| Policy | Insured | Reason | File |
| --- | --- | --- | --- |
| 860521214 | 3JR Contracting LLC | General | `860521214 Progressive Memo General.pdf` |
| 879512352 | ALTI TRANSPORT LLC | Policy Verific. | `879512352 Progressive Memo Policy Verific.pdf` |
| 993334183 | Yolanda Concepcion | Signature | `993334183 Progressive Memo Signature.pdf` |
| 983754955 | MHS LLC | General | `983754955 Progressive Memo General.pdf` |

A trailing period on the reason is stripped in the filename. The code's
fixture tests use those identities with synthetic PDF bytes. The customer
PDFs are not in git.

## What runs

`ProgressiveRetrieval.pull_fao_communications` checks `ROBIE_ENV=TEST`, the
existing 32-inclusive-day window, and that every row is a unique actionable
memo whose processed date is inside the window. Rows already recorded in the
local ledger are skipped. Anything ambiguous holds the pull before download.

`python -m robie_job_engine.progressive_fao_memo` attaches to an
already-open FAO tab and writes one QA pack per processed date. A receipt
with `"status": "PULLED"` means the local pack passed the verification gate
below. That is not Job Engine `COMPLETE`.

`--pull-only` leaves `"ezlynx": "not_run"`. Omitting that flag, or passing
`--file-ezlynx`, asks the shared filing stage to run after the pull. The
stage still does not write unless `ROBIE_ENV=TEST`, the host is
`hermes-test-01`, and `ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX=1`. With the
switch off the receipt stays `PULLED` and `"ezlynx": "disabled"`. A missing
`Additional Information - Progressive Memo` discussion uploads the PDF,
skips the note, and asks Zapier for a Nicole review task. Dates must sit in
the standing window: yesterday and today in `America/New_York`. Monday also
includes Friday through Sunday. See
[DOCUMENT_RETRIEVAL_FILING.md](DOCUMENT_RETRIEVAL_FILING.md).

## Hermes QA pack

Every accepted window writes under the Progressive QA root. `--output`
defaults to that root. CI and laptops pass an explicit private directory
so they do not create the Hermes path.

```text
/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/progressive/{YYYY-MM-DD}/
```

A window that spans days gets one subfolder per processed date, including a
day with zero Memo rows. Each date folder contains:

1. `fao-communications-memo-YYYY-MM-DD.png` — full-page Communications list
2. That date's memo PDFs (`[PolicyNumber] Progressive Memo [Reason].pdf`)
3. `README.md` — what Robie saw, pulled this run, already had, and held
4. `manifest.json` — carrier `progressive`, window, `memos[]`, and `PULLED` or `HELD`

`fao-memo-ledger.json` stays on the QA root (machine state for replay).
Hash-named source copies stay in `{root}/sources`. Neither file is the
Nicole pack. README and manifest are replaced on each run. A different
existing PDF or PNG is kept.

When the window spans days, the same on-screen list PNG is copied into
each date folder. `screenshot_covers_window` in the manifest says that image
is the window list, not a second query filtered to that day.

## Verification gate

Robie's gate for a pull, checked separately for **each processed date** in
the requested window:

**Memo rows on the Communications list for that date = PDFs saved for that date.**

The list screenshot is taken while the Communications section URL is open,
before any Memo is opened. The QA pack, including that PNG, is written for
every date in an accepted window. A count mismatch still writes the pack
with `"status": "HELD"` and the hold reason, then the command exits `HELD`.
The receipt is not `PULLED`. Already written PDFs are left in place; they
are not deleted and they are not a successful partial. An extra PDF for
that same processed date fails the same way. A day in the window with no
Memo rows must have no PDFs for that date (`0 == 0`).

A later pull that captures different screenshot bytes does not replace the
first PNG; it adds a sibling file.

There is no systemd timer. Do not enable one from this slice. Production
is not a target.

## Drive

The documented destination for a day's folder is shared Drive
`Robie Carrier Pull QA (Nicole)/Progressive/{YYYY-MM-DD}/`.

| | |
| --- | --- |
| Parent `Robie Carrier Pull QA (Nicole)` | `1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2` |
| Progressive child | `1MMojqm99ft4DgxplMuBvz-eTnKdgpY9U` |

Folder upload is **TODO**. The existing Drive helper uploads one `video/webm`
recording; it is not used here. `--upload-drive` is off by default. When it
is set, the local pack is still written, then the command fails closed and
does not call Google or report the pack as uploaded. The manifest `drive`
status becomes `HELD`.

Manage Policies accepts the live header control: accessible name
`Manage Policies Home`, any link or button whose accessible name matches
`^Manage Policies`, or `a[data-at="header-nav__parent-link--manage-policies"]`.
Those selectors are one element. Policy Activity accepts `Policy Activity`
or `View policy activity reports`. If the chosen control is not visible,
Main Navigation is clicked once and the control is resolved again. Zero
matches, two visible matches, or two Manage Policies elements hold. There
is no positional click. Policy Activity then selects View Activity By
`Processed Date` (`select#PDDateType[name="DateType"]`, option value
`PROCESSEDDATE`). `select#PDDateRange` is a preset list (Yesterday, Last 30
Days, Select Date Range, and others), not a wrapper around the date fields.
The run chooses the single option whose visible text is `Select Date Range`.
The option value is read from that option. It then waits until the page-level
Start Date and End Date inputs are visible, fills them (`YYYY-MM-DD`), and
clicks `Get Policy Activity` (`[data-at="ProcessedDateButton"]`, unique on
the page). Start is
`input[type=date]#js-datepicker__date-start[data-at="datatable-daterangepicker-startdate"]`.
End is `input[type=date][data-at="datatable-daterangepicker-enddate"]`. Both
can sit in the DOM hidden until Select Date Range is chosen, and neither is
a child of `select#PDDateRange`. The labels `Processed date from` and
`Processed date to` are not in that DOM. A missing, still-hidden, or second
match holds. Get Policy Activity then requires the processed-date results
URL (`/managepolicies/policyactivity/processeddateresults/<section>/`).
The live landing is section `cancels` (title Policy Activity Processed Date
Results – Cancels, Lapses, Reinstates). Sibling sections are the same page
family. There is no Search button on that page. The worker does not click
Search. The next control is the Communications section link
`a[data-at="policy-activity-tab-communications"]`, or one link named
`Communications` when that element is absent. Those two queries must be the
same element when both match. `role=tab` and `aria-selected` are not
required and are not clicked. After the click the URL must stay under
`processeddateresults` with section `communications` or `underwriting`
(optional legacy suffix: `underwriting`, `underwritinglegacy`,
`underwriting-legacy`, `underwriting_legacy`). Staying on `cancels`, a
second link, or any other section holds. Memo stays
the same exact name. A missing or second match holds. A unique
`data-at` or id match is used when that element has no accessible name; a
different accessible name, or the expected name on another element, holds.
A Memo open accepts one PDF from a download event, a new tab (blob, Progressive/foragentsonly PDF
URL, or embed), or a same-tab PDF that can be returned to the list. HTML is
not printed into a fake PDF. Two different PDFs hold.

Policies Need Service and BOP pending-cancel are not navigated.

## hermes-test-01 later

Do not run this on `hermes-poc-01`. Do not point `ROBIE_ENV` at Production.
This repository change does not deploy itself. After a normal Test release
of this commit is installed on `hermes-test-01` (not done here):

1. Confirm hostname is `hermes-test-01`.
2. Leave Chrome's CDP on loopback (`http://127.0.0.1:9222` or
   `ROBIE_BROWSER_CDP_URL`). Remote CDP is refused.
3. A person completes FAO login and SMS OTP in that Test browser. The pull
   does not type the password or the OTP. It holds if the login host or a
   password field is visible.
4. Leave **one** `foragentsonly.com` application tab. Extra FAO tabs hold.
5. The shell must show StreetSmart agency `CA33617` and no second agency.
   Live FAO Home may omit the `CA` prefix and show `(33617)` and/or login id
   `33617c`; those are the same agent. A missing code, or a second
   `CA#####`, `(#####)`, or `#####c` login, holds the pull. Override only
   with `--agent-code` / `PROGRESSIVE_FAO_AGENT_CODE` when the Test session
   is actually that code.
6. The default output root is private (mode `0700`), and each date folder
   is too. The command creates them and holds if either is group- or
   world-accessible. Existing named PDFs that do not match the ledger are
   left in place and the pull holds. Do not pass `--upload-drive` until
   folder upload exists; it fails closed.

```bash
cd /opt/streetsmart-hermes-test/releases/current
ROBIE_ENV=TEST PYTHONPATH=. python3 -m robie_job_engine.progressive_fao_memo \
  --pull-only \
  --as-of 2026-09-26 \
  --start 2026-09-25 \
  --end 2026-09-25
```

That writes
`/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/progressive/2026-09-25/`.
`--start` and `--end` must fall inside yesterday and today (Monday includes
Friday through Monday). Omit both flags to use that window. After `PULLED`,
that date folder must contain the PNG, README, manifest, and the same number
of memo PDFs as Memo rows on that page. A count mismatch writes a `HELD`
pack and is a failed pull, not a partial success. This code has not been
run on `hermes-test-01`.

Do not export `ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX=1` until Carlo says the
Test filing path may call EZLynx. `--file-ezlynx` without that switch does
not upload or write a note.

## UNVERIFIED until that Test run

- Manage Policies and Policy Activity names below were read off the
  authenticated FAO Home session that held on release `3f774852151c`. This
  commit is not that release and has not been installed on `hermes-test-01`.
  The header link's accessible name is `Manage Policies Home`
  (`aria-label`), with `a[data-at="header-nav__parent-link--manage-policies"]`.
  It was hidden until Main Navigation was clicked once. Policy Activity on
  the landing is `View policy activity reports` and/or header `Policy Activity`,
  which may also be hidden until that expand. Release `65740660` reached
  Policy Activity, so that Manage Policies hold is no longer the open failure.
- Policy Activity date filter. Release `3b651fadabb6` held on
  `Processed date from` because those labels are not in the DOM. Release
  `65740660` (includes #606) cleared that hold, reached Policy Activity, and
  held with `Progressive control 'Start Date' is missing or ambiguous`.
  Live DOM on that page: `#PDDateType` is View Activity By;
  `#PDDateRange` is a `<select>` of presets (Yesterday, Last 30 Days,
  Select Date Range, …), not a container; Start/End
  `input[type=date]#js-datepicker__date-*` exist with `visible=false` until
  the custom preset is chosen; `Get Policy Activity`
  `[data-at=ProcessedDateButton]` is present and visible. #606 scoped
  Start/End under `#PDDateRange`, so the scoped count was not 1. This
  commit selects `Select Date Range`, waits until Start and End are
  visible at page scope, then fills them. Release `00a0ae294ec0` (#612) is
  that change. Official `--pull-only` on hermes-test-01 cleared the date
  window, clicked Get Policy Activity, and landed on
  `https://www.foragentsonly.com/managepolicies/policyactivity/processeddateresults/cancels/`
  (Policy Activity Processed Date Results – Cancels, Lapses, Reinstates).
  The next line clicked a Search button. The receipt was
  `Progressive control 'Search' is missing or ambiguous` (`ezlynx: not_run`,
  `status: HELD`). Communications and Memo were not reached (0 PDFs).
  Search is not on that results page. Release `6eb4750874c6` (#614) stopped
  that click and required a Communications `role=tab` with
  `aria-selected=true`. On the same authenticated FAO session the live
  control is a link, `a[data-at="policy-activity-tab-communications"]`,
  whose target is a `processeddateresults` underwriting path (underwriting,
  or that slug with a legacy suffix). A manual click loaded Communications.
  The Saturday–Sunday window showed 0 Records Found. That empty list is
  data; this commit does not invent Memo rows or PDF clicks for it. A
  missing memo table still holds after the section opens. Official
  `--pull-only` can still time out re-entering Policy Activity from Manage
  Policies (about 30 seconds on the Playwright click). That flake is
  unchanged. This commit clicks the unique Communications section link and
  requires the Communications/underwriting results URL. It has not been
  installed, and a completed pull is still UNVERIFIED. After merge and a
  Test zip, re-run `--pull-only` on `hermes-test-01` and confirm the page
  leaves `cancels` for `underwriting` or `communications` without a Search
  click.
- Ambiguous date UI stays fail-closed in this commit. The StreetSmart HITL
  ladder for a later change is Gemini for an open-ended read (“what am I
  looking at / which control”), then **Jev** (TypeSafe System One) as the
  typed judgment gate (boolean, choice, or score, plus confidence) — for
  example “does this look like the processed-date filter we expect?” or
  “quote-only vs complete.” No Jev client, network call, or secret is added
  here. Keys are not configured.
- Still exact, and still UNVERIFIED on a completed pull: a Memo link or
  button on each row. A mismatch holds. The Communications page observed
  after the section link showed 0 Records Found. Memo and PDF controls were
  not in that DOM, so this commit does not add a click for them. If that
  empty page has no single memo table, the pull holds after the section
  URL matches. Do not treat that hold as a zero-row success.
- Whether the communications grid uses a Type column, and whether a disabled
  `Next` control is present. An enabled `Next` holds so a partial page is
  not treated as the full day.
- The PDF response host. Fetches are limited to `foragentsonly.com` and
  `progressive.com` (and their subdomains), plus `blob:`. A CDN host holds.
- Prior delivery into EZLynx. The ledger is only this output directory.
- Test release digest, pointer flip, and rollback target. No release was
  built or installed for this change.
- N=3 clean Test jobs. Not started.
- Drive folder upload. Destination ids are documented only. `--upload-drive`
  fails closed and does not call Google.
- A run on `hermes-test-01`. The default path above is the contract; it has
  not been created by this change.
