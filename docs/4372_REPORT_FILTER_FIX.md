# 4744 Mortgagee Verification Report — Design (supersedes the 4372 task-based approach)

**Date:** 2026-09-27
**Status:** REPORT FIXED IN EZLynx; worker ingestion implemented on this branch

## What changed

Report 4372 ("Mortgagee Verification Queue - ROBIE Task-Based") was the
wrong source: a 32-column task export that returned closed 2025 tasks and
carried no policy expiration date, so the worker could never verify its
30–45 day window. Carlo corrected the design on 2026-09-27: the mortgagee
worker runs off a **Homeowners + Flood policy-expiration report**.

The corrected report is **4744 — "Mortgagee Verification Queue - ROBIE"**:

- Filters: Line Of Business is Flood or Homeowners; Policy Status is
  Active; Policy Expiration Date is before (relative) 45 days from now
  (rolling — saved via Explore actions → Edit → blue Save button; the
  "Save As..." path does NOT persist filter changes).
- Columns (8): Account Name, Policy Number, Master Company,
  Line Of Business, Premium - Annualized, Assigned Producer, CSR,
  Policy Expiration Date. (The Looker viewer displays these prefixed as
  "Policy Data ..."/"Applicant Data ..."; the scheduled CSV export uses
  the base names — the worker fingerprints the export.)
- Schedule: Email → robie@streetsmart.insurance, CSV, Daily, 5:00 AM
  America/New_York. The schedule inherits the rolling filter.

There is intentionally **no "Mortgagee Bill Payment" activity label**
anywhere in this design — the queue is driven by policy expiration, not
by task labels. The old doc revision telling the reader to "retain" that
label was wrong and is superseded by this file.

## Worker contract (this branch)

- `gmail_report_ingestion` fingerprints the 8-column CSV as report
  `4744` and validates the exact ordered header list.
- `verification_workers.build_work_items` maps the 8 export columns onto
  work items (one per policy number).
- `_4744_window_reason` narrows the report's ≤45-day feed to Carlo's
  **30–45 day** mortgagee window. Out-of-window rows land in
  `excluded_stale` with a reason — never silently dropped.
- `plan_4372` plans from the expiration date (no task due date exists on
  this report): `expires YYYY-MM-DD (N days out)`.
- `mortgagee_enrichment` resolves lender + loan number read-only
  (PolicyApi search → Additional Interests; browser fallback on API
  miss). Missing, ambiguous, or conflicting lender identity → HOLD,
  never a guessed lender. Producer gate still blocks delivery.
- Report 4372 ingestion is retained for backward compatibility but is
  no longer the mortgagee source; do not schedule it.

## Verification

- `pytest tests/test_verification_workers.py` — 4744 ingestion, window
  narrowing, enrichment HOLD/READY paths.
- Dry-run `run_worker("4744", ...)` against a real 4744 CSV: expect
  `ingested > 0`, `work_items` = rows inside 30–45d, and every action
  `blocked` with a lender reason until live enrichment is wired.
