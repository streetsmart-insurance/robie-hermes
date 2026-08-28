---
name: "ascend-locator-artifact-audit"
description: "Test-only Job Engine audit: walk the Ascend new-program flow with Playwright strict mode and save/look up a quote PDF under the real job id. Stop before Save program. Never bind, email, or use a real client."
job_type: "ascend.locator_artifact_audit"
production_ready: false
---

# Ascend locator + artifact audit (Test only)

This is a **full Job Engine job**, not a UI click-through script. It is the
required Test gate before a **NEW** site or workflow (Ascend, next carrier
portal, anything that is not already live) may run on a real Production
account.

Commercial auto already on Production is **not** a free pass. N clean Test
jobs still applies (Carlo: N=3). This skill stays `production_ready: false`.
This PR must not flip Production.

This skill is **not** Loom `ascend-finance`. Do not overwrite that skill.

## Hard stops

- Test account only. Never PAWIVA / 221398001 / a real client.
- Stop **before** Save program, Send email, Copy checkout, payment, bind.
- Playwright strict mode: every element needs a unique locator. A
  non-unique locator is **FAIL**. No Gemini. No `.first` / `.nth` / `.last`.
- Generate / save / look up a quote PDF the same way a live Chat job does
  (artifacts under the real job id). Fail if the worker cannot open the PDF
  it just saved (concatenated / mangled job-id folder).
- This job ends as a **punch-list report**, not COMPLETE of a finance
  agreement. COMPLETE is never allowed without destination evidence.

## Expected flow

1. Open `https://dashboard.useascend.com/programs`
2. + New program (first HITL we hit live)
3. Commercial customer radio
4. Import document / upload (not Hawksoft / AMS360 / Epic)
5. Insured fields, address autocomplete (must pick the exact row)
6. Quote number, carrier, wholesaler, coverage type
7. Dates, premium, taxes, Agency Fee field
8. Stop before Save program
9. Save and reopen the quote PDF under `{artifact_root}/{job_id}/`

## Where it runs

- **GitHub CI:** punch-list reporter + artifact-path assertions only. No
  live Ascend. No EZLynx.
- **hermes-test-01:** `ROBIE_ENV=TEST` and
  `python -m robie_job_engine.ascend_locator_audit --live --db … --artifact-root …`
- **hermes-poc-01:** never. Production hold stays. Jake Approves, Carlo
  Confirms later. Hermes stays on.

## Punch list

Each step is PASS or FAIL with the locator or artifact path and the exact
error (strict mode violation, TimeoutError, missing artifact dir, concatenated
job-id folder, missing PDF after save).
