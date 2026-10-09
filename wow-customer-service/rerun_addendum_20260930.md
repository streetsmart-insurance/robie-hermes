# WOW September — Full Re-run Addendum (2026-09-30)

> Re-pulled EZLynx Shared Report 14761 for 2026-09-01 → 2026-09-30 and
> re-verified every label against the 9/27 baseline. Read-only except
> dashboard filter selections; no EZLynx records modified.

## Pull notes (methodology correction)

The first re-pull returned only 4 rows because the dashboard's Activity Type
filter was set to "Task Specific Note or Task or Note". The 9/27 baseline
(raw: `september_raw.csv`, 95 rows) used "Task Note" (90), "Task Creation
Note" (4), "Note" (1). After correcting Activity Type to
"Note or Task Note or Task Creation Note" and Labels to the 7 WOW labels,
the pull returned **119 rows** (fresh CSV in browser downloads,
2026-09-30 ~18:06 UTC). Baseline comparison is valid: 95 → 119.

## Per-label counts

| Label | 9/27 | 9/30 | Change |
|---|---|---|---|
| New Customer CSR | 43 | 49 | +6 |
| AutoPay Setup | 35 | 47 | +12 |
| All Star Call | 46 | 52 | +6 |
| Cross Sell | 13 | 16 | +3 |
| Google Review | 3 | 8 | +5 |
| Referral Won | 4 | 5 | +1 |
| Coverage Enhancement | 5 | 5 | 0 |

29 new rows total (some multi-labeled). 7 baseline rows no longer carry WOW
labels; 3 of those carried "All Star Call" but are superseded by same-account
relabels (no verified credit lost — All Star was note-screening only).
All previously-verified rows are still present with labels intact.

## New-row dispositions

### New Customer CSR (+6)
- **Baldegram, LLC** (Zeus, 9/28) — "Sold CAIP with PROG at $1700 premium
  with Pol # 880594636". PolicyApi: **880594636 Active, eff 9/29/26, NBS,
  AUTOB, $1,739** — September issuance VERIFIED. Row carries both New
  Customer CSR and Cross Sell labels (contradictory). **PENDING
  attribution**: cross-sell if Baldegram was already a client gaining a new
  CAIP line; NCSR if new client. Needs browser prior-client check.
- **Kamal & Pragati Sinha** (Karla, 9/25), discussion 847874224:
  - Plymouth Rock Auto **PAA80002252993**, "PAID IN FULL", $4,348.
    PolicyApi: Inactive, eff **10/1/26**, NBS, Direct. → OCT-EFFECTIVE
    (Carlo's call). PAID IN FULL → AutoPay FLAG.
  - Cabrillo Home **NJH1031620**, $3,724, "Mortgage billed". PolicyApi:
    Inactive, eff **10/1/26**, NBS, HOME, Direct, txn 9/29/26. →
    OCT-EFFECTIVE (Carlo's call). Mortgage billed → AutoPay FLAG.
- **Paul & Maryann Cabrera** (Karla, 9/25) — Umbrella **6193199613117**,
  $484, "payment on 10/04, as authorized". PolicyApi: Inactive, eff
  **10/4/26**, PUMBR, NBS, txn 9/29/26. → OCT-EFFECTIVE (Carlo's call).
  AutoPay Setup label on this row: WEAK (one-time authorized payment,
  no autopay/EFT claim).
- **Sunflower Maid Services LLC** (Taylor, 9/9) — "issued new Gl policy
  set insued up on auto pay", no policy number. NCSR: CLAIM (no issuance
  evidence). AutoPay: CLAIM (portal proof needed).
- **William Sieber** (Carlo Ferrara, 8/27) — "LPR sent to allstate as
  well | please confirm it downloads and close out to do onboarding".
  NCSR: REJECTED/WEAK (no sale, no policy). AutoPay: WEAK (no autopay
  mention).

### Cross Sell (+3)
- **Rachel Edmond-Millington** (Taylor, 9/29) — "Hi Jazmin, the insured
  emailed looking for a quote. Can you please reach out to her?" →
  WEAK (quote-request handoff, no sale/issuance).
- **Baldegram, LLC** — issuance verified (above). PENDING under Carlo's
  newly-controlled-line rule (browser prior-client check needed).
- **SLH GENERAL CONTRACTORS LLC** (Erika, 7/10) — "Insured agreed on
  moving his vehicle with us. Could you please call the insured for
  details?", task reassigned → WEAK (no policy, no issuance evidence).

### AutoPay Setup (+12)
- FLAG (note contradicts label): Sinha Cabrillo ("Mortgage billed"),
  Sinha Plymouth Rock ("PAID IN FULL").
- WEAK: Cabrera Umbrella (no autopay mention); Sieber (no autopay
  mention); KP Drywall (Erika — renewal payment status, carrier renewal
  billing, task reassigned); NASH (same incomplete "working on binding"
  note as 9/27).
- CLAIM (portal proof pending/blocked): Sunflower ("set insued up on
  auto pay"); Marucci Home Improvements (Erika — "EFT was properly set
  up"); ALTI TRANSPORT (Eunice — "EFT signed and sent to Progressive");
  1000 Monroe Inc (Eunice — "Auto-pay enrollment done", $942.24
  processing); Cocoa Beach Tanning (Eunice — "autopay set up"; note is
  renewal payment processing **and contains a privnote.com payment link
  — data-hygiene flag**); Jimenez White Glove (Eunice — "auto pay set
  up", $98.38/mo); Hydrodynamics Lawn Sprinklers (Erika — "Payment
  received 09/23/2026, amount due: $2445.59 | EFT ENROLLED").

### All Star Call (+6 keys; Magellan data still unavailable)
- Call evidenced in note (quality check pending): **Chawki & Lody Azar**
  (Alejandro — detailed recap: $1,777 FOS payment, reinstatement,
  contact update; policy HONJM06241) — genuinely new.
- WEAK (no call evidence): Mary Florence (Alejandro — internal handoff
  "Hi Karla, this insured is looking to get an Auto quote…", no call by
  Alejandro); Baldegram (sales note only); Sinha (quote/email
  discussion).
- Grafer 845839239 and Corchado 841772774 have AI-Notes summaries but
  are same-account relabels of dropped baseline rows.

### Google Review (+5)
- NEEDS-REVIEW-CHECK (cannot browse GBP): Marvin Calderon (Jazmin, 9/26
  — note pastes full review text from "Marvin Menjivar" naming
  **"Mr. Zeus Quezada"** and **"Ms. Jazmín Molina"** — per Carlo's rule
  credit goes to the named; attribution question: Zeus + Jazmin, not
  the label holder); Sinha ("He left me a Google Review", Karla);
  Cabrera ("They left me a Google review", Karla); Theresa Cutillo
  ("She left me a Google Review", Karla).
- REJECTED: Gus Martinez (Alejandro — note describes no review:
  "I called the insured and it went to VM… asking to have this
  completed for us").

### Referral Won (+1)
- **Marvin Calderon** (Zeus, no discussion ID) — "Adding Referral
  label" → UNVERIFIED (no referral link/detail; needs Nicole's weekly
  report check).

### Coverage Enhancement (5 = 5, identical rows)
Same 5 discussion IDs as baseline. New in pull: 3 discussions labeled
**"Coverage Enhancement-Technician"** (Pristas 847101842, Rizzo
846762076, Iodice 844080096 — all Daniela Aguilar), a different label
from the WOW one. Open question for Alejandro/Carlo whether the
-Technician variant counts.

## Updated credit ranges (Carlo's rulings applied)

- **NCSR**: min **28** confirmed (unchanged — no new September-effective
  verified credit). Max **38** (was 35): 28 + 3 baseline Oct-effective
  (Cabrera Home/Auto, Gangwar Home) + 2 pending Alejandro (1812 #38,
  Mendieta #39) + 1 Baldegram (if new client) + 3 new Oct-effective
  (Sinha PR Auto, Sinha Cabrillo Home, Cabrera Umbrella) + 1 Sunflower
  (if September issuance confirms).
- **Cross Sell**: clean **10** (unchanged). Max **11** if Baldegram proves
  to be an existing client gaining a new CAIP line (else 0 cross-sell
  and +1 NCSR). Murray King stays October; Slavin stays questionable;
  Bruder rejected.

## Still blocked

- Alejandro: rows 38/39, coverage three-enhancement rule, September
  Magellan data, referral confirmations (Pender attribution,
  Huntley/Calhoun, Calderon), portal credentials (Bristol West, LM,
  Plymouth Rock). Nudged 9/30; no reply as of 9/30 ~14:00 EDT.
- Carlo: October-effective calls — now **6** policies (3 baseline:
  Cabrera Home/Auto, Gangwar Home; 3 new: Sinha PR Auto, Sinha Cabrillo
  Home, Cabrera Umbrella). Kelso timing flag stands.
- Browser-needed: Baldegram prior-client status; Rachel/SLH account
  history (both WEAK on notes alone).
- Data hygiene: new privnote.com payment URL in the Cocoa Beach
  AutoPay note (repo-wide privnote sweep still open).
