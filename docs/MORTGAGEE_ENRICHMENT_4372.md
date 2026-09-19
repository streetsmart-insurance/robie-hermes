# 4372 mortgagee enrichment scaffold

For Carlo and Ralph. This is a **read-only scaffold**, not a live Production
path. No portal automation and no Bland dials ship in this PR.

## Where it plugs in

Open 4372 work items (closed tasks already excluded) go through
`robie_job_engine/mortgagee_enrichment.py` before `plan_4372`:

```
Gmail CSV 4372
  → build_work_items
  → drop Closed tasks (explicit reason, never silent)
  → enrich_work_item (DocumentApi search + PolicyApi structured fields)
  → plan_4372 (ready / hitl / proven_zero / incomplete)
```

Default mode is **dry-run**. `run_worker(..., enrichment_ports=...)` is how
tests (and a later Test live-prove) inject clients. Unbound ports never
construct `EzlynxApiClient` and never touch Secret Manager.

## Statuses

| Status | Meaning | Planner |
| --- | --- | --- |
| `ready` | Every mortgage has lender + loan from **agreeing** structured sources. Each mortgage is a separate record. | Blocked — delivery is out of this scaffold |
| `hitl` | Declaration structured fields disagree with policy fields. | Blocked — halt, do not pick a side |
| `proven_zero` | Both sources returned an **explicit empty** mortgage collection. | Waiting — skip only because zero was proven |
| `incomplete` | Lookup missing/failed, or no unique structured field match. | Blocked — same class as "lender/loan not on file" |

A missing key, a failed search, or a declarations PDF with no structured
mortgagee fields is **not** zero mortgages.

## Rules (locked by unit tests)

1. DocumentApi search lists current declaration docs and read-backs numeric
   `document_id`. PolicyApi (or a test double of that shape) supplies
   structured mortgagee fields the codebase already has.
2. Multi-mortgage policies emit one record per loan. No merging.
3. Never invent values. Never scrape `ocr_text` / `pdf_text` / PDF bytes
   into lender or loan. Document title is not a lender name.
4. Conflict → HITL. The module does not choose policy vs declaration.
5. Skip only after `proven_zero`. Silent skip is a test failure.
6. SSN / full SSN never appears in output (field names or `NNN-NN-NNNN`).
7. Notes/docs writes stay API-only. Playwright / CDP / file-chooser calls
   `refuse_playwright_note_or_doc` and cannot authorize COMPLETE.

## TODO — next live-prove step

Out of this PR:

- Bind a real `EzlynxApiClient` on **Test only** and prove DocumentApi
  search + PolicyApi search against the ROBIE Test applicant (not a live
  client). Record the actual metadata keys that carry mortgagee name and
  loan number — do not guess new keys in Production.
- After `ready`, hand each mortgage to the existing producer-gate /
  lender-of-record checks in `mortgagee_verification_worker.py`.
- Portal agent-section delivery and Bland mortgage-company dials.
- DiscussionApi note of the enrichment outcome with `note_id` read-back.
- No Production zip, no live EZLynx writes from an agent, no merge of #495.
