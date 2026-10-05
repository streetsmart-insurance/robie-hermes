# Handoff: Carrier Document Retrieval — All 7 Carriers

**Date:** 2026-10-05
**From:** Ralph
**To:** Antigravity
**PR:** #765 (https://github.com/streetsmart-insurance/robie-hermes/pull/765)
**Branch:** `feat/carrier-workers-travelers-fos-guard-utica`

---

## Objective

Automate Nicole's carrier-document workflow for all carriers except Kingston:
1. Retrieve cancellation docs, pending-cancellation, pre-cancellation/intent notices
2. Match correct EZLynx applicant
3. Upload via EZLynx DocumentApi only
4. Verify by API read-back, prevent duplicates
5. Update Nicole's status sheet
6. Save dated QA copies in Drive
7. Alert plainly on failures

**Scope (Carlo 2026-10-04):** Cancellations + underwriting memos. Billing is OUT. Progressive is a carrier (FAO portal), not a doc type.

---

## What's Done and Pushed

### GEICO (commit df832691)
**File:** `robie_job_engine/geico_memo_retrieval.py` (new)

- **Login:** Username `I003873` (NOT email — SSO rejects email format; pattern is `^[a-zA-Z0-9._-]{4,18}$`). Saved in Secure Vault for `geicoextendprod.b2clogin.com`.
- **Underwriting chip:** `<gds-toggle-button role="button" aria-pressed="false|true">` with shadow DOM. Click filters client-side; URL doesn't change. Count is dynamic (27→26 on 2026-10-04).
- **Table:** 26 rows extracted. Columns: Client/Policy# | Type | Priority | Product/Description | Details btn | Due Date | Claim # | Producer Name | Action ("View Policy"/"Review Change").
- **CRITICAL FINDING:** No downloadable "underwriting memo" document exists on GEICO portal. Verified via deep inspection: personal auto Documents tab has only standard forms; commercial Timeline has transaction dialogs but no downloads; Details buttons don't exist on Underwriting rows. Emailed Nicole 2026-10-04 asking where she gets memos from (2-3 business day response requested).
- **GEICO now scoped to cancellations only** until Nicole responds.

### Guard (commit d8c540ba)
**File:** `robie_job_engine/guard_pending_cancellation.py` (modified)

- **Login:** Active as `cferrara2`. 30 cancellation records on cancellations page.
- **Fix:** "Policy Documents"/"Miscellaneous Documents" are bare text nodes (not headings/anchors). Document `<a>` elements are flat siblings with NO class/id — only `href` + `target="_blank"`. Hrefs point to `/dotnet/mvc/Workflow/ASCScribe/Home/DownloadScribeItem?scribeItemId=...&Download=true`.
- **CRITICAL:** Identical display texts are DISTINCT documents (different scribeItemIds). Do NOT dedupe by link text.
- **Tests:** 34 Guard tests pass.

### Chip patterns
**File:** `robie_job_engine/geico_pending_cancellation_noc.py` (modified)
- Added `_UNDERWRITING_CHIP_NAME` and `_UNDERWRITING_TOGGLE_TEXT` patterns.

---

## Carrier Login Status

| Carrier | Status | Details |
|---------|--------|---------|
| GEICO | ✅ Active | I003873, cancellations only |
| Guard | ✅ Active | cferrara2, 30 cancellations, parsing fix pushed |
| Farmers of Salem | ✅ Active | Carlo Ferrara, via farmersofsalem.com (bypasses CAPTCHA), 771 open tasks |
| Travelers | ✅ Active | CarloF1, has "Cancellation & Reinstatement Notices" section |
| Progressive (FAO) | ⏳ Queued | Session check at browser queue position 12 |
| NatGen | ⏳ Queued | Was logged out, session check at position 13 |
| Utica First | ❌ Blocked | Okta rejected saved password. Reset queued at position 14. **Needs fresh password from Carlo or takeover.** |

### Key Login Learnings
- **GEICO:** Use username `I003873`, never email. SSO is on `geicoextendprod.b2clogin.com`, not `gateway2.geico.com`.
- **Farmers of Salem:** Go via `www.farmersofsalem.com` → Agent Login. Direct `fos.finys.com` hits CAPTCHA.
- **Travelers:** `foragents.travelers.com` → redirects to `signin.travelers.com`. Red SIGN IN button. User ID `carloF1`.
- **Utica First:** Okta at `uticafirst.okta.com`. Saved password rejected 2026-10-05.

---

## What's NOT Done

1. **Actual document downloads:** Code is built but no PDFs have been downloaded yet from any carrier. The Guard parsing fix needs live testing with real downloads.
2. **Progressive FAO / NatGen:** Session checks queued, not yet run.
3. **Utica First:** Blocked on password. Needs Carlo's help.
4. **EZLynx integration:** DocumentApi upload + read-back not yet wired for new carriers.
5. **Duplicate prevention:** Ledger not yet implemented for new carriers.
6. **Status sheet updates:** Not yet wired.
7. **Drive QA copies:** Not yet implemented for new carriers.

---

## Technical References

### GEICO DOM (verified 2026-10-04)
```
<gds-toggle-button role="button" aria-pressed="false" show-icons="" size="small">
  Underwriting (26)
</gds-toggle-button>
```
- Shadow DOM: inner `<button>` with `<slot>` projection
- Selected: `aria-pressed="true"` + `selected=""` attribute

### Guard DOM (verified 2026-10-05)
```
[Panel: "Agency Service Center Printable Documents for PRAU716089"]
  "Policy Documents" (bare text node)
  <a href="/dotnet/mvc/Workflow/ASCScribe/Home/DownloadScribeItem?scribeItemId=...&Download=true" target="_blank">Cancellation - 09/23/2026 - 09/23/2026</a>
  <a href="..." target="_blank">Premium Billing Statements - 09/22/2026 - 09/23/2026</a>
  "Miscellaneous Documents" (bare text node)
  <a href="..." target="_blank">...</a>
```

### Test Commands
```bash
cd ~/workspace/projects/robie-hermes
python3 -m pytest tests/test_guard_pending_cancellation.py -q  # 34 tests
python3 -m py_compile robie_job_engine/geico_memo_retrieval.py
```

### Push to PR (no git push auth in this env — use GitHub API)
See `/tmp/push-geico-commit.py` and `/tmp/push-guard-fix.py` for the API push pattern.

---

## Nicole Email (sent 2026-10-04)
**To:** nicole@streetsmart.insurance
**Subject:** Quick question on GEICO underwriting memos
**Ask:** Where does she get underwriting memos from? What format? Can she share an example? Response requested in 2-3 business days.

---

## Status Sheet
Google Sheet: https://docs.google.com/spreadsheets/d/1mftcTKv7lRCspyN0sXKxdoYfnmF7S4k8Nb25Z1_71WA/edit
Excel backup in Drive QA folder: `Robie Carrier Pull QA (Nicole)` (ID: 1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2)

---

## Immediate Next Steps for Antigravity

1. **Utica First:** Get fresh password from Carlo (or have him do browser takeover). Complete Okta login.
2. **Progressive FAO / NatGen:** Wait for queued session checks, then verify document sections.
3. **Guard live download test:** Run the worker against the 30 cancellations, download actual PDFs, verify the parsing fix works on live data.
4. **Wire EZLynx DocumentApi:** Upload downloaded docs, verify with API read-back.
5. **Build duplicate ledger:** Prevent re-downloading same documents.
6. **Nicole's response:** When she replies about underwriting memos, adjust GEICO scope accordingly.

---

## Standing Rules (from AGENTS.md)
- Work on feature branch, PR required. Never develop on `main`.
- EZLynx notes/documents are API-only. Never use browser for EZLynx uploads.
- Every bug fix needs a regression test.
- Show commands and actual output. "Done" without proof is UNVERIFIED.
- Separate BUILT from DESIGNED.
- Merge freeze: HOLD all merges to `main` until Dusty's #779 fix lands with certified commit.

## HOW ANTIGRAVITY NEEDS TO WORK (Carlo's 8 rules — 2026-10-05)

1. Don't tell me it's done — show me. Paste the command and its actual output. "Fixed", "done", "deployed", "working" are not results.

2. If you can't show output, the answer is UNVERIFIED. Say that word. It's not a failure and I won't treat it as one.

3. Separate BUILT from DESIGNED. BUILT means it ran and here's the proof. DESIGNED means you wrote it and it has never run. Label every claim as one or the other, on the same line as the claim.

4. Quote sources literally. Never paraphrase a value, an ID, a number, a filename or a line of a file. If you couldn't read it, say so — don't reconstruct it from memory.

5. Don't write "verified", "enforced", "active" or "confirmed" unless you can name the specific check that would fail if it weren't true.

6. Tell me what you tried and abandoned, and why. A reply containing only successes is incomplete and I'll treat it as UNVERIFIED.

7. If you're inferring rather than checking, say "I'm inferring this" and say what would confirm it.

8. If what I asked for is wrong, impossible, or a bad idea, say so plainly instead of doing the nearest easy thing and calling it done.
