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

## Final NCSR Tally (43 rows)
- Confirmed Active in EZLynx: 15
- Sold, not yet effective: 6
- Notes support credit: 4
- Red flags: 7 (+ 3 API flags = 10)
- Mislabeled (not new business): 2
- Weak/thin: 4
- Ambiguous: 1
- API-flagged old-term/deleted: 3 (Huntley Deleted, Korkowski renewal, Jaguar Tree old policy)

## Total: 43 rows
## By employee: Karla 19, Jazmin 7, Ashley 5, Zeus 4, Taylor 4, Mike 4
