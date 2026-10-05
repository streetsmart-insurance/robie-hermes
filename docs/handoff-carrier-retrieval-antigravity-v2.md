# Antigravity Handoff: Carrier Document Retrieval — Build This Out

**Date:** 2026-10-05 13:05 EDT
**Branch:** `feat/carrier-workers-travelers-fos-guard-utica`
**PR:** https://github.com/streetsmart-insurance/robie-hermes/pull/765
**Test Server:** hermes-test-01 (us-east1-b, streetsmart-hermes-poc)
**Current Test pointer:** `/opt/streetsmart-hermes-test/releases/c855876a0781/robie-hermes-c855876a0781` (main, NOT our branch)

---

## Carlo's 8 Working Rules (verbatim — follow these or don't bother)

1. Don't tell me it's done — show me. Paste the command and its actual output. "Fixed", "done", "deployed", "working" are not results.
2. If you can't show output, the answer is UNVERIFIED. Say that word.
3. Separate BUILT from DESIGNED. BUILT = it ran and here's the proof. DESIGNED = written, never run. Label every claim.
4. Quote sources literally. Never paraphrase a value, ID, number, filename or file line. If you couldn't read it, say so.
5. Don't write "verified", "enforced", "active" or "confirmed" unless you can name the specific check that would fail if it weren't true.
6. Tell me what you tried and abandoned, and why. A reply with only successes is incomplete.
7. If inferring rather than checking, say "I'm inferring this" and say what would confirm it.
8. If what he asked is wrong, impossible, or a bad idea, say so plainly instead of doing the nearest easy thing.

---

## Objective

Automate Nicole Segovia's carrier-document workflow for **all carriers except Kingston**.

**Document scope:**
- Cancellation, pending-cancellation, pre-cancellation/intent notices
- Underwriting memos where they genuinely exist
- **Billing is OUT**
- **GEICO is cancellations-only** until Nicole clarifies underwriting memos (email sent 2026-10-04, awaiting response)

---

## Carrier Status (as of 2026-10-05 13:05 EDT)

### 1. GEICO — BUILT (code), UNVERIFIED (live pull)
- **Login:** BUILT — username `I003873` works. GEICO rejects email-form usernames.
- **Form pattern observed:** `^[a-zA-Z0-9._-]{4,18}$`
- **Credential origin:** `https://geicoextendprod.b2clogin.com`
- **Client Alerts:** `https://gateway2.geico.com/client-alerts`
- **Finding:** 26 Underwriting alerts extracted 2026-10-04. Deep investigation found NO downloadable underwriting memo files (Personal auto Documents = standard forms; Commercial Timeline = Change Summary/Transaction Detail dialogs; Underwriting rows had no Details buttons; Commercial Policy Forms = standard forms).
- **Decision:** Cancellations-only until Nicole responds.
- **Code:** `robie_job_engine/geico_pending_cancellation_noc.py` (BUILT, pushed, NOT run live)
- **Code:** `robie_job_engine/geico_memo_retrieval.py` (BUILT, pushed, PARKED — not operational proof)

### 2. Guard — BUILT (code + fix), UNVERIFIED (live download)
- **Login:** BUILT — `cferrara2` at `https://gigezrate.guard.com/portal/book-of-business/cancellations`
- **Finding:** 30 cancellation records, 25 visible on page 1 (checked 2026-10-04 23:22 UTC)
- **Printable Documents URL pattern:** `https://gigezrate.guard.com/dotNet/mvc/workflow/PrintableDocuments?linkid=356&MGACODE={POLICY}&TABLABEL=undefined`
- **Document links use:** `/dotnet/mvc/Workflow/ASCScribe/Home/DownloadScribeItem?scribeItemId=...&Download=true`
- **Known gap:** `GuardDocument` does NOT preserve `href` or `scribeItemId`. `open_document()` locates by description text. Duplicate display text with different IDs = ambiguous.
- **Code:** `robie_job_engine/guard_pending_cancellation.py` (BUILT, pushed)
- **Fix pushed:** printable-documents parser for live DOM (commit `d8c540ba`)
- **Tests:** 34 passed locally
- **STILL NEEDED:**
  1. Add `href`/`scribeItemId` to `GuardDocument`
  2. Regression test with duplicate visible text + different IDs
  3. `open_document()` selects by durable ID, not description
  4. Run real download on Test, then second run proving duplicate skip

### 3. Farmers of Salem — DESIGNED (login only), NOT MAPPED
- **Login:** BUILT — via `https://www.farmersofsalem.com/` → Agent Login → `https://fos.finys.com/` (direct navigation hits reCAPTCHA)
- **Finding:** 771 open tasks including cancellation-related (checked 2026-10-04 23:25 UTC)
- **Carlo's directive (2026-10-05):** Filter to last 1-2 weeks. Don't work all 771.
- **STILL NEEDED:** Map task grid structure, build date-filtered pull, extract cancellation tasks

### 4. Travelers — DESIGNED (login only), NOT MAPPED
- **Login:** BUILT — `CarloF1` at `https://foragents.travelers.com/Business`
- **Finding:** `Direct Bill Activity — Cancellation & Reinstatement Notices` section found (2026-10-04 23:34 UTC)
- **STILL NEEDED:** Map the section, build document pull

### 5. Progressive FAO — DESIGNED (login only), MAPPING IN PROGRESS
- **Login:** BUILT — session at `https://www.foragentsonly.com/home/?Welcome=474`
- **Finding:** SmartView Alerts shows 44+ items (e-Sign: 8, follow-up: 11, save canceled: 4, paperless: 6, claims: 3, policy changes: 12) (checked 2026-10-05 16:43 UTC)
- **STILL NEEDED:** Map SmartView Alerts DOM, build alert-to-document pull logic

### 6. NatGen — DESIGNED (login only), NOT MAPPED
- **Login:** BUILT — session at `https://natgenagency.com/MainMenu.aspx`
- **Finding:** 3 Pending Cancellations, 2 Pending Non-Renewal, 2 Pending Renewal Actions (checked 2026-10-05 16:43 UTC)
- **STILL NEEDED:** Map alerts, build document pull

### 7. Utica First — BLOCKED
- **Status:** Password reset emails NOT arriving. Reset flow exhausted.
- **Username:** User's email address (corrected 2026-10-05, saved in Secure Vault for `uticafirst.okta.com`)
- **Password:** Saved in Secure Vault (user provided 2026-10-05)
- **Next:** Direct login with saved credentials (bypasses broken reset)

---

## What Needs Building

### Phase 1: Carrier Pull Logic (per carrier)
For each of Farmers, Travelers, Progressive, NatGen:
1. Map the alert/document DOM structure (browser inspection)
2. Write the pull module in `robie_job_engine/`
3. Unit tests with fixtures
4. Run on Test server against live portal, download real PDFs
5. Show the downloaded files as proof

### Phase 2: Guard Durable ID Fix
1. Add `href`/`scribeItemId` to `GuardDocument`
2. Regression test with duplicate text/different IDs
3. `open_document()` by durable ID
4. Prove duplicate skip on second run

### Phase 3: Output Pipeline
For every downloaded document:
1. **Durable ledger** — record document ID, carrier, policy, date. Skip if already recorded.
2. **EZLynx DocumentApi upload** — `upload_applicant_document()`, then read-back via `search_applicant_documents` asserting a HIT
3. **Nicole status sheet** — update the carrier status
4. **Drive QA copy** — save PDF under dated folder structure
5. **Plain-English failure alert** — if pull fails, say what's missing

### What Does NOT Get Built
- No EZLynx browser uploads (API only)
- No policy/change transaction entry (Robie never enters these)
- No GEICO underwriting filing until Nicole clarifies
- No billing workflow
- No production deploy, no timers (merge/timer freeze in effect until Dusty's #779 lands)

---

## Server Deployment

**Test VM:** hermes-test-01
**Connect:**
```bash
# Start IAP tunnel (background)
/home/hatch/google-cloud-sdk/bin/gcloud compute start-iap-tunnel hermes-test-01 22 --local-host-port=localhost:2225 --zone=us-east1-b --project=streetsmart-hermes-poc

# SSH
ssh -i ~/.ssh/hermes_ralph -p 2225 sa_112650695780807418521@localhost
```

**Current Test pointer:** `/opt/streetsmart-hermes-test/releases/c855876a0781/robie-hermes-c855876a0781` (main)

**To deploy our branch to Test:**
1. Build release from `feat/carrier-workers-travelers-fos-guard-utica` HEAD
2. Create new release dir under `/opt/streetsmart-hermes-test/releases/`
3. Flip the `current` symlink
4. Run smoke test: execute each carrier pull once, show downloaded files
5. Health check: verify ledger, EZLynx upload read-back, sheet update

**Kill switch (keep OFF):**
```
ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX=0
```

---

## Key Files

- `robie_job_engine/guard_pending_cancellation.py` — Guard worker
- `robie_job_engine/geico_pending_cancellation_noc.py` — GEICO cancellations
- `robie_job_engine/geico_memo_retrieval.py` — GEICO memos (PARKED)
- `docs/handoff-geico-memo-expansion.md` — GEICO investigation notes
- `docs/handoff-carrier-retrieval-antigravity.md` — Previous handoff (this file supersedes it)

---

## Blockers & Risks

1. **PR #765 is stacked on conflicting PR #699.** Do NOT force-merge #699. Changes eventually need porting to clean branch from current main.
2. **Merge/timer freeze:** No merges to main, no timers until Dusty supplies certified commit after #779.
3. **Utica First:** Login blocked until direct credential login succeeds.
4. **GEICO underwriting:** Parked until Nicole responds (sent 2026-10-04, expect response ~2026-10-07/08).
5. **Browser queue:** Multiple tasks share one browser; expect delays.

---

## Definition of Done

For each carrier:
- [ ] Real PDFs downloaded from live portal on Test server (show the files)
- [ ] Duplicate run skips already-downloaded documents (show the skip)
- [ ] EZLynx DocumentApi upload with read-back HIT (show the API response)
- [ ] Nicole's status sheet updated (show the cell values)
- [ ] All claims labeled BUILT/DESIGNED/UNVERIFIED per Carlo's rules
