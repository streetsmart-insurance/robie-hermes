# New Customer CSR Verification - September 2026
# Status as of 2026-09-27

> Note-screening only. Final status requires new-business reports, sales
> center reports, and carrier downloads (per Carlo).
> Red flags = note screening, pending authoritative check, NOT final rejections.

## Methodology (per Carlo)
- Check new business reports
- Check sales center reports  
- Check carrier downloads
- Policy must be ISSUED, not just sold/bound/quoted
- "Sold" without policy number = weak claim
- "Bound" or "waiting on download" = not issued yet, flag for review

## Red flags (not issued yet - pending authoritative check)
1. Jazmin Molina | Murray King | "I sent in the app for signature" - APP SENT, NOT ISSUED
2. Jazmin Molina | Daniel Chando | "Sold the policy" - NO DETAILS, NO POLICY NUMBER
3. Zeus Quezada | J Swat Contracting LLC | "sent app, set up policy shell... wait for esign" - SHELL ONLY
4. Mike Sosa | JOSE EFRAIN FLORES QUINONES | "coverage bound... waiting on the download" - BOUND, NOT DOWNLOADED
5. Zeus Quezada | Puma Enterprise LLC | "Binder and invoice, policy bound" - BOUND, NOT ISSUED
6. Mike Sosa | DREAMS BUILT LLC | "coverage bound... putting the app together" - BOUND, APP INCOMPLETE

## With policy numbers (verify issuance in EZLynx)
- Ashley Huntley | Tadeusz & Mariloa Stopka | 879834477 (Progressive)
- Ashley Huntley | Ashley & Troy Huntley | 13579_83445947 (Travelers)
- Ashley Huntley | Cheryl Mendelson | 6192642562061 (Travelers)
- Ashley Huntley | Kevin Calhoun | 6192966482061 (Travelers)
- Ashley Huntley | Jordan Kelso | HONJ052827 (FOS)
- Jazmin Molina | Dennis Korkowski | CDNJ004781 (FMI)
- Taylor Cimei | 1812 Marketing Corp | ESP0440988661
- Zeus Quezada | Jaguar Tree Service LLC | R2WC555848

## EZLynx PolicyApi Issuance Checks (2026-09-27, PRODUCTION API)

### CONFIRMED Active (new September issuances)
1. **879834477** (Ashley Huntley | Tadeusz & Mariloa Stopka) — Active, eff 2026-09-12, AUTOP. Also verified in Progressive portal with EFT autopay.
2. **6192642562061** (Ashley Huntley | Cheryl Mendelson) — Active, eff 2026-09-14, AUTOP
3. **HONJ052827** (Ashley Huntley | Jordan Kelso) — Active, eff 2026-08-31, HOME
4. **6192966482061** (Ashley Huntley | Kevin Calhoun) — Active, eff 2026-09-23, AUTOP
5. **ESP0440988661** (Taylor Cimei | 1812 Marketing Corp) — Active, eff 2026-09-16, CPKGE

### FLAGGED (policy number does not support a new September issuance)
6. **13579_83445947** (Ashley Huntley | Ashley & Troy Huntley) — status **Deleted**, FLOOD, eff 2026-09-03. Policy was deleted from EZLynx.
7. **CDNJ004781** (Jazmin Molina | Dennis Korkowski) — **Inactive**, DFIRE, term 2025-05-01 to 2026-05-01. Last year's policy; September notes describe a renewal ("sent the signed app to FMI", "took the yearly payment"), not new business.
8. **R2WC555848** (Zeus Quezada | Jaguar Tree Service LLC) — **Inactive**, WORK, eff 2024-02-24, **cancelled 2024-04-29**. A 2024 cancelled policy cited in a September NCSR note — wrong/old policy number.

## EZLynx PolicyApi — Policy Numbers from Notes (2026-09-27, PRODUCTION API)

### CONFIRMED Active (8)
1. **G01-9303024-00** (Karla Brown | Danny Dominguez Alvarado, Bristol West) — Active, eff 2026-09-25, AUTOP
2. **PAA80002252328** (Karla Brown | Jennifer Pinkowsky, Plymouth Rock) — Active, eff 2026-09-24, AUTOP
3. **880297566** (Zeus Quezada | MCM Home Services LLC, Progressive) — Active, eff 2026-09-22, AUTOB
4. **879499436** (Karla Brown | Elaine McDermott, Progressive) — Active, eff 2026-09-09, AUTOP
5. **K4540928** (Karla Brown | Jackie & Shane Pender, LM) — Active, eff 2026-09-10, AUTOP
6. **2037527327** (Karla Brown | Michael Gutierrez & Melissa Torres, NatGen) — Active, eff 2026-09-02, HOME
7. **PAA80002251414** (Karla Brown | Jennifer & Brian Olenick, Plymouth Rock Auto) — Active, eff 2026-09-03, AUTOP
8. **NJH00002220171** (Karla Brown | Jennifer & Brian Olenick, Plymouth Rock Home) — Active, eff 2026-09-08, HOME

### SOLD, NOT YET EFFECTIVE (6) — Inactive status, future effective date; sold in September, takes effect later
9. **619315196-633-1** (Karla Brown | Paul & Maryann Cabrera, Travelers Home) — Inactive, eff 2026-10-04, created 2026-09-25
10. **619315117-206-1** (Karla Brown | Paul & Maryann Cabrera, Travelers Auto) — Inactive, eff 2026-10-04, created 2026-09-25
11. **NJA198092** (Karla Brown | Raghavender & Archana Gangwar, Progressive Home) — Inactive, eff 2026-10-03, created 2026-09-22
12. **DPNJ2026090016-26** (Karla Brown | Michael Swartz, Hyundai) — Inactive, eff 2026-09-30, created 2026-09-24
13. **DPNJ2026090015-26** (Karla Brown | Michael Swartz, Hyundai) — Inactive, eff 2026-09-30, created 2026-09-24
14. **6192983786331** (Karla Brown | Richard & Lindsay Guarini, Travelers Home) — Inactive, eff 2026-09-28, created 2026-09-22. (Note wrote it with dashes as 619298378-633-1; EZLynx stores it without dashes.)

### CORRECTIONS (2026-09-27 — earlier flags were wrong, dash formatting in search)
- **K4547767** (Karla Brown | Rebecca Gallman, LM) — was flagged Deleted; recheck found the live record **Active**, eff 2026-09-17. (A duplicate K4547767_83811082 is Deleted; the real policy is Active.) CONFIRMED, not flagged.
- **6192380252061** (Karla Brown | Lauren Pender, Travelers Auto) — was flagged not-found; recheck without dashes found it **Active**, eff 2026-09-13. CONFIRMED, not flagged.

### FLAGGED (3, verified)
15. **13579_83445947** (Ashley Huntley | Ashley & Troy Huntley, Flood) — **Deleted** (only record; no active variant found).
16. **CDNJ004781** (Jazmin Molina | Dennis Korkowski) — **Inactive**, DFIRE, term 2025-05-01 to 2026-05-01. Last year's policy; September notes describe a renewal, not new business.
17. **R2WC555848** (Zeus Quezada | Jaguar Tree Service LLC) — **Inactive**, WORK, eff 2024-02-24, cancelled 2024-04-29. Wrong/old policy number in a September note.

## NCSR Scorecard (EZLynx PolicyApi evidence, corrected 2026-09-27)
- Confirmed Active: 15 (5 from Policy Number column + 10 from notes, incl. Gallman K4547767 and Pender 6192380252061 corrections)
- Sold, not yet effective: 6
- Flagged: 3 (Deleted x1, Inactive old-term x2)
- Still need review (no policy number found): ~19

## Rows Without Policy Numbers — Note Screening (2026-09-27)

The PolicyApi has no applicant-name search (`search_policy_by_number` only), so these 18 rows were screened on note content alone.

### RED FLAGS — app sent / shell / bind request, not issued (7)
1. **Murray King** (Jazmin Molina) — "I sent in the app for signature." App sent, not issued.
2. **Marvin Calderon** (Jazmin Molina) — "I went with Progressive... We took the full 6 month payment of $1671. Waiting for the signature." Payment taken, app unsigned.
3. **J Swat Contracting LLC** (Zeus Quezada) — "sent app, set up policy shell in ez... just need to wait for esign to be completed to upload it to the natgen's site." Shell only, not bound.
4. **John & Lisa Sabo** (Karla Brown) — "I set up signatures through Cabrillo. Please watch for the app to be signed... Watch for download." Not signed, not downloaded.
5. **DREAMS BUILT - BY DESIGN LLC** (Mike Sosa) — "coverage bound through BiBerk putting the app together send to the client for e-signatures." Claims bound but app not yet signed — contradictory.
6. **Xerion Architectural Services PC** (Taylor Cimei) — "I sent bind request to Novate. Please follow up for binder and key in new policy." Bind request sent, not bound.
7. **NASH RESIDENTIAL SERVICES LLC DBA HOGANS' TRANSFER** (Mike Sosa) — "Payment details provided... Working on binding coverage through RLIG." Working on binding, not bound.

### MISLABELED — coverage added to existing policy, not new business (2)
8-9. **Sarah & Nicholas Lupano** x2 (Karla Brown) — "The umbrella was approved and added on to the policy" / "Added Umbrella to their home policy $170.55 premium." Existing client, added coverage — not a new customer.

### WEAK / THIN — no issuance evidence (4)
10. **GREGORY JOHNSON** (Jazmin Molina) — "I sent out the proof of insurance for his parents, and the insured signed everything." Signed, but no carrier or policy details.
11. **Clara Perez Cuautle & Ignacio Peralta** (Jazmin Molina) — "Finalized. I'll keep an eye on the payment, and the app." Says finalized while payment and app are still pending — contradictory.
12. **Daniel Chando** (Jazmin Molina) — "Sold the policy." Two words, no details.
13. **MENDIETA O. CONSTRUCTION LLC** (Mike Sosa) — "they were ready to move forward with the quote... took payment info." Payment info taken, no bind or issue mentioned — reads like a quote, not a sale.

### SUPPORTS CREDIT — bound/issued per notes (4)
14. **Raghavender & Archana Gangwar** auto (Karla Brown) — "Sold Progressive auto $2158-12 months Paid in full... I shared his receipt, ID card, and Dec page." Dec page + ID cards shared = issued.
15. **1812 Marketing Corp** CNA/Amtrust (Taylor Cimei) — "issued CNA GL & Umbrella + Amtrust WC. Insured asked to pay in full." Says issued; no numbers to API-check.
16. **JOSE EFRAIN FLORES QUINONES DBA F & Q TRANSPORT** (Mike Sosa) — "coverage bound through Geico waiting on the download." Bound, pending EZLynx download.
17. **1812 Marketing Corp** cyber (Taylor Cimei) — "recieved professional/cyber policy, sent bind request, keyed in new pol. Insured made payment in full. Please follow up for binder and invoice." Contradictory (policy received vs bind request sent); payment made. AMBIGUOUS.

## Drill-Down: Document Evidence (2026-09-27)

Used DiscussionApi `get_discussion` → applicant ID → DocumentApi `search_applicant_documents`. Document records carry no date field; recency inferred from document ID sequence (820M-823M = September 2026) plus September "New Business - Won" discussion titles. Where a dec names an effective date, that is quoted literally.

### RESOLVED — red/weak flags with issuance documents on file (7)
1. **Murray King** (was: app sent, not issued) — "502202728900 - Declaration page.pdf" + "MURRAY KING DEC.pdf" + "Please Sign-E" on file. Dec page exists.
2. **Marvin Calderon** (was: waiting for signature) — "Completed eSignatures" + "ID Cards Marvin.pdf" + "Proof of Insurance (Binders/Evidence of Insurance)". Signature completed, ID cards issued.
3. **GREGORY JOHNSON** (was: weak) — "DECLARATIONS EFFECTIVE 09232026 114 PM". Dec effective 9/23/2026 — definitive September issuance.
4. **Daniel Chando** (was: weak, "Sold the policy.") — "Chando Sign.pdf" + "Proof of Insurance (Binders/Evidence of Insurance)". Signed + proof of insurance.
5. **Clara Perez Cuautle & Ignacio Peralta** (was: weak) — "CLARA PEREZ POLICY.pdf" + "New Business Package". Policy doc exists.
6. **John & Lisa Sabo** (was: app not signed) — "Sabo Signed Cabrillo App.pdf" + "39 Linda Rd Cabrillo Home Declarations.pdf" + "NJH1031418 - Cabrillo Requirements Met Notice". App signed, dec issued.
7. **Xerion Architectural Services PC** (was: bind request, not bound) — "Travelers Binder 2026-2027.pdf" + "Travelers - Professional Liab Binder 2026-2027.pdf" + "Completed eSignatures". Bound (with Travelers, not Novatae).

### CONFIRMED STRONG — supporting notes now backed by documents (2 accounts)
8. **Raghavender & Archana Gangwar** — "Gangwar Progressive Auto Declarations.pdf" + "Gangwar Progressive Auto Application.pdf" + "Raghavender Progressive Auto Receipt.pdf" + "Raghavender ID cards.pdf" + "14 Carlisle Ct Progressive Home Declarations.pdf" + "14 Carlisle Ct Progressive Home Receipt.pdf". Both auto and home issued.
9. **1812 Marketing Corp** — "CFC - POLICY ESP0440988661.pdf" (cyber, API-confirmed Active) + "Apogee - Comm pkg 2026-2027 Binder.pdf" + "Amtrust WC Policy 26-27.pdf" + "Certificate of Liability Insurance 26-27". Three policies documented. (This also resolves the "ambiguous" cyber row — same applicant.)

### STILL FLAGGED (4)
10. **Ashley & Troy Huntley** — flood policy 13579_83445947 is Deleted. Note describes a Travelers AUTO policy but the Policy Number column holds the deleted flood number; the real Travelers auto number is unknown. Applicant docs show Progressive auto (pol871740154) and Safeco renters only — no Travelers auto. Need the real policy number.
11. **Dennis Korkowski** — "New Business Package" / "New Business Korkowski" docs exist, but cited policy CDNJ004781 is last year's (2025-2026 term). Rewrite vs renewal unclear. Discussion titled "Dwelling Fire Download Renewal / Mortgage Verification."
12. **Jaguar Tree Service LLC** — R2WC555848 cancelled 2024. No discussion ID in CSV; cannot check docs.
13. **J Swat Contracting LLC** (borderline) — "2024 FORD F350 SUPER DUTY DEC PAGE 2026.pdf" + "Dec Pages.pdf" + "Master Certificate of Liability Insurance 26-27" exist, but cannot tie definitively to the September NatGen note vs an existing policy. Likely converted, not conclusive.

### CONFIRMED MISLABELED (1 account, 2 rows)
14. **Sarah & Nicholas Lupano** x2 — "Lupano_FMI_Dec_page_with_Umbrella.pdf" + "Lupano FMI Umbrella Full policy.pdf" on existing Plymouth Rock policy PAA80002251092. Umbrella added to existing client — not new business. (Discussion titled "Umbrella added to FMI".)

### NO DOCUMENT ACCESS — no discussion ID in CSV (4)
15. **DREAMS BUILT - BY DESIGN LLC** (Mike Sosa) — red flag stands on note alone.
16. **MENDIETA O. CONSTRUCTION LLC** (Mike Sosa) — weak stands on note alone.
17. **JOSE EFRAIN FLORES QUINONES DBA F & Q TRANSPORT** (Mike Sosa) — note says bound via Geico, waiting on download; no docs to confirm.
18. **NASH RESIDENTIAL SERVICES LLC DBA HOGANS' TRANSFER** (Mike Sosa) — red flag stands on note alone.

## Revised NCSR Tally (43 rows, after document drill-down)
- Confirmed Active in EZLynx (PolicyApi): 15
- Sold, not yet effective: 6
- Resolved by document evidence: 7
- Strongly confirmed by documents: 2 accounts (Gangwar, 1812 Marketing)
- Still flagged: 4 (Huntley, Korkowski, Jaguar Tree, J Swat borderline)
- Confirmed mislabeled: 1 account (Lupano x2)
- No document access: 4 (all Mike Sosa rows)

## Total: 43 rows
## By employee: Karla 19, Jazmin 7, Ashley 5, Zeus 4, Taylor 4, Mike 4
