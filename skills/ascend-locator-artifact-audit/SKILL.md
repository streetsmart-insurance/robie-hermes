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
3. **This Test job on `hermes-test-01` is the Test gate.** A visual walk
   on Dusty's computer is not the Test gate. PR 35 CI is not the Test
   gate. Shipping a zip to `hermes-poc-01` is not the Test gate.
   Production is not the first test. Jobs `807f8920` and `38c0fa79`
   (2026-08-28) skipped this gate on a live client; the safety net
   caught them. No new live Ascend / PAWIVA Production job until
   **N=1** clean Test pass (Carlo asked for at least one). New
   job-type / `production_ready` flip still needs N=3. Follow-tab is
   separately proven.
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
   normal). **Log the seconds** until the unique primary `New program`
   is visible and enabled AND the programs table or KPI cards are
   present. Missing seconds is FAIL. The plus is an icon/SVG, not text.
   Accessible name is exactly `New program`. Never the split-menu caret
   (`Open split button menu`). If the spinner or button is not ready past
   timeout: `PLAYWRIGHT_BLOCKED` then HITL. No Gemini.
3. Click the unique primary `New program` only
   (`get_by_role("button", name="New program", exact=True)`). Then
   `wait_for_url /create/new`. That is not follow-tab proof. The create
   form is **not instant** after that URL (same class as the programs
   spinner).
4. Create a program (`/create/new`): **wait** until the unique primary
   `Import document` is visible and enabled
   (`get_by_role("button", name="Import document", exact=True)`).
   **Log the seconds.** A too-soon 0-element lookup is FAIL. Then
   **log** Import document vs Upload document vs dropzone labels.
   Prefer Import document (that panel has no Hawksoft / AMS360 / Epic).
   Unclear → dry HITL. If the button is not ready past timeout:
   `PLAYWRIGHT_BLOCKED` then HITL. No Gemini.
5. **Log** the Producer and Account Manager prefills (live default is
   `Robie AI`). LOGIN is Robie (browser session only) — do **not** put
   Robie AI on Producer or Account Manager. Overwrite both with the
   agent who SENT the job (`payload.requested_by` — Google Chat
   `user_name` / `user_id`). The unique option is the concatenated
   Name+email label, **not** the display name alone. Jake →
   `Jake Ferrara jake@streetsmart.insurance`. Carlo →
   `Carlo Ferrara carlo@streetsmart.insurance` (not `carlo@ssinj.com`,
   not Robie AI). Name-only `Carlo Ferrara` is **FAIL** — two Carlo
   rows exist. If the sender email is missing from the list, HITL/FAIL
   that field; do not fall back to Robie AI or the other Carlo. Leave
   them only if they already equal that unique option. **FAIL** if they
   stay Robie AI when requested_by is Carlo or Jake. Unknown sender →
   HITL in dry English. Unique locators. No `.first` / `.nth` / `.last`.
6. Open each create-form combobox the job would use (Producer, Account
   Manager, Carrier / Writing company, Coverage type, State, etc.).
   The visible list must have a **unique** locator for the intended
   option (`get_by_role("option", name=…, exact=True)`). For Producer
   and Account Manager that name is the concatenated Name+email label
   (`Carlo Ferrara carlo@streetsmart.insurance`), not `Carlo Ferrara`.
   Two options matching the same selector is `PLAYWRIGHT_BLOCKED` (job
   `38c0fa79`). Log the blocked field. Dry HITL if the intended option
   is missing. Carlo will not RETRY `38c0fa79`. No `.first` / `.nth` /
   `.last`.
7. Commercial vs Personal radio from **line of business**, not the form
   default and not LLC vs person-name. Commercial auto / commercial
   package / BOP / CGL / workers comp / trucking / garage → Commercial
   customer. Homeowners / personal auto / renters / personal umbrella /
   dwelling fire → Personal customer. Missing or unclear LOB → HITL in
   dry English. Unique radio locator. No `.first` / `.nth` / `.last`.
8. Customer Name (Test account only), address autocomplete (exact row)
9. Quote number, carrier, wholesaler, coverage type (unique listbox
   option — see step 6)
10. Dates, premium, taxes. **Log** the Agency Fee default (expect $0.00
   / empty). If the fee field exists, **set 500** in this Test run.
11. Stop before Save program / Send email / Copy checkout / payment / bind
    (same PR 37 stop; Loom `ascend-finance` is not overwritten).
12. Save and reopen the quote PDF under `{artifact_root}/{job_id}/`

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
job-id folder, missing PDF after save, missing spinner seconds, missing
create-form seconds after /create/new, unlogged role or Agency Fee default).
