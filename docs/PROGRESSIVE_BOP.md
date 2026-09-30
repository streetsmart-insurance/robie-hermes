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

1. Businessowner/Contractor GL (one new window). When the shell is already
   on FAO Home / Manage Policies Home, Manage Policies is not clicked again.
   The opener is one exact link or button named `Businessowner/Contractor GL`
   or `Go to Businessowner/Contractor GL policy search`. Zero or two matches
   hold. A Communications / underwritinglegacy tab still opens Home first,
   then clicks Manage Policies and then `Businessowner/Contractor GL`.
2. Pending Cancel report. `expect_popup` returns
   `sbr*.foragentsonly.com/.../HPLanding.aspx` (`Close this window`) as soon
   as that window opens. The pull then waits up to 20s for
   `https://bop.americanstrategic.com/` (or that application's frame) and
   does not click report controls on the landing page. It closes the landing
   page when the application is a different page. A failed close does not
   hold. HPLanding alone, after that wait, holds. On the BOP application the
   pull waits until a known control is visible, or until the network is idle.
   It does not sleep a fixed interval, and it does not look before that.
   An older page still uses `View Reports` / `VIEW REPORTS`, then
   `Pending Cancel for Nonpayment`. The reports page seen on hermes-test-01
   on 2026-09-30 has no View Reports control. It has
   `Export Pending Cancel for Non-Payment Pdf` and
   `Export Pending Cancel for Non-Payment Xls`. Those buttons are already
   the report. The click only downloads. It does not bind, cancel, or pay.
3. Full-page PNG of that report, then read policies from the on-screen policy
   table, or from the PDF export, or from the Xls export. The PDF export is
   preferred. A list PDF is read for insured, policy number, and cancel date,
   the same fields the other Wave A pulls keep. Xls is read as xlsx (zip XML,
   no extra package), an HTML table, or CSV. A classic BIFF `.xls` file holds
   with a plain reason. An empty or truncated download holds. It is not an
   empty report and it is not "no docs".
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
5. The shell must show StreetSmart agency `CA33617` and no second agency.
   Live FAO Home may omit the `CA` prefix and show `(33617)`, bare `33617`,
   and/or login id `33617c`; those are the same agent. A missing code, or a
   second `CA#####`, `(#####)`, or `#####c` login, holds the pull. If the
   only FAO tab is not FAO Home / Manage Policies Home — including
   Communications / `underwritinglegacy` — the pull clicks the existing
   header control `a[data-at="header-nav__parent-link--manage-policies"]`
   (accessible name `Manage Policies Home`) before that check. A hidden
   control expands Main Navigation once. That step does not ask Gemini. A
   missing or ambiguous Home control holds and names the scrubbed page URL.
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

CLEAR pull-only re-run on hermes-test-01 (no EZLynx, no `--upload-drive`,
no Production). This module has no `--pull-only` flag because it never
files to EZLynx. Tunnel CDP from a workstation, leave one authenticated FAO
tab, and run on the Test host with the Test virtualenv:

```bash
gcloud compute ssh hermes-test-01 -N -L 9222:127.0.0.1:9222
```

```bash
cd /opt/streetsmart-hermes-test/releases/current
ROBIE_ENV=TEST PYTHONPATH=. /opt/streetsmart-hermes-test/venv/bin/python \
  -m robie_job_engine.progressive_bop \
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

- Agent context on a non-home tab. The pull treats `/`, `/home`,
  `/managepolicies` (optional `/home`), and `/landingpages/managepolicies`
  (optional `/home`, optional trailing slash) on `foragentsonly.com` as FAO
  Home / Manage Policies Home and does not click. Any other FAO URL, including
  `processeddateresults/underwritinglegacy/` and Communications, must open
  that landing through the existing Manage Policies Home header control
  before the agent check. A click that lands on `/landingpages/managepolicies/`
  is Home. A click that stays off those paths holds. This step does not ask
  Gemini. When the tab was already on that Home, the next step skips
  `Manage Policies` and opens one exact `Businessowner/Contractor GL` or
  `Go to Businessowner/Contractor GL policy search` control in a new window.
  Zero or several of those names hold. A tab that was not already on Home
  still clicks `Manage Policies`, then exact `Businessowner/Contractor GL`,
  after Home opens. That opener does not ask Gemini.
- Live accessible names: `Manage Policies`, `Businessowner/Contractor GL`,
  `Go to Businessowner/Contractor GL policy search` (shell Home only, one
  exact link or button), `View Reports`, `Pending Cancel for Nonpayment`,
  `Export Pending Cancel for Non-Payment Pdf`,
  `Export Pending Cancel for Non-Payment Xls`,
  the shell policy search, `Documents`, and the `Policy` document tab. A
  mismatch holds; do not widen these from Production.
- Whether Businessowner/Contractor GL opens exactly one new window.
- Whether the reports page is ready because the export button is visible, or
  only after the network is idle. This change has not been run on
  `hermes-test-01`. Do not deploy it while that host is in use.
- Whether the PDF export is a policy list (insured, policy number, cancel
  date) or a packet of per-policy notices. A list is parsed. The per-policy
  Notice of Non Payment is still downloaded from FAO Documents. A cancel date
  that is not the requested report date holds. Confirm that against a live
  non-empty file.
- Whether a finished export is still 0 bytes after the page is ready. That
  result must stay a hold that says the file is empty. It must not become an
  empty pack.
- Whether the Xls export, once non-empty, is xlsx, an HTML table, CSV, or
  classic BIFF `.xls`. xlsx, HTML, and CSV are read with the standard
  library. BIFF holds until a real file shows that format. No Excel package
  was added.
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
