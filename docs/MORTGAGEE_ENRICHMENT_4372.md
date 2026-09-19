# 4372 mortgagee enrichment scaffold

For Carlo and Ralph. This is a **read-only / Test-prove** scaffold, not a
Production path. No portal automation and no Bland dials ship in this PR.

## Where it plugs in

Open 4372 work items (closed tasks already excluded) go through
`robie_job_engine/mortgagee_enrichment.py` before `plan_4372`:

```
Gmail CSV 4372
  → build_work_items
  → drop Closed tasks (explicit reason, never silent)
  → enrich_work_item (DocumentApi search + PolicyApi structured fields)
  → if ready: verify_lender per MortgageRecord (ZIP from the row)
  → producer_gate (delivery blocked until producer_review_complete)
  → plan_4372 (ready checks / hitl / proven_zero / incomplete)
```

Default mode is **dry-run / unbound ports**. Unbound ports never construct
`EzlynxApiClient` and never touch Secret Manager.

## Test-only client bind (Carlo GO)

`bind_test_enrichment_ports()` / `resolve_enrichment_ports(live_test=True)`
constructs a real client from the **UAT** secret
(`ROBIE_EZLYNX_API_UAT_SECRET` via `load_ezlynx_api_config(environment=TEST)`).

It **refuses** when:

- `ROBIE_ENV` is Production / PROD / LIVE
- the caller asks for a Production client
- `ROBIE_ENV` is unset

It never reads `ROBIE_EZLYNX_API_PROD_SECRET`.

Enable on a Test worker run:

```
ROBIE_ENV=TEST ROBIE_4372_ENRICHMENT_TEST=1
# or
python3 -m robie_job_engine.verification_workers --report 4372 --test-enrichment --mode dry_run
```

`--mode dry_run` still performs **reads** when ports are bound; it does not
file notes, upload docs, or dial.

## Statuses

| Status | Meaning | Planner |
| --- | --- | --- |
| `ready` | Structured sources agree on every mortgage. Then `verify_lender` runs per mortgage. | Pass → waiting (portal out of scope) or blocked on producer / missing ZIP. Not "blocked forever" for missing lender/loan. |
| `hitl` | Declaration structured fields disagree with policy fields. | Blocked — halt, do not pick a side. `verify_lender` is not called. |
| `proven_zero` | Both sources returned an **explicit empty** mortgage collection. | Waiting — skip only because zero was proven |
| `incomplete` | Lookup missing/failed, or no unique structured field match. | Blocked — "lender/loan not on file" |

A missing key, a failed search, or a declarations PDF with no structured
mortgagee fields is **not** zero mortgages.

`verify_lender_of_record` is called with `portal_lookup=None` unless a later
step injects one. A missing portal lookup is recorded; it is not an
input-verification failure. Portal / Bland stay out of this PR.

## Rules (locked by unit tests)

1. DocumentApi search lists current declaration docs and read-backs numeric
   `document_id`. PolicyApi (or a test double of that shape) supplies
   structured mortgagee fields the codebase already has.
2. Multi-mortgage policies emit one record per loan. No merging.
   `verify_lender` is called once per ready mortgage.
3. Never invent values. Never scrape `ocr_text` / `pdf_text` / PDF bytes
   into lender or loan. Document title is not a lender name.
4. Conflict → HITL. The module does not choose policy vs declaration.
5. Skip only after `proven_zero`. Silent skip is a test failure.
6. SSN / full SSN never appears in output (field names or `NNN-NN-NNNN`).
7. Notes/docs writes stay API-only. Playwright / CDP / file-chooser calls
   `refuse_playwright_note_or_doc` and cannot authorize COMPLETE.
8. The Test client factory does not default to Production.

Allowlisted structured keys stay fail-closed. Do not add Production keys
from memory. If the hermes-test-01 probe reports live keys missing from
the allowlist, reply with that literal `unknown` list.

## Dusty — live-prove probe on hermes-test-01

Read-only. Applicant **220250093** (ROBIE Test LLC) only. No zip flip.
No notes/docs writes. No real-client applicant ids.

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
# confirm the UAT reference is set; do not print the file
test -n "$ROBIE_EZLYNX_API_UAT_SECRET"

PYTHONPATH=. python3 scripts/probe_4372_mortgagee_metadata.py
# optional: also search a known Test policy number
# PYTHONPATH=. python3 scripts/probe_4372_mortgagee_metadata.py --policy-number '<TEST-POLICY>'
```

The JSON prints `keys.document` / `keys.policy` and
`classification.allowlisted` / `classification.unknown`. Paste
`unknown` back if a live mortgagee/loan field is missing from the
allowlist. Do not invent keys.

## TODO — still out of this PR

- Portal agent-section delivery and Bland mortgage-company dials
- DiscussionApi note of the enrichment outcome with `note_id` read-back
- No Production zip, no live EZLynx writes from an agent, no merge of #495
