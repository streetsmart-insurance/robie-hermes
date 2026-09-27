# Progressive BOP pending-cancel nonpayment pull

Test-only list and download for For Agents Only **Businessowner/Contractor GL →
View Reports → Pending Cancel for Nonpayment**. Process name stays
`progressive`. Scope stays `bop_pending_cancel_nonpayment`.

This slice does not upload to EZLynx, file notes, create tasks, apply labels,
mark carrier rows processed, deploy, or enable a timer. It does not type the
FAO password or SMS OTP.

The playbook path is Nicole's document-retrieval steps. Live accessible names,
the report grid, and the policy document table are **UNVERIFIED** until a
`hermes-test-01` run. A missing or non-unique control holds. There is no
positional click.

## What runs

`python -m robie_job_engine.progressive_bop` attaches to one already-open FAO
tab and writes one QA pack for `--report-date`:

1. Manage Policies → Businessowner/Contractor GL (one new window).
2. View Reports → Pending Cancel for Nonpayment.
3. Full-page PNG of that report, then read policies from the on-screen policy
   table, or from one Excel export, or from one PDF export.
4. For each policy, on the original FAO shell: search the policy → Documents →
   Policy → download the Notice of Non Payment whose date is the report date.
5. Save `[Policy Number] - NOC - Non Payment.pdf`.

Other documents are skipped. A notice with no date, two dates, or two matching
notices holds that row. A listed policy with no matching notice is **HELD**
for that row and the pull continues. A blank report (empty policy table, empty
Excel with a policy header and no rows, or a visible no-records phrase and no
policy number) keeps the screenshot and writes an empty pack.

A receipt with `"status": "PULLED"` or `"status": "EMPTY"` means the local
pack passed the gate below. That is not Job Engine `COMPLETE`.
`"ezlynx": "not_run"` is always set.

## Hermes QA pack

`--output-root` defaults to the Progressive BOP QA root. CI and laptops pass
an explicit private directory so they do not create the Hermes path.

```text
/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/progressive-bop/{YYYY-MM-DD}/
```

That date folder contains:

1. `pending-cancel-nonpayment-YYYY-MM-DD.png` — full-page Pending Cancel report
2. NOC PDFs named `[Policy Number] - NOC - Non Payment.pdf`
3. `README.md` — policies seen, pulled this run, already present, and held
4. `manifest.json` — carrier `progressive`, product `bop`, counts, and `PULLED`, `EMPTY`, or `HELD`

`bop-noc-ledger.json` stays on the QA root (machine state for replay).
Hash-named source copies stay in `{root}/sources`. Neither file is the Nicole
pack. README and manifest are replaced on each run. A different existing PDF
or PNG is kept.

## Verification gate

Checked for the requested report date:

**Policies on the report with a successful NOC = PDFs saved for that date.**

The screenshot is taken on the Pending Cancel report before any policy is
searched. It must be a PNG. A missing or non-PNG screenshot fails closed.

| Report | Pack | Command |
| --- | --- | --- |
| No policies, screenshot, 0 PDFs | `EMPTY` | exit 0 |
| Every listed policy has one saved NOC and the counts match | `PULLED` | exit 0 |
| Any listed policy is missing its NOC, a row is ambiguous, or the counts differ | `HELD` | exit 2 |

HELD still writes the screenshot, any PDFs already saved, README, and
manifest. Those files are not a successful partial. An extra PDF for that
date fails the same way. A later pull that captures different screenshot
bytes does not replace the first PNG; it adds a sibling file.

There is no systemd timer. Do not enable one from this slice. Production is
not a target. `ROBIE_ENV=TEST` is required before attach or download.
Production and an unset env hold.

## Drive

The documented destination for a day's folder is shared Drive
`Robie Carrier Pull QA (Nicole)/Progressive BOP/{YYYY-MM-DD}/`.

| | |
| --- | --- |
| Parent `Robie Carrier Pull QA (Nicole)` | `1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2` |
| Child folder | `Progressive BOP` (create if it is missing) |

The child folder id is not known in this slice. Folder upload is **TODO**.
`GoogleDriveUploader` uploads one `video/webm` recording into a known folder.
It is not used here and it does not create `Progressive BOP`. `--upload-drive`
is off by default. When it is set, the local pack is still written, then the
command fails closed and does not call Google or report the pack as uploaded.
The manifest `drive` status becomes `HELD`.

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
   The GL window is opened by the pull.
5. The shell must show agent code `CA33617` and no second `CA#####` code.
   Override only with `--agent-code` / `PROGRESSIVE_FAO_AGENT_CODE` when the
   Test session is actually that code.
6. The output root and the date folder must be private (mode `0700`). The
   command creates them and holds if either is group- or world-accessible.
   Do not pass `--upload-drive` until folder upload exists; it fails closed.

```bash
cd /opt/streetsmart-hermes-test/releases/current
ROBIE_ENV=TEST PYTHONPATH=. python3 -m robie_job_engine.progressive_bop \
  --report-date 2026-09-26
```

That writes
`/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/progressive-bop/2026-09-26/`.
After `PULLED`, that folder must contain the PNG, README, manifest, and the
same number of NOC PDFs as policies on the report. `EMPTY` is a blank report:
PNG, README, manifest, and zero PDFs. A missing NOC writes a `HELD` pack and
is a failed pull, not a partial success. This code has not been run on
`hermes-test-01`.

## UNVERIFIED until that Test run

- Live accessible names: `Manage Policies`, `Businessowner/Contractor GL`,
  `View Reports`, `Pending Cancel for Nonpayment`, the shell policy search,
  `Documents`, and the `Policy` document tab. A mismatch holds; do not widen
  these from Production.
- Whether Businessowner/Contractor GL opens exactly one new window.
- Whether the report is an HTML policy table, an Excel control, or a PDF
  control. An empty scan with no policy table, no export, and no no-records
  phrase holds, so a blank pack is not invented from a failed parse.
- The policy-document table headers and the Notice of Non Payment control.
- The PDF response host. Fetches are limited to `foragentsonly.com` and
  `progressive.com` (and their subdomains), plus `blob:`.
- Prior delivery into EZLynx. The ledger is only this output directory.
- Test release digest, pointer flip, and rollback target. No release was
  built or installed for this change.
- N=3 clean Test jobs. Not started.
- Drive folder create and upload. The Nicole parent id and the child name
  `Progressive BOP` are documented only. `--upload-drive` fails closed and
  does not call Google.
- A run on `hermes-test-01`. The default path above is the contract; it has
  not been created by this change.
