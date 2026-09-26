# 4372 Mortgagee Verification Report — Filter Fix Required

**Date:** 2026-09-26
**Status:** NEEDS EZLynx LOOK EDIT (cannot be fixed in code)

## Problem

Report 4372 ("Mortgagee Verification Queue - ROBIE") currently returns **closed tasks from 2025** instead of actionable upcoming work. On 2026-09-26 the report returned 4 rows, all with `Task Status: Closed`:

| Account | Policy | Task Due | Task Closed |
|---|---|---|---|
| Saeed Abbaszadeh | SAHO581361 | 2025-12-12 | 2025-12-12 |
| Ruth Cruz | 4217318 | 2025-12-09 | 2025-12-09 |
| Angela & Sean Marchak | FLD272903 | 2025-10-30 | 2025-10-30 |
| Claudia Salgado & Paul Still | HONJ046535 | 2025-10-07 | 2025-10-08 |

The worker correctly filters these (`_4372_closed_reason`), so the Monday run plans **zero actions**. The queue is empty not because there's no work, but because the report filter is wrong.

## Required Look Changes (EZLynx UI)

1. **Task Status = Open only.** Exclude Closed/Cancelled tasks. The current filter appears to include all statuses.
2. **Add Policy Expiration Date column.** The report has no expiration date — the worker cannot verify the 30–45 day window without it.
3. **Filter to policies expiring in 30–45 days.** The worker's job is "30–45 days before renewal" mortgagee verification. The report should pre-filter to that window.
4. **Activity Label = "Mortgagee Bill Payment"** (already present — keep).

## Why Code Can't Fix This

The worker already handles closed tasks correctly (skips them). But if the report only returns closed tasks, there's nothing to work. The report is the worker's eyes — it needs to see the right policies.

## Verification

After the Look is fixed, re-run the 4372 worker in dry_run and confirm:
- `ingested > 0` AND `work_items > 0` (not just ingested > 0)
- Plans show `lender TBD` → blocked (expected until enrichment is wired), NOT zero items
