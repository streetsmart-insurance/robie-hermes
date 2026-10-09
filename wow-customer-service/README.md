# September 2026 WOW Customer Service Incentive Run

## Status: INVESTIGATION IN PROGRESS (all 7 labels analyzed)

### Investigation Results (2026-09-27)
All 7 WOW labels have been analyzed against September data (95 rows):

| Label | Rows | Rejected | Verified | Pending/Weak |
|-------|------|----------|----------|--------------|
| Google Reviews | 8 | 0 | 5 ($50) | 3 ambiguous |
| Autopay Setup | 35 | 6 | 1 | 28 |
| New Customer CSR | 43 | 7 | 0 | 36 |
| Cross Sell | 13 | 4 | 0 | 9 |
| Coverage Enhancement | 5 | 2 | 0 | 3 |
| Referral Won | 4 | 4 | 0 | 0 |
| All Star Call | 46 | 0 | 0 | 46 (40 no call evidence) |

See individual `*_verification.md` files for details.
See `verification-methodologies.md` for proof standards per label.
See `monthly_automation_design.md` for Oct 1 system design.

## Directory Contents
- `september_data_template.csv` - Empty 14-column template matching August format
- `qualify.py` - Qualification script (applies manual rules, detects duplicates)
- `calculate.py` - Payout calculator (100% accuracy tiers, 3-pack rule, topsheet)
- `DATA_PULL_INSTRUCTIONS.md` - Exact browser steps to pull September data from EZLynx

## Workflow
1. **Data Pull** (BLOCKED - needs browser): Follow DATA_PULL_INSTRUCTIONS.md
   - Run EZLynx Looker report 14761 for 9/1/2026–9/27/2026
   - Save as `september_data.csv` in this directory

2. **Qualification**: `python3 qualify.py september_data.csv`
   - Applies Autopay/Google Review/Coverage rules from PROCESS_LEARNED.md
   - Detects duplicates (same account + premium + discussion)
   - Outputs `september_data_qualified.csv` with Manual_Qualified column
   - NCSR, Cross Sell, Referral, All Star rows marked PENDING for manual review

3. **Manual Review**: Resolve all PENDING rows
   - NCSR: Verify premium in EZLynx, confirm new customer
   - Google Review: Check StreetSmart Google Chat for posted review
   - All Star: Read EZLynx discussion for ambiguous duplicates

4. **Calculation**: `python3 calculate.py september_data_qualified.csv`
   - Applies 100% accuracy tiers and 3-pack rule
   - Outputs `topsheet_september.csv` and console summary

5. **Publish**: (Parent agent) Create Google Sheet tabs after review

## 100% Accuracy Rules
- NCSR: $1-3k=$50, $3k-5k=$75, $5k-7.5k=$100, $7.5k-10k=$150, $10k-15k=$250, $15k-25k=$400, $25k-35k=$600
- Coverage Enhancement: $25 per 3-pack (floor(count/3)*25)
- Cross Sell $50 | Referral $25 | Google Review $10 | All Star $5 | Autopay $10/$5
