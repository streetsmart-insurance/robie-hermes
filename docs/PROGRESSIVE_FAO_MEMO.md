# Progressive FAO Communications memo pull

Test-only list and download for For Agents Only **Communications → Memo**
rows. Process name stays `progressive`. Scope stays `fao_communications`.
This slice does not upload to EZLynx, file notes, create tasks, apply labels,
mark carrier rows processed, deploy, or enable a timer.

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
already-open FAO tab and writes named PDFs plus `fao-memo-ledger.json`.
A receipt with `"status": "PULLED"` means files were saved locally and the
verification gate below passed. That is not Job Engine `COMPLETE`.
`"ezlynx": "not_run"` is always set.

## Verification gate

Robie's gate for a pull, checked separately for **each processed date** in
the requested window:

**Memo rows on the Communications list for that date = PDFs saved for that date.**

The list screenshot is taken while the Communications tab is selected,
before any Memo is opened, and it is written only after the counts match.
A mismatch holds the pull (`HELD`). The receipt is not `PULLED`. Already
written PDFs are left in place for recovery; they are not deleted and they
are not treated as a successful partial. An extra PDF for that same
processed date fails the same way. A day in the window with no Memo rows
must have no PDFs for that date (`0 == 0`).

Daily QA for Nicole should use the same start and end date. The screenshot
is then one full-page PNG of that day's Communications Memo list:

`fao-communications-memo-YYYY-MM-DD.png`

in the pull output directory (mode `0600`), next to the PDFs. A multi-day
window still counts each date on its own and saves one PNG of the list that
was actually on screen, named with the window. A later pull that captures
different bytes does not replace the first PNG; it adds a sibling file.

There is no systemd timer. Do not enable one from this slice. Production
is not a target.

The browser steps use exact accessible names from the manual path. Zero or
multiple matches hold. There is no positional click. A Memo open accepts one
PDF from a download event, a new tab (blob, Progressive/foragentsonly PDF
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
5. The shell must show agent code `CA33617` and no second `CA#####` code.
   Override only with `--agent-code` / `PROGRESSIVE_FAO_AGENT_CODE` when the
   Test session is actually that code.
6. Use a private output directory (mode `0700`). The command creates it and
   holds if it is group- or world-accessible. Existing named PDFs that do
   not match the ledger are left in place and the pull holds.

```bash
cd /opt/streetsmart-hermes-test/releases/current
ROBIE_ENV=TEST PYTHONPATH=. python3 -m robie_job_engine.progressive_fao_memo \
  --start 2026-09-25 \
  --end 2026-09-25 \
  --output /opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/fao-memo-pull
```

Use an explicit window that covers the business days you mean, including
weekends when those days should be included. The window cannot exceed 32
inclusive days. For one processed date, `--start` and `--end` are that
date. After `PULLED`, the output folder must contain the PNG and the same
number of memo PDFs as Memo rows on that page. A count mismatch is a
failed pull, not a partial success.

## UNVERIFIED until that Test run

- Live accessible names: `Manage Policies`, `Policy Activity`,
  `Processed date from`, `Processed date to`, `Search`, Communications tab
  `aria-selected`, and a Memo link or button on each row. A mismatch holds;
  do not widen these from Production.
- Whether the communications grid uses a Type column, and whether a disabled
  `Next` control is present. An enabled `Next` holds so a partial page is
  not treated as the full day.
- The PDF response host. Fetches are limited to `foragentsonly.com` and
  `progressive.com` (and their subdomains), plus `blob:`. A CDN host holds.
- Prior delivery into EZLynx. The ledger is only this output directory.
- Test release digest, pointer flip, and rollback target. No release was
  built or installed for this change.
- N=3 clean Test jobs. Not started.
