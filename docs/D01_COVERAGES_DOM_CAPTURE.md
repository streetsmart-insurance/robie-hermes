# D01 Coverages FormEntry DOM capture — status 2026-09-16

## Goal

One offline reference dump of the **Coverages** FormEntry screen for
`TEST-HO-20260912-D01` (policyId `83651751`, applicant `220250093`): every
input/select/textarea with label, type, id/name, and **all** `<option>`
values. Pattern: `.github/workflows/capture-formentry-dom.yml` +
`scripts/capture_formentry_dom.py` (extended for Coverages mode).

## Checked before minting (do not mint yet)

| Check | Result |
|-------|--------|
| Prior mint `34697459460` | `ok: false` — stayed on Edit URL after Save & Continue Edit |
| Prior mint-retry `34698224965` | `ok: false` — no FormEntry URL on any tab |
| CDP on `hermes-poc-01` (2026-09-16) | No EZLynx tab — only `about:blank` |
| `ezlynx_session` | `INTERACTIVE_AUTH_REQUIRED` |

**Conclusion:** D01 is **not** sitting on FormEntry Coverages. There is no
literal `formEntryId` to navigate. Coverages capture is blocked until Carlo
(1) restores an authenticated Prod CDP session and (2) either confirms an
existing FormEntry URL or runs the separate one-write workflow
`MINT_FORMENTRY_D01` (not this read-only capture).

## How to run once FormEntry exists

```text
Workflow: Capture FormEntry DOM (read-only CDP)
confirmation: CAPTURE_FORMENTRY_COVERAGES_DOM
formentry_url: https://app.ezlynx.com/applicantportal/Policy/83651751/FormEntry/Index/<literal-id>
```

Script flags: `--url <formentry_url> --prefer-tab Coverages`.  
Select dumps include **all** options (no 40-cap). Still read-only: no fill /
type / check / select_option / Save.

Edit-header-only (old path): `confirmation=CAPTURE_FORMENTRY_DOM`.
