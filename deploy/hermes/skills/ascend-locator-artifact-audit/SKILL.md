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
  it just saved (concatenated / mangled job-id folder). Production
  `eb96f620` looked in `{job_id[:-11]}{artifact_id}/`. Lookup must be
  exactly `{artifact_root}/{full_job_id}/`.
- HITL Chat posts are dry/technical only (`PLAYWRIGHT_BLOCKED` + path +
  ask). Cowboy slang, blame, and nickname voice are rewritten before send.
- This job ends as a **punch-list report**, not COMPLETE of a finance
  agreement. COMPLETE is never allowed without destination evidence.

## Expected flow

1. Open `https://dashboard.useascend.com/programs`
2. Wait out the programs spinner (~20s is normal). Do not click until the
   unique primary `+ New program` is visible and enabled AND the programs
   table or KPI cards are present. Never the split-menu caret. If the
   spinner or button is not ready past timeout: `PLAYWRIGHT_BLOCKED` then
   HITL. No Gemini.
3. Click the unique primary `+ New program` only
   (`get_by_role("button", name="+ New program", exact=True)`).
4. Create a program (`/create/new`): **Import document** only (that panel
   has no Hawksoft / AMS360 / Epic).
5. Producer and Account Manager prefill `Robie AI`. Overwrite both with
   the agent who SENT the job (`payload.requested_by` — Google Chat
   `user_name` / `user_id`). Jake → Jake Ferrara. Carlo → Carlo Ferrara.
   Leave them only if they already equal that name. Unknown sender → HITL
   in dry English. Never leave Robie AI / SSRobie when requested_by is
   known. Unique locators. No `.first` / `.nth` / `.last`.
6. Commercial customer radio is already selected by default.
7. Customer Name (Test account only), address autocomplete (exact row)
8. Quote number, carrier, wholesaler, coverage type
9. Dates, premium, taxes, Agency Fee field
10. Stop before Save program / Send email / Copy checkout / payment / bind
11. Save and reopen the quote PDF under `{artifact_root}/{job_id}/`

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
