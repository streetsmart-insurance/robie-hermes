# 4372 mortgagee enrichment (API first → Additional Interests table)

For Carlo and Ralph. This is a **read-only / Test-prove** scaffold, not a
Production path. No portal automation and no Bland dials ship in this PR.
**No merge until live-proven.** No Production zip.

## Pivot (Carlo 2026-09-19, definitive)

PolicyApi **cannot** see Additional Interests / mortgagee / loan.
Policy-by-ID detail endpoints do not exist. The API-only enrichment path
is dead for lender + loan.

Control evidence: policy **SAHO581361** — PolicyApi search works and
returns **no mortgagee fields**. The Additional Interests UI on other
policies shows Wells Fargo / loan-style rows. That table is the sanctioned
source.

New path (API-first, then browser fallback):

1. Still try DocumentApi + PolicyApi structured fields first.
2. If API returns incomplete / no mortgagee fields, open the policy
   **Additional Interests** tab and read the **structured table DOM**
   (lender name, loan number, type).
3. This is **not** a PDF scrape and **not** OCR.

The browser port reuses in-tree Hermes / EZLynx infra
(`CdpReadPort` CDP attach, unique `get_by_role` tab click like FormEntry
coverages, locator registry). It does **not** invent a new browser stack
and does **not** invent policy-search navigation. SSRobie Chrome must
already be on the policy FormEntry page.

## Where it plugs in

Open 4372 work items (closed tasks already excluded) go through
`robie_job_engine/mortgagee_enrichment.py` before `plan_4372`:

```
Gmail CSV 4372
  → build_work_items
  → drop Closed tasks (explicit reason, never silent)
  → enrich_work_item
        1. DocumentApi search + PolicyApi structured fields
        2. if incomplete / no mortgagee fields:
             Additional Interests tab table (read-only CDP)
  → if ready: verify_lender per MortgageRecord (ZIP from the row)
  → producer_gate (delivery blocked until producer_review_complete)
  → plan_4372 (ready checks / hitl / proven_zero / incomplete)
```

Default mode is **dry-run / unbound ports**. Unbound ports never construct
`EzlynxApiClient`, never touch Secret Manager, and **never open a live
browser**.

## Test-only binds (Carlo GO)

`bind_test_enrichment_ports()` / `resolve_enrichment_ports(live_test=True)`
constructs a real client from the **UAT** secret
(`ROBIE_EZLYNX_API_UAT_SECRET` via `load_ezlynx_api_config(environment=TEST)`).
It does **not** attach Chrome.

`bind_test_browser_port()` / `ROBIE_4372_BROWSER_READ=1` / `--browser-read`
attaches the existing SSRobie Chrome CDP session. `__init__` does not
connect. It never launches Chrome.

Both **refuse** when:

- `ROBIE_ENV` is Production / PROD / LIVE
- the caller asks for a Production client / browser
- `ROBIE_ENV` is unset

They never read `ROBIE_EZLYNX_API_PROD_SECRET`.

Enable on a Test worker run:

```
ROBIE_ENV=TEST ROBIE_4372_ENRICHMENT_TEST=1
# API only — still no browser
python3 -m robie_job_engine.verification_workers --report 4372 --test-enrichment --mode dry_run

# API + Additional Interests table (SSRobie Chrome already open on the policy)
ROBIE_ENV=TEST ROBIE_4372_ENRICHMENT_TEST=1 ROBIE_4372_BROWSER_READ=1
python3 -m robie_job_engine.verification_workers --report 4372 --test-enrichment --browser-read --mode dry_run
```

`--mode dry_run` still performs **reads** when ports are bound; it does not
file notes, upload docs, or dial.

## Statuses

| Status | Meaning | Planner |
| --- | --- | --- |
| `ready` | API sources agree, **or** Additional Interests table produced unique lender+loan after an API miss. Then `verify_lender` runs per mortgage. | Pass → waiting (portal out of scope) or blocked on producer / missing ZIP. Not "blocked forever" for missing lender/loan. |
| `hitl` | Sources disagree (API vs API, API vs table, or intra-table). | Blocked — halt, do not pick a side. `verify_lender` is not called. |
| `proven_zero` | Additional Interests table is present and **explicitly empty**. | Waiting — skip only because zero was proven |
| `incomplete` | Lookup missing/failed, tab missing, unbound browser, or no unique field match. | Blocked — "lender/loan not on file" |

API `mortgagees: []` is **not** zero mortgages. PolicyApi cannot see that
tab. A missing key, a failed search, a missing tab, or a declarations PDF
with no structured mortgagee fields is **not** zero mortgages.

`verify_lender_of_record` is called with `portal_lookup=None` unless a later
step injects one. A missing portal lookup is recorded; it is not an
input-verification failure. Portal / Bland stay out of this PR.

## Rules (locked by unit tests)

1. DocumentApi search lists current declaration docs and read-backs numeric
   `document_id`. PolicyApi (or a test double of that shape) is attempted
   first.
2. API incomplete / no mortgagee fields → Additional Interests table
   fallback is invoked. API `ready` / `hitl` do not open a browser.
3. Multi-mortgage policies emit one record per loan. No merging.
   `verify_lender` is called once per ready mortgage.
4. Never invent values. Never scrape `ocr_text` / `pdf_text` / PDF bytes
   into lender or loan. Document title is not a lender name. Table parse
   refuses the same scrape keys.
5. Conflict → HITL. The module does not choose policy vs declaration vs
   table.
6. Skip only after `proven_zero` on an **explicit empty table**. Silent
   skip is a test failure. API-only empty collections fail this test.
7. SSN / full SSN never appears in output (field names or `NNN-NN-NNNN`).
8. Notes/docs writes stay API-only. Playwright / CDP / file-chooser
   **writes** call `refuse_playwright_note_or_doc` and cannot authorize
   COMPLETE. The table read is attach-only and write-less.
9. The Test client factory does not default to Production. The Test
   browser factory does not launch Chrome and does not default on.

Allowlisted structured keys stay fail-closed. Do not add Production keys
from memory. If the hermes-test-01 probe reports live keys missing from
the allowlist, reply with that literal `unknown` list.

## Dusty — live-prove the browser read on hermes-test-01 (SSRobie)

Read-only. Prefer **ROBIE Test / canary**. Applicant **220250093**
(ROBIE Test LLC) when an applicant id is needed. No zip flip. No
notes/docs writes. No portal upload. No Bland. No real-client writes.

Control policy **SAHO581361** is the API-works / no-mortgagee-fields
case. Additional Interests Wells Fargo / loan rows live on **other**
Test policies — use a ROBIE Test / canary policy that actually shows
those rows for the happy-path table prove.

```bash
gcloud compute ssh hermes-test-01 \
  --project=streetsmart-hermes-poc \
  --zone=us-east1-b \
  --tunnel-through-iap \
  --ssh-key-file=$HOME/.ssh/hermes-nopass

hostname
# must print hermes-test-01 (the probe refuses hermes-poc-01)

# Checkout this PR branch (do not deploy a Production zip):
#   git fetch origin cursor/scaffold-4372-mortgagee-enrichment-b74f
#   git checkout cursor/scaffold-4372-mortgagee-enrichment-b74f

export ROBIE_ENV=TEST
set -a
# names only — this file holds Secret Manager *references*, not payloads
source /etc/streetsmart-hermes-test/robie-verification.env
set +a
test -n "$ROBIE_EZLYNX_API_UAT_SECRET"

# 1. API key probe (still useful; expect no mortgagee fields on SAHO581361)
PYTHONPATH=. python3 scripts/probe_4372_mortgagee_metadata.py
# optional:
# PYTHONPATH=. python3 scripts/probe_4372_mortgagee_metadata.py --policy-number SAHO581361

# 2. Additional Interests table prove — SSRobie Chrome must already be
#    attached (CDP 9222) and sitting on the policy FormEntry page.
#    Do not invent a search. Do not click Add Note / Save Note.
#    Do not upload a document.
#
#    Happy path: open a ROBIE Test / canary policy whose Additional
#    Interests table shows Wells Fargo / loan-style rows, then:
PYTHONPATH=. python3 scripts/probe_4372_additional_interests.py \
  --policy-number '<TEST-OR-CANARY-POLICY>'
#
#    API-miss / empty-table path: open SAHO581361 FormEntry (if present
#    on Test) and run the same probe. Expect either explicit_empty
#    (proven_zero) or rows if that policy actually has a table.
#    Missing tab → available=false, incomplete — not proven_zero.
```

The table probe JSON prints `mortgages[].lender_name` / `loan_number` /
`interest_type`, plus `explicit_empty`, `writes=false`,
`pdf_scrape=false`, `cdp_launched_chrome=false`. Paste that JSON back.
Do not invent lenders. Do not scrape a dec PDF.

CDP URL defaults to `ROBIE_PLAYWRIGHT_CDP_URL` or
`ROBIE_BROWSER_CDP_URL` or `http://127.0.0.1:9222` — the same endpoint
`CdpReadPort` already uses.

## TODO — still out of this PR

- Portal agent-section delivery and Bland mortgage-company dials
- DiscussionApi note of the enrichment outcome with `note_id` read-back
- No Production zip, no live EZLynx writes from an agent, no merge of
  #495 or #504 until Dusty live-proves the table read
- Do not invent full EZLynx policy-search navigation
