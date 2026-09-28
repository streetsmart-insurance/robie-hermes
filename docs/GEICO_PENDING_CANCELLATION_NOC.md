# Geico Gateway Pending Cancellation NOC pull

Test-only list and download for Geico Agent Gateway **Client Alerts →
Pending Cancellations**. Process name is `geico`. There is no earlier
`geico_*` process in this repo; this module is the sibling of
`progressive_fao_memo`, not a second Geico process.

Pull-first only. This slice does not upload to EZLynx, file notes, create
tasks, apply labels, write a Status Sheet, deploy, or enable a timer.

Manual prove (Dusty box browser, 2026-09-26, not re-run by this code):
Geico For Your Agency B2C login at `geicoextendprod.b2clogin.com`, then
Gateway `https://gateway2.geico.com/client-alerts`, Client Alerts →
**Pending Cancellations**. Personal auto policy view: Documents → Billing →
Pending Cancellation Notice / CANCELLATION NOTICE. Files were named
`[PolicyNumber] NOC Geico.pdf`.

That list had three High alerts. The code does not special-case these
policy numbers; they are the acceptance example a fixture test replays
with synthetic PDF bytes. The customer PDFs are not in git.

| Policy | Insured | Due | Product | Result |
| --- | --- | --- | --- | --- |
| 9300116248 | BYOND TRANSPORTATION LLC | 2026-09-21 | Commercial Auto | HELD — Documents path missing; Billing only |
| 6253395526 | Charlemagne Guevara | 2026-10-06 | Private Passenger Auto | `6253395526 NOC Geico.pdf` |
| 6260043796 | TIMOTHY PANELLA | 2026-09-26 | Private Passenger Auto | `6260043796 NOC Geico.pdf` |

## What runs

`python -m robie_job_engine.geico_pending_cancellation_noc` requires
`ROBIE_ENV=TEST` before it attaches or downloads. Production and an unset
env hold. Hostname `hermes-poc-01` holds even if `ROBIE_ENV` is mis-set.

The pull attaches to an already-open Gateway tab, selects Pending
Cancellations, and reads visible High rows (policy number, insured, due
date, severity as status, product). It does not type a password or a
one-time passcode. A login host or a password field holds.

On the current Gateway UI the Pending Cancellations control is a filter
chip, often named `Pending Cancellations (3)`. A selected chip
(`aria-pressed`, `aria-selected`, or `aria-checked`) is the view. A chip
with no toggle attribute is the view when the alerts table is already the
only table. Client Alerts is not required on that path. The older Client
Alerts link, then the Pending Cancellations option, remains the path when
no chip is present.

Personal lines (`Private Passenger Auto`) are targeted only when Documents
→ Billing → Pending Cancellation Notice (or CANCELLATION NOTICE) is a
single control. The PDF is saved as `[PolicyNumber] NOC Geico.pdf`.

Commercial Auto with Billing only and no Documents control is **HELD** on
that row. The reason is explicit. That row is not counted as a download
and is not reported as PULLED.

A receipt with `"status": "PULLED"` means the personal-lines gate below
passed and files were saved locally. That is not Job Engine `COMPLETE`.
`"ezlynx": "not_run"` is always set. Commercial holds stay in `"held"`
even when the personal-lines gate passes.

## Verification gate

For the set targeted as downloadable personal-lines on this pull:

**Targeted personal-lines alerts = PDFs saved for those alerts.**

The Pending Cancellations list PNG is captured while that view is selected,
before any policy is opened:

`geico-pending-cancellations-YYYY-MM-DD.png`

A matched pull writes that PNG into the dated QA pack. A hold after the
list is visible still writes the PNG, the README, and the manifest with
status `HELD`. A hold before the list is on screen does not invent a
screenshot.

(`--as-of` is that date.) A mismatch holds the pull (`HELD`). The receipt
is not `PULLED`. Already written PDFs are left in place for recovery; they
are not deleted and they are not a successful partial. Commercial holds
are listed separately and do not satisfy the PDF count.

A later pull that captures different screenshot bytes does not replace the
first PNG; it adds a sibling file. A private ledger skips a later pull of
the same personal-lines bytes and holds if a local file disagrees.

There is no systemd timer. Do not enable one from this slice. Production
is not a target.

The browser steps use exact accessible names from the manual path. Zero or
multiple matches hold. There is no positional click. A notice open accepts
one PDF from a download event, a new tab (`blob:`, `geico.com` PDF URL, or
embed), or a same-tab PDF that can be returned to the list. HTML is not
printed into a fake PDF. Two different PDFs hold. Login hosts are not a
PDF source.

## hermes-test-01 later

Do not run this on `hermes-poc-01`. Do not point `ROBIE_ENV` at Production.
This repository change does not deploy itself. After a normal Test release
of this commit is installed on `hermes-test-01` (not done here):

1. Confirm hostname is `hermes-test-01`. The command holds on
   `hermes-poc-01`.
2. Leave Chrome's CDP on loopback (`http://127.0.0.1:9222` or
   `ROBIE_BROWSER_CDP_URL`). Remote CDP is refused.
3. A person completes Gateway login and the one-time passcode in that Test
   browser and leaves the session open. The pull does not type the password
   or the passcode. It holds if the B2C login host or a password field is
   visible.
4. Leave **one** `gateway2.geico.com` application tab. Extra Gateway tabs
   hold.
5. The dated pack directory must be private (mode `0700`). The command
   creates it and holds if it is group- or world-accessible. Existing named
   PDFs that do not match the ledger are left in place and the pull holds.
6. Do not pass `--upload-drive`. Folder upload is not implemented. The flag
   writes the local pack, then exits `HELD` and does not call Google.

CLEAR pull-only re-run on hermes-test-01. This command does not file to
EZLynx. Do not pass `--upload-drive`. Tunnel CDP, leave the authenticated
`gateway2.geico.com/client-alerts` tab, and run:

```bash
gcloud compute ssh hermes-test-01 -N -L 9222:127.0.0.1:9222
```

```bash
cd /opt/streetsmart-hermes-test/releases/current
ROBIE_ENV=TEST PYTHONPATH=. /opt/streetsmart-hermes-test/venv/bin/python \
  -m robie_job_engine.geico_pending_cancellation_noc \
  --as-of 2026-09-26
```

`--output-root` defaults to
`/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/geico`.
The command writes one date folder:

```text
/opt/streetsmart-hermes-test/robie-job-engine/data/artifacts/carrier-pull-qa/geico/2026-09-26/
```

That folder contains:

1. `geico-pending-cancellations-2026-09-26.png`
2. Each saved `[PolicyNumber] NOC Geico.pdf`
3. `README.md` — what Robie saw, downloaded, and held
4. `manifest.json` — `carrier` `geico`, `status` `PULLED` or `HELD`, `rows`,
   `downloaded`, `held`, screenshot path, `ezlynx` `not_run`

After `PULLED`, the date folder must contain the PNG and the same number
of NOC PDFs as personal-lines alerts targeted on that page. Commercial rows
appear in the receipt `"held"` array and in the README. A count mismatch is
a failed pull, not a partial success. The ledger file stays in the same
date folder and is not part of the Nicole-facing README.

## Drive

Documented destination: `Robie Carrier Pull QA (Nicole)/Geico/{YYYY-MM-DD}/`.

| | |
| --- | --- |
| Parent | `1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2` |
| Geico child | `1mMy9nrYjN8PRRwihRLgjjDdb213WqBLt` |

`--upload-drive` is off by default. When set, the local pack is written,
then the command fails closed. It does not call Google and it does not
report the pack as uploaded. The local date folder is left in place.

## UNVERIFIED until that Test run

- Live accessible names: `Client Alerts`, combobox or option
  `Pending Cancellations`, policy-number link, `Documents`, `Billing`,
  `Pending Cancellation Notice`, and `CANCELLATION NOTICE`. A mismatch
  holds; do not widen these from Production.
- Whether the grid uses a Severity column whose visible value is `High`,
  and whether a disabled `Next` control is present. An enabled `Next`
  holds so a partial page is not treated as the full list. A non-High row
  holds the list.
- Product labels other than `Private Passenger Auto` and `Commercial Auto`.
  Anything else holds. Policy numbers other than 10 digits hold.
- The PDF response host. Fetches are limited to `geico.com` and its
  subdomains, plus `blob:`. A login host or a CDN host holds.
- Prior delivery into EZLynx. The ledger is only this output directory.
- Drive folder upload. The parent and Geico folder ids are documented.
  `--upload-drive` fails closed and does not call Google.
- Test release digest, pointer flip, and rollback target. No release was
  built or installed for this change.
- N=3 clean Test jobs. Not started.
