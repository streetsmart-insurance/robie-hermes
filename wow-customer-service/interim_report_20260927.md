# WOW Customer Service - September 2026 Investigation
# Interim Report as of 2026-09-27 20:00 EDT

> This is NOTE-SCREENING, not final reconciliation. Per agency rules, labels
> and notes are claims, not proof. Evidence must come from the authoritative
> destination system (carrier portal, EZLynx reports, Magellan). Flagged items
> are pending review, NOT final rejections.

## Executive Summary
Analyzed 95 September rows across 7 WOW labels. Significant label inflation found.
Only 1 autopay verified via carrier portal. 40 of 46 "All Star Calls" have no call evidence in notes.

## Label-by-Label Results

### 1. Google Reviews - IN PROGRESS (not complete)
- Methodology saved to repo
- September: 8 reviews reviewed, 5 direct-name credits ($50)
- Attribution rule: "If they say someone's name, we give them the credit"
- Ambiguous reviews routed to staff via distribution list
- Still unresolved: month/attribution for some reviews, EZLynx matching for unnamed reviews

### 2. Autopay Setup (35 rows)
**FLAGGED FOR REVIEW (6):** Notes contradict label (paid in full / mortgage billed)
- All 6 by Karla Brown. Pending review, not final rejection.

**VERIFIED (1):** 
- Stopka / Progressive 879834477 - EFT confirmed in ForAgentsOnly

**PORTAL PENDING/BLOCKED (9):**
- Travelers (5): blocked by bot detection, Dusty brief ready
- Bristol West (1): no saved credentials
- Liberty Mutual (1): credentials rejected
- Plymouth Rock (2): credentials rejected 2026-09-27

**WEAK (19):** Notes don't mention autopay - likely mislabeled
- 3 claim autopay but need verification (DJ Movers, East Coast Framers, Mansukh)
- 16 don't mention autopay at all

### 3. New Customer CSR (43 rows)
**RED FLAGS (6):** Not issued - app sent, shell only, bound not downloaded
**WITH POLICY NUMBERS (8):** Need EZLynx issuance check
**REMAINING (29):** Need review

By employee: Karla 19, Jazmin 7, Ashley 5, Zeus 4, Taylor 4, Mike 4

### 4. Cross Sell (13 rows)
**STRONG (3):** Issued, verify in EZLynx (Top Notch Lawn, SK Direct, Bruder)
**NEEDS VERIFICATION (5):** Sold/bound, check issuance
**WEAK (4):** App sent/shell/bind requested - not issued
**FLAGGED (1):** Top Notch Tree Service - BOR renewal, not a cross-sell. Pending review.

### 5. Coverage Enhancement (5 rows)
**POSSIBLE (1):** Yen Tshering - collision + 2 discounts (3-enhancement rule UNCONFIRMED, awaiting Alejandro)
**WEAK (2):** Umbrella request (not completed), PFA endorsement (unclear)
**FLAGGED (2):** New policies mislabeled as enhancements. Pending review.

### 6. Referral Won (4 rows) — rule CONFIRMED by Carlo 2026-09-27
**Rule:** Agent got the referral — wrote a policy for a client, that client referred someone else.
**Proof:** Linked applicant + Nicole's weekly referral report (verified: Nicole IS sending ~weekly since Aug 4, report is being worked).
**CONFIRMED IN REPORT (2):** Lauren Pender (referrer Jackie & Shane Pender), Rebecca Gallman (referrer Jennifer Pinkowsky) — both Karla Brown.
  - Attribution flag: report shows Jazmin Molina as assigned agent on Lauren Pender; WOW label says Karla. Alejandro to confirm credit.
**FLAGGED (2):** Ashley Huntley x2 (Ashley & Troy Huntley, Kevin Calhoun) — not in report. Check with Nicole whether missing or never referrals.

### 7. All Star Call (46 rows)
**WITH AI NOTES (6):** Need Magellan verification (criteria UNCONFIRMED, awaiting Alejandro)
**WITHOUT CALL EVIDENCE (40):** No proof a call occurred in notes. Flagged pending Magellan pull.

By employee: Karla 24, Jazmin 10, Zeus 5, Taylor 5, Ashley 2
Karla's 24 claims with minimal evidence needs verification.

## AppSheet Status
- August 2026: 167 rows uploaded ✓
- July 2026: MISSING (not uploaded)
- September 2026: Not yet uploaded (in progress)

## Blockers
1. Travelers portal bot-blocks sandbox (Dusty brief ready for box)
2. Bristol West: no saved credentials
3. Liberty Mutual: saved credentials rejected
4. Magellan/Sonant: no API skill, need browser access for sentiment data
5. July 2026 data missing from AppSheet

## Next Steps
1. Get Travelers verifications via Dusty/box
2. Resolve Bristol West/LM/Plymouth Rock credentials via Alejandro
3. Alejandro confirms: Coverage Enhancement rule, Referral source standard, All Star Call criteria
4. Pull Magellan data for All Star Call verification
5. EZLynx issuance checks for NCSR/Cross Sell
6. Compile final reconciliation for Oct 1
