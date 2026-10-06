# Handoff: GEICO Memo Expansion (Underwriting + Billing)

**Date:** 2026-10-04
**Status:** UNDERWRITING IMPLEMENTED — live DOM verified 2026-10-04 17:19 EDT. Billing chip does NOT exist.
**Branch:** (uncommitted — needs feature branch per AGENTS.md)
**File:** `robie_job_engine/geico_memo_retrieval.py`

---

## What was requested

Carlo (2026-10-04): Expand the carrier document retrieval beyond just cancellations
to include **underwriting memos** and **billing memos**.

## What's built

### 1. Framework file: `robie_job_engine/geico_memo_retrieval.py`

A skeleton module with:
- Chip name patterns for Underwriting and Billing (with count suffix regex)
- `MemoRecord` dataclass (policy_number, insured_name, alert_date, description, etc.)
- `MemoPullResult` dataclass (records, downloaded, held, errors)
- `ensure_underwriting_view()` — TODO, needs live DOM
- `ensure_billing_view()` — TODO, needs live DOM
- `extract_memo_list()` — TODO, needs live DOM
- `download_memo_document()` — TODO, needs live DOM
- `run_memo_pull()` — orchestrator (implemented, calls the TODO functions)
- CLI entry point (`--type underwriting|billing`)

### 2. Chip patterns added to `geico_pending_cancellation_noc.py`

```python
_UNDERWRITING_CHIP_NAME = re.compile(
    r"^underwriting(?:\s*\(\s*\d+\s*\))?$", re.IGNORECASE,
)
_BILLING_CHIP_NAME = re.compile(
    r"^billing(?:\s*\(\s*\d+\s*\))?$", re.IGNORECASE,
)
_UNDERWRITING_TOGGLE_TEXT = re.compile(r"^\s*Underwriting", re.IGNORECASE)
_BILLING_TOGGLE_TEXT = re.compile(r"^\s*Billing", re.IGNORECASE)
```

## What's NOT done (needs live testing)

The four `TODO` functions need to be implemented against the live GEICO portal DOM.
The Pending Cancellations flow in `geico_pending_cancellation_noc.py` is the reference
implementation — it handles:
- `gds-toggle-button` shadow DOM (the chip is a toggle with a shadow button inside)
- `aria-pressed` state detection
- Count suffix in accessible name (e.g. "Underwriting (27)")
- Role-based fallback queries

### Completion steps

1. **Re-establish GEICO session** on the test VM:
   ```bash
   # SSH tunnel must be active on localhost:2225
   # GEICO uses SSO — the session may need re-auth
   # Portal: https://gateway.geico.com/ (verify exact URL from prior work)
   ```

2. **Navigate to Client Alerts page** and inspect the Underwriting chip DOM:
   - Is it a `gds-toggle-button` like Pending Cancellations?
   - What's the exact accessible name? (e.g. "Underwriting (27)")
   - Does `aria-pressed` indicate selection state?

3. **Implement `ensure_underwriting_view()`** following `ensure_pending_view()` pattern:
   - Copy the structure from lines 608-634 of `geico_pending_cancellation_noc.py`
   - Replace `_PENDING_TOGGLE_TEXT` with `_UNDERWRITING_TOGGLE_TEXT`
   - Replace `_PENDING_CHIP_NAME` with `_UNDERWRITING_CHIP_NAME`
   - The helper functions (`_pending_chip_matches`, `_pending_chip_view`,
     `_click_pending_chip`, `pending_view_selected`) need generic versions
     OR duplicated with the new patterns

4. **Implement `ensure_billing_view()`** — same as above with Billing patterns.

5. **Implement `extract_memo_list()`**:
   - After selecting the chip, what does the table/grid look like?
   - What columns are available? (policy, insured, date, description?)
   - Is there a link/button per row to view the memo document?

6. **Implement `download_memo_document()`**:
   - Click into the memo — does it open a new tab, modal, or PDF viewer?
   - Is there a download button? What's its selector?
   - What's the filename pattern?

7. **Write regression tests** (per AGENTS.md: "Every meaningful bug fix requires
   a regression test" — this is a new feature, tests are required):
   - Test chip pattern regexes match "Underwriting (27)", "Billing", etc.
   - Test `MemoRecord` and `MemoPullResult` dataclasses
   - Mock-based tests for `run_memo_pull()` orchestration
   - DO NOT test against live portal in CI

8. **Create feature branch and PR** (per AGENTS.md):
   ```bash
   git checkout -b feat/geico-memo-retrieval
   git add robie_job_engine/geico_memo_retrieval.py
   git add robie_job_engine/geico_pending_cancellation_noc.py
   git add tests/test_geico_memo_retrieval.py
   git commit -m "Add GEICO underwriting/billing memo retrieval framework"
   git push origin feat/geico-memo-retrieval
   # Create PR via gh CLI
   ```

## Known live state (2026-10-04)

- **GEICO Client Alerts page** has these chips (observed):
  - Pending Cancellations (2) — WORKING via `geico_pending_cancellation_noc.py`
  - Underwriting (27) — NOT YET IMPLEMENTED
  - Billing — NOT YET IMPLEMENTED (count unknown)
  - All Alerts, Renewals, Claims, Umbrella Eligible — out of scope

- **GEICO session**: Was active earlier 2026-10-04 via SSO. Tab was closed during
  Guard cleanup. Will need re-auth before live testing.

- **Test VM**: `hermes-test-01`, CDP at `http://127.0.0.1:9223`
  SSH: `ssh -i ~/.ssh/hermes_ralph -p 2225 sa_112650695780807418521@localhost`
  (requires IAP tunnel on localhost:2225)

## Other carriers (out of scope for this handoff)

- **Guard**: Printable documents page lists Cancellations, Endorsements,
  Declarations, Welcome Letters, Billing Statements. Currently filters for
  cancellations only. Expanding the filter is straightforward once the
  download selector is fixed (separate issue).
- **Farmers of Salem**: Worker mentions "billing memo matching" but blocked by
  reCAPTCHA on Finys. Needs correct portal URL from Carlo.
- **Travelers, Utica, NatGen, Progressive FAO**: Cancellation-focused.
  Memo expansion not yet scoped.

## Files changed

| File | Change |
|------|--------|
| `robie_job_engine/geico_memo_retrieval.py` | NEW — framework skeleton (7.4KB) |
| `robie_job_engine/geico_pending_cancellation_noc.py` | ADDED chip patterns for Underwriting/Billing |

## Test results

- Existing GEICO tests: **not re-run** (only added regex patterns, no logic changed)
- New module: **no tests yet** (framework only, functions raise NotImplementedError)
- Full suite: **not run**

## Remaining risks

1. The Underwriting/Billing chips may have different DOM structure than Pending
   Cancellations (different component, different interaction pattern).
2. Memo documents may not be downloadable (may be view-only in portal).
3. The 27 Underwriting items may include non-document alerts (informational only).
4. GEICO session expiry during development (SSO re-auth needed).

## Rollback

No deployment occurred. Changes are uncommitted in working tree. To discard:
```bash
cd /home/hatch/workspace/projects/robie-hermes
git checkout -- robie_job_engine/geico_pending_cancellation_noc.py
rm robie_job_engine/geico_memo_retrieval.py
```

---

**Per AGENTS.md:** This is a candidate handoff, not a deployment. It requires
a feature branch, PR, regression tests, and QA before Test deployment.
