# Travelers Policy Activity Report pull

Test-only list and download for Travelers for Agents **Policy Activity Report**.
Personal Lines and Commercial Lines share one module and one CLI. `--lob pl`
or `--lob cl` selects the line. This slice does not upload to EZLynx, file
notes, create tasks, apply labels, mark carrier rows processed, deploy, or
enable a timer.

Live portal controls are **UNVERIFIED**. The names below are the playbook
path. Zero or multiple matches hold. Do not widen them from Production.

## Paths

| Line | `--lob` | Navigation |
| --- | --- | --- |
| Personal | `pl` | Personal Insurance → Featured Reports → Policy Activity Report |
| Commercial | `cl` | Business Insurance → Agency Reports → Policy Activity Report |

Both set **Processed Within** and Search.

- A single previous calendar day uses the **Last Day** option.
- Any other window, including the Monday weekend window, fills **Processed date from** and **Processed date to** and reads them back.
- The Monday flag does not trust Last Day to include Saturday.

## What is downloaded

Actionable rows are saved when a PDF control exists. The file name is
`[Policy Number] [Document Title] Travelers.pdf`.

Example: `UB-8370B557-25 Premium Adjustment Notice Travelers.pdf`.

Insured PDF is preferred. When both Insured and Agent copies exist, only the
Insured PDF is saved, so the row still counts as one PDF. Commercial Lines
may save an agent-only PDF. Personal Lines does not; an agent-only Personal
row holds.

Known actionable titles include renewal, cancel / NOC, reinstate,
nonrenewal, additional info / AI, audit, and premium adjustment. Proposal-New
and other non-action rows are skipped when they have no PDF. A proposal row
with an Insured PDF is saved, because that copy is treated as insured action
required. An unrecognized title holds the list.

## Dashboard alerts

A Dashboard Alert or New Business Action Item with no PDF is a **note**.
The pack records it as `held_no_pdf`. No PDF file is written for it. The
count gate does not treat the note as a missing PDF and does not invent one.

That date's pack status is `HELD` even when the PDF counts match, so the
alert is not a silent success. A dashboard row that really has an Insured
PDF is downloaded as that PDF.

## Monday weekends

`--include-weekends` is valid only when `--as-of` is a Monday
(`America/New_York` when `--as-of` is omitted). It selects the Saturday
through Sunday immediately before that Monday.

Omitted dates on a Monday hold. The pull will not click Last Day and drop
Saturday. Explicit `--start` and `--end` are a historical window of at most
32 inclusive days and do not require the flag.

| Run date | Flag | Dates omitted | Window |
| --- | --- | --- | --- |
| Tuesday 2026-09-29 | no | yes | 2026-09-28 (Last Day) |
| Monday 2026-09-28 | yes | yes | 2026-09-26 through 2026-09-27 |
| Monday 2026-09-28 | no | yes | HELD |
| Any non-Monday | yes | either | HELD |

## Verification gate

For each processed date in the window:

**Actionable rows that have a PDF = PDFs saved for that date.**

Dashboard notes are not on either side of that equality. A mismatch is
`HELD`, not `PULLED`, and not a silent partial. The full-page Policy
Activity Report PNG is captured after Search, before any PDF is opened, and
is written into the date folder:

`policy-activity-report-YYYY-MM-DD.png`

A day in the window with no actionable PDF rows must have no PDFs for that
date (`0 == 0`). An extra PDF for that date fails the same way. Already
written PDFs are left in place.

## Hermes QA pack

```text
/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/travelers-pl/{YYYY-MM-DD}/
/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/travelers-cl/{YYYY-MM-DD}/
```

`--output` defaults to the root for the selected line (the parent of the
date folder). CI and laptops pass an explicit private directory so they do
not create the Hermes path.

Each date folder contains:

1. `policy-activity-report-YYYY-MM-DD.png`
2. That date's PDFs
3. `README.md` — seen, pulled, already present, held notes, skipped, and the hold reason
4. `manifest.json` — carrier `travelers`, `lob`, window, `documents[]`, and `PULLED` or `HELD`

`travelers-activity-ledger.json` stays on the QA root. Hash-named source
copies stay in `{root}/sources`. README and manifest are replaced on each
run. A different existing PDF or PNG is kept.

When the window spans days, the same on-screen PNG is copied into each date
folder. `screenshot_covers_window` says that image is the window list, not a
second query filtered to that day.

A receipt with `"status": "PULLED"` means the local pack passed the count
gate and had no dashboard notes. That is not Job Engine `COMPLETE`.
`"ezlynx": "not_run"` is always set.

## Drive

The documented destination is shared Drive
`Robie Carrier Pull QA (Nicole)/Travelers PL/{YYYY-MM-DD}/` or
`.../Travelers CL/{YYYY-MM-DD}/`.

| | |
| --- | --- |
| Parent `Robie Carrier Pull QA (Nicole)` | `1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2` |
| Travelers PL / CL child folder | **UNVERIFIED** — this slice does not invent an id |

Folder upload is **TODO**. `--upload-drive` is off by default. When it is
set, the local pack is still written, then the command fails closed and
does not call Google or report the pack as uploaded. The manifest `drive`
status becomes `HELD`.

## hermes-test-01 later

Do not run this on `hermes-poc-01`. Do not point `ROBIE_ENV` at Production.
This repository change does not deploy itself. After a normal Test release
of this commit is installed on `hermes-test-01` (not done here):

1. Confirm hostname is `hermes-test-01`.
2. Leave Chrome's CDP on loopback (`http://127.0.0.1:9222` or
   `ROBIE_BROWSER_CDP_URL`). Remote CDP is refused.
3. A person completes Travelers login in that Test browser. The pull does
   not type the password. It holds if the login path or a password field is
   visible.
4. Leave **one** Travelers application tab (`travelers.com` or a
   subdomain, not `/login`). Extra Travelers tabs hold.
5. The default output root is private (mode `0700`), and each date folder
   is too. Do not pass `--upload-drive` until folder upload exists.

Personal, one processed day:

```bash
cd /opt/streetsmart-hermes-test/releases/current
ROBIE_ENV=TEST PYTHONPATH=. python3 -m robie_job_engine.travelers_retrieval \
  --lob pl \
  --as-of 2026-09-29 \
  --start 2026-09-28 \
  --end 2026-09-28
```

Commercial, Monday weekend:

```bash
cd /opt/streetsmart-hermes-test/releases/current
ROBIE_ENV=TEST PYTHONPATH=. python3 -m robie_job_engine.travelers_retrieval \
  --lob cl \
  --as-of 2026-09-28 \
  --include-weekends
```

There is no systemd timer. Do not enable one from this slice.

## UNVERIFIED until that Test run

- Live accessible names: `Personal Insurance`, `Business Insurance`,
  `Featured Reports`, `Agency Reports`, `Policy Activity Report`,
  `Processed Within` / `Last Day`, `Processed date from`, `Processed date to`,
  `Search`, `Insured PDF`, and `Agent PDF`.
- Whether the report grid uses those column titles, and whether a disabled
  `Next` control is present. An enabled `Next` holds so a partial page is
  not treated as the full window.
- The PDF response host. Fetches are limited to `travelers.com` and its
  subdomains, plus `blob:`. Any other host holds.
- Prior delivery into EZLynx. The ledger is only this output directory.
- Test release digest, pointer flip, and rollback target. No release was
  built or installed for this change.
- N=3 clean Test jobs. Not started.
- Drive child-folder ids and folder upload. The parent id is documented.
  `--upload-drive` fails closed and does not call Google.
- A run on `hermes-test-01`. The default paths above are the contract; this
  change has not created them.
