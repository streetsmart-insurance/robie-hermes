# Cross Sell Verification - September 2026
# Status as of 2026-09-27

> Note-screening only. Final status requires EZLynx issuance evidence.
> Flagged = note contradicts label; pending review, NOT a final rejection.

## Methodology
- Cross Sell = NEW line/policy sold to EXISTING client
- Must verify: (1) client was already a customer, (2) new policy was actually issued
- "App sent" or "shell" = NOT issued, flag
- BOR/renewal = NOT a cross-sell, flag

## STRONG (issued, verify in EZLynx)
1. Taylor Cimei | TOP NOTCH LAWN & LANDSCAPING LLC | "issued new wc policy" - waiting download
2. Taylor Cimei | SK Direct LLC | "issued new WC policy" - waiting download
3. Taylor Cimei | Jeffrey and Theresa Bruder | "New Vacant Express gl was issued"

## NEEDS VERIFICATION (sold/bound, check issuance)
4. Zeus Quezada | MCM Home Services LLC | Sold CAIP PROG 880297566 - waiting download
5. Zeus Quezada | Michael Caruso & Rosemarie Fratta-Caruso | Sold event liability Markel - "will set up"
6. Zeus Quezada | EG Smart Home LLC | Bound with Next Ins - "cross-sell completed"
7. Zeus Quezada | Puma Enterprise LLC | Binder with Tapco
8. Zeus Quezada | Jaguar Tree Service LLC | Sold WC NJCRIB

## WEAK (not issued)
9. Jazmin Molina | Murray King | "sent app for signature" - not issued
10. Jazmin Molina | Elizabeth & Timothy Slavin | "sending app for signature" - not issued
11. Zeus Quezada | J Swat Contracting LLC | "policy shell... wait" - not issued
12. Taylor Cimei | Mansukh Auto Repair Inc | "sent bind request" - not bound

## FLAGGED FOR REVIEW (not a cross-sell)
13. Taylor Cimei | Top Notch Tree Service LLC | "sent updated BOR for renewal" - This is a renewal BOR, NOT a new sale to existing client. Pending review.

## EZLynx PolicyApi Issuance Check (2026-09-27, PRODUCTION API)

**VEP0386394** (Taylor Cimei | Jeffrey and Theresa Bruder) — **Inactive**, CGL, term 2025-08-12 to 2026-08-12. The note says "New Vacant Express gl was issued. Please key in new policy" but cites last year's policy number. FLAGGED: the cited policy number is the old term, not a new issuance. (The "please key in new policy" note suggests the new policy may not be keyed in EZLynx yet — check for the new policy number.)

## Total: 13 rows
## By employee: Zeus 6, Taylor 5, Jazmin 2

## Browser Verification — Batch 1 (2026-09-27 ~21:55 EDT, read-only, signed in as Carlo Ferrara)

- **Murray King** — EXISTING client (since 2021/22). New Foremost dwelling fire 502754335100 ($3,316, placed 9/26/26, PENDING, eff 10/2/26–10/2/27). October-effective, not yet active. Cross-sell label correct; New Customer labels wrong. Agent: Jazmin Molina.
- **Elizabeth & Timothy Slavin** — EXISTING (since 2018). New umbrella to REPLACE cancelled Nationwide umbrella 51291U000067; eSignature still PENDING as of 9/27 — NOT issued, no policy number/carrier/effective date in system. Cross-sell label questionable (replacement of a cancelled line, not new coverage). Agent: Jazmin Molina.
- **MCM Home Services LLC** — EXISTING (since 2024). Progressive commercial auto 880297566 (Active, $3,005, eff 9/22/26–9/22/27). Clean cross-sell; New Customer CSR label wrong. Agent: Zeus Quezada.
- **Michael Caruso & Rosemarie Fratta-Caruso** — EXISTING (since ~2023). Markel event liability 3DS5477 (Active, $106 — note said $111 — eff 2/1/26–2/1/27). Clean cross-sell. Agent: Zeus Quezada.
- **Jeffrey & Theresa Bruder** — EXISTING (since ~2023). Old VEP0386394 (Inactive, 8/12/25–8/12/26) replaced by NEW VEP0440216 (Vacant Express, Active, $433.30, eff 9/4/26–9/4/27, Transaction Date 9/8/26). Issued and active — but it is the same GL line renewed, so the Cross Sell label is questionable. Agent: Taylor Cimei.

## Browser Verification — Batch 2 (running 2026-09-27 ~22:00 EDT)

Top Notch Lawn & Landscaping LLC, EG Smart Home LLC, Puma Enterprise LLC, SK Direct LLC / Mrs. K's Motel & Restaurant, Mansukh Auto Repair Inc.

## Browser Verification — Batch 3 (queued)

Top Notch Tree Service LLC; Jaguar Tree Service LLC prior-client determination (new WC WC533SB27T34016 verified issued 9/3/26 — cross-sell if existing client, new customer if not).
