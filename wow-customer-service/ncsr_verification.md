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

## Total: 43 rows
## By employee: Karla 19, Jazmin 7, Ashley 5, Zeus 4, Taylor 4, Mike 4
