---
name: "ascend-locator-artifact-audit"
description: "Test-only Job Engine audit: walk the Ascend new-program flow with Playwright strict mode and save/look up a quote PDF under the real job id. Stop before Save program. Never bind, email, or use a real client."
job_type: "ascend.locator_artifact_audit"
production_ready: false
---

# Ascend locator + artifact audit (Test only)

This is a **full Job Engine job**, not a UI click-through script. It is the
`hermes-test-01` Test job (step 3) in the written NEW-site gate. It is
**not** the automatic simulator.

Written gate (Carlo 2026-08-28) — ChatGPT / Claude / Jake-paste must
follow this. Do not skip the battery or the Dusty walk because a model
is “just trying it.”

1. **PR 35 battery (CI on the PR)** is the automatic simulator. Must be
   green before merge when it is a NEW failure mode or NEW site/workflow.
2. **Dusty walks the live site himself** before a NEW website/portal is
   tried on a real account (same as the 2026-08-28 Ascend look).
3. **This Test job on `hermes-test-01`.** No new live Ascend / PAWIVA
   Production job until **N=1** clean Test pass (Carlo asked for at
   least one). New job-type / `production_ready` flip still needs N=3.
   Then Production zip. Follow-tab is separately proven.
4. **After Production zip, run the PR 35 battery again.** Pointer-only
   is not live.
5. Do not skip the battery or the Dusty walk because a model is “just
   trying it.”

PR 36 is one-load-path only — do not call 36 the simulator.

Commercial auto already on Production is **not** a free pass. N clean Test
jobs still applies (Carlo: N=3). This skill stays `production_ready: false`.
This PR must not flip Production.

This skill is **not** Loom `ascend-finance`. Do not overwrite that skill.

## Hard stops

- Test account only: Robie username + email 2SV. Never PAWIVA /
  221398001 / a real client.
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
2. Wait out the programs spinner (~12s was seen live; ~20s is still
   normal). **Log the seconds** until the unique primary `+ New program`
   is visible and enabled AND the programs table or KPI cards are
   present. Missing seconds is FAIL. Never the split-menu caret. If the
   spinner or button is not ready past timeout: `PLAYWRIGHT_BLOCKED` then
   HITL. No Gemini.
3. Click the unique primary `+ New program` only
   (`get_by_role("button", name="+ New program", exact=True)`). Then
   `wait_for_url /create/new`. That is not follow-tab proof.
4. Create a program (`/create/new`): **log** Import document vs Upload
   document vs dropzone labels. Prefer Import document (that panel has
   no Hawksoft / AMS360 / Epic). Unclear → dry HITL.
5. **Log** the Producer and Account Manager prefills (live default is
   `Robie AI`). Overwrite both with the agent who SENT the job
   (`payload.requested_by` — Google Chat `user_name` / `user_id`).
   Jake → Jake Ferrara. Carlo → Carlo Ferrara. Leave them only if they
   already equal that name. **FAIL** if they stay Robie AI when
   requested_by is Carlo Ferrara or Jake Ferrara. Unknown sender → HITL
   in dry English. Unique locators. No `.first` / `.nth` / `.last`.
6. Commercial vs Personal radio from **line of business**, not the form
   default and not LLC vs person-name. Commercial auto / commercial
   package / BOP / CGL / workers comp / trucking / garage → Commercial
   customer. Homeowners / personal auto / renters / personal umbrella /
   dwelling fire → Personal customer. Missing or unclear LOB → HITL in
   dry English. Unique radio locator. No `.first` / `.nth` / `.last`.
7. Customer Name (Test account only), address autocomplete (exact row)
8. Quote number, carrier, wholesaler, coverage type
9. Dates, premium, taxes. **Log** the Agency Fee default (expect $0.00
   / empty). If the fee field exists, **set 500** in this Test run.
10. Stop before Save program / Send email / Copy checkout / payment / bind
    (same PR 37 stop; Loom `ascend-finance` is not overwritten).
11. Save and reopen the quote PDF under `{artifact_root}/{job_id}/`

## Where it runs

- **GitHub CI:** punch-list reporter + artifact-path assertions only. No
  live Ascend. No EZLynx.
- **hermes-test-01:** `ROBIE_ENV=TEST` and
  `python -m robie_job_engine.ascend_locator_audit --live --db … --artifact-root …`
- **hermes-poc-01:** never. Production hold stays. Jake Approves, Carlo
  Confirms later. Hermes stays on.

## Punch list

Each step is PASS or FAIL with the locator or artifact path, the logged
default / seconds / labels when the step has them, and the exact error
(strict mode violation, TimeoutError, missing artifact dir, concatenated
job-id folder, missing PDF after save, missing spinner seconds, unlogged
role or Agency Fee default).
