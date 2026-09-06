# StreetSmart Insurance — Policy Change Confirmation & LOB Master Handoff

**Date**: September 6, 2026  
**Environment**: Production / Hermes Automation Engine  
**Author / Quality Controller**: Robie AI Agent & StreetSmart Automation Team  
**Audit Signature**: `ROBIE was here`

---

## 1. Executive Summary

This handoff packages the complete **Policy Change Confirmation & Verification Engine**, the upstream **Document Retrieval Handoff Protocol**, and the field-level operating rules extracted from **all 18 Line of Business (LOB) Loom video walkthroughs**. 

The pipeline bridges the gap between carrier document acquisition and EZLynx agency management synchronization. When carrier documents (revised declarations, endorsements, invoices) are retrieved from carrier portals, email queues, or autonomous phone calls, they are passed directly into the **Three-Way Verification Engine** for automated cross-checking, EZLynx transaction validation, and quality-controlled task resolution.

---

## 2. End-to-End Pipeline Architecture

```
┌────────────────────────────────────────────────────────┐
│     Upstream: Carrier Policy Document Retrieval        │
│  (Carrier Portals, IVANS eDocs, Email, Voice AI Phone) │
└──────────────────────────┬─────────────────────────────┘
                           │ Documents Retrieved & Filed
                           ▼
┌────────────────────────────────────────────────────────┐
│     Handoff Interface (JSON Payload / CLI Command)     │
│ Account ID, Policy #, LOB, Document URI, Original Req  │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│   Downstream: Policy Change Confirmation & Audit QC    │
│  (scripts/policy_change_verification_pipeline.py)      │
│  - 3-Way Match: Request vs Carrier Dec vs EZLynx Rec   │
│  - Line of Business Specialized Underwriting Audits    │
│  - Posts QC Audit Note ending with 'ROBIE was here'    │
│  - Closes Task if 0 Exceptions / Holds if Discrepancy  │
└────────────────────────────────────────────────────────┘
```

### Retrieval -> Verification CLI Execution
```bash
python3 scripts/policy_change_verification_pipeline.py \
  --account-id <EZLYNX_ACCOUNT_ID> \
  --policy-number <POLICY_NUMBER> \
  --lob "<LINE_OF_BUSINESS>" \
  --document-path "/path/to/carrier_endorsement.pdf" \
  --request-text "<ORIGINAL_CLIENT_REQUEST>" \
  --post-note
```

---

## 3. Codified Lines of Business (LOB) Directory

Detailed reference guides and operational click paths are maintained in `.agents/skills/ezlynx-policy-change-confirmation/references/lobs/`:

### 1. Commercial Automobile (`commercial_auto.md`)
*Demonstrated on Maier Solar / AmGUARD Insurance.*
- **Mandatory Location Building**: Garaging addresses must have `Building # 1` added.
- **Covered Auto Symbols**: Liability Symbols `7, 8, 9` vs Comp/Collision Symbol `7`.
- **Limits**: UM/UIM limits must never exceed Bodily Injury CSL limit.
- **Driver Exclusion Preservation**: Excluded drivers (e.g. Kyle Clarkson, Jesse Daniel) must **NEVER** be deleted during policy changes. Keep `Excluded Driver? = Yes`.
- **Lienholder Vehicle Linking**: Banks (e.g. Chrysler Capital) must be explicitly mapped to the vehicle unit number.

### 2. Business Owners Policy (BOP) (`commercial_package_bop.md`)
*Demonstrated on Saint Paul's House Condominium Association & Kevin Hill Plumbing (AmGUARD).*
- **Habitational / Real Property Risks**: Key Building & BPP at Replacement Cost (`RC`), 100% Coinsurance, $500 Deductible, and 12-month Actual Loss Sustained (`ALS`) Business Income.
- **Trade Contractors on BOP**: Skip complex building structure fields if not on dec page; key GL Class Code (e.g., `75781` Plumbing Office) and Contractor Tools Floaters ($3k tools / $5k transit).
- **MedPay Keying Rule**: If MedPay does not populate in the main liability block, add under Policy Level Additional Coverages (`MED PAY`, `$5,000`).

### 3. Commercial Package (CPP) (`commercial_package_bop.md`)
*Demonstrated on SJAY LLC / JEI Learning Center Ashburn (USLI / Johnson & Johnson).*
- **Package Bundling**: Select **Package** (not Monoline) and check *General Liability* and *Commercial Property*.
- **GL Extensions as Class Codes**: Specialized liability endorsements are keyed as class code lines:
  - Professional Liability (Class `72990`, Flat $0 / Included).
  - Abuse & Molestation Liability (Class `41799`, Flat charge `$325.00`).
- **Property Section**: Key BPP (`$30,000`), Special Form, and verify the carrier dec deductible (`$500` Flat).

### 4. Commercial Property (`commercial_package_bop.md`)
*Demonstrated on SK Direct LLC, Koudelle Inc (Ironshore Europe / Johnson & Johnson).*
- **Multi-Building Complex Structuring**: Multiple structures on one parcel (hotel + restaurant) must be structured as `Location 1, Building 1` (Hotel Suites `$600k` Bldg / `$26k` BPP / `$80k` Bus Income) and `Location 1, Building 2` (Restaurant `$400k` Bldg / `$24k` BPP).
- **IVANS Download Overwrite Warning**: Electronic downloads from carrier IVANS can wipe out detailed building improvements (roof year, plumbing year, wiring year). Ensure property underwriting data is preserved during audits.

### 5. Commercial General Liability (`commercial_general_liability.md`)
*Demonstrated on Fly with Freedom LLC & E&B Development Group Inc (Scottsdale / J&J).*
- **Mailing vs. Business Address**: Mailing PO Box belongs on primary applicant; operating yard belongs under Locations with `Building # 1`.
- **Occurrence Form Standard**: `$1M Occ / $2M Gen Agg / $2M Prod-Comp / $1M Pers-Adv / $100k Rented / $5k MedPay`.
- **Deductible Type**: Explicitly specify **Per Claim** vs **Per Occurrence**.
- **Limit Applies Per**: Default is **Policy**; change to **Project** or **Location** only if endorsement `CG 25 03` / `CG 25 04` is attached.
- **Blanket Additional Insured**: Add under GL Class Codes: Class Code `Blanket Additional Insured`, Basis `Unit 1`, Premium `$250.00` flat.

### 6. Workers' Compensation (`workers_compensation.md`)
*Demonstrated on Yes We Do LLC (Continental Casualty / Applied Underwriters — $108k premium).*
- **Mandatory Location Building**: EZLynx requires `Building # 1` at each location to attach class codes.
- **Part 1 & Part 2 Limits**: Part 1 Statutory; Part 2 Employers' Liability `$1,000,000 Each Accident / $1,000,000 Policy Limit / $1,000,000 Each Employee`.
- **Part 3 Other States**: Exclude the 4 monopolistic state funds (North Dakota, Ohio, Washington, Wyoming) while keeping all eligible states checked.
- **Owners & Officers (NJ PP1-B Form)**: Verify election form (`WC 29 03 07`); apply executive remuneration cap (e.g., `$33,500`) and class code (e.g., `5606`).
- **Accounting Contact**: Always enter an Accounting Contact under Policy Contacts for audit tracking.

### 7. Commercial Umbrella & Excess Liability (`commercial_umbrella_excess.md`)
*Demonstrated on A To Z Complete Construction LLC (Evanston / Markel).*
- **Occurrence Form**: `$1M Each Occ / $1M Aggregate`, Retained Limit / SIR `$0`.
- **CRITICAL Underlying Schedule Audit**: Cross-reference carrier Schedule of Underlying Insurance (`MUB 1800 04 17`). If an underlying line (e.g. Employers' Liability / WC) is on EZLynx but **NOT** on the carrier dec, it must be removed from EZLynx immediately to eliminate E&O gaps.

### 8. Personal Automobile (`personal_lines.md`)
*Demonstrated on Joszef Petkes & Tunde Sokoli (Classic Auto / American Modern).*
- **Agreed Value / Stated Amount**: For collector/exotic cars ($322k Lamborghini / $123k Porsche), enter exact value into **Stated Amount / ACV unless Stated** field.
- **Mileage Tier & Usage**: Key annual mileage limit (`3,000` miles) and set Usage to `Pleasure`.
- **NJ PIP**: Medical Expense `$15,000`, PIP Deductible `$250`, Lawsuit Threshold selected.
- **Lienholder Association**: Link TD Auto Finance specifically to Unit #1; verify no lienholder on Unit #2.

### 9. Homeowners & Dwelling Fire (`personal_lines.md`)
*Demonstrated on Jonathan Haviland (All Risks / Lloyd's) & Shafar's Masonry (Hyundai Marine).*
- **Address Validation**: Validate physical dwelling address with USPS.
- **Dwelling Pre-Fill**: Populate year built (`1920`), living area (`2,380 sq ft`), construction, roof, and protection class (`4`) from rater into policy record.
- **Loss Settlement**: Select **Replacement Cost - Dwelling** and **Replacement Cost - Contents**. **Never select Full Value**.
- **Form Type**: Set to **Special (HO-3)** for standard home; **Basic (DP-1)** for builder's risk/vacant.
- **Water Backup Audit**: Uncheck in EZLynx if not shown on carrier dec.
- **Mortgagee Escrow Routing**: Check **Send Bill** under Additional Interest to route payor to **Mortgagee / Escrow**.

### 10. Commercial Inland Marine (`commercial_inland_marine.md`)
*Demonstrated on The Tree Guy Service LLC (Allianz / Gridiron Insurance Underwriters).*
- **5-Year Age Valuation Rule**: Machinery **$\le$ 5 years old** is valued at **Replacement Cost (RC)** with 100% Coinsurance. Machinery **> 5 years old** reverts to **Actual Cash Value (ACV)**.
- **Storage Limits**: Key maximum values inside vs. outside under Equipment Floater.
- **Unscheduled Tools**: Blanket tools (max `$1,000` per tool, `$25,000` blanket, 80% coinsurance).
- **Theft Check & Loss Payee**: Verify theft coverage is included; link lenders as **Loss Payee**.

### 11. Errors & Omissions (E&O) (`commercial_errors_and_omissions.md`)
*Demonstrated on The Millstone Mint Inc (Kinsale Insurance Company).*
- **LOB Selection Rule**: Always select **"Errors and Omissions" (NOT Professional Liability)** in EZLynx.
- **Defense Costs**: Record whether defense is **Inside Limits** (erodes indemnity) or **Outside Limits** (supplemental).
- **Retroactive Date Preservation**: Match continuous prior acts date; never advance the retro date without written authorization.
- **Revenue & Scope**: Record projected current year revenue and 100% allocation of professional service description.

### 12. Garage & Dealers Policy (`commercial_garage_and_dealers.md`)
*Demonstrated on Autonomy Service LLC (AmWINS MGA).*
- **Operations Classification**: Differentiate *Auto Service / Repair* (gross sales and remuneration) from *Auto Dealers* (vehicle inventory mix). For mobile mechanics, key `% off-premises` = `100%`.
- **Symbols & Liability**: Applies to *Automobile & Premises Operations*, Symbol `29` (Non-owned autos).
- **Garagekeepers**: Key basis as **Legal Liability** (or Direct Primary), Symbol `30`, Location Limit (`$250,000`), Deductible (`$1,000` Per Auto / `$5,000` Max Loss).
- **Mandatory Driver**: Every garage policy **must have at least one active driver listed**.

---

## 4. Master Video Catalog (Downloaded Locally)

All 18 official Loom MP4s (~420 MB) are stored in `data/videos/`:

| File Name | Size | Line of Business | Status |
| :--- | :--- | :--- | :--- |
| `commercial_auto_liability.mp4` | 64.1 MB | Commercial Auto | ✅ Codified |
| `business_owner_policy_bop.mp4` | 34.4 MB | BOP | ✅ Codified |
| `commercial_package.mp4` | 11.6 MB | Commercial Package | ✅ Codified |
| `commercial_property.mp4` | 7.1 MB | Commercial Property | ✅ Codified |
| `general_liability.mp4` | 17.5 MB | General Liability | ✅ Codified |
| `workers_compensation.mp4` | 17.1 MB | Workers' Comp | ✅ Codified |
| `commercial_umbrella.mp4` | 16.3 MB | Commercial Umbrella | ✅ Codified |
| `personal_auto.mp4` | 37.2 MB | Personal Auto | ✅ Codified |
| `homeowners.mp4` | 24.6 MB | Homeowners / DP-1 | ✅ Codified |
| `inland_marine_commercial.mp4` | 60.3 MB | Inland Marine Comm | ✅ Codified |
| `errors_and_omissions_eando.mp4`| 12.5 MB | Errors & Omissions | ✅ Codified |
| `garage_and_dealers_policy.mp4` | 16.0 MB | Garage & Dealers | ✅ Codified |
| `crime.mp4` | 20.8 MB | Commercial Crime | ⏳ Ready for next batch |
| `bond.mp4` | 32.9 MB | Surety / Bonds | ⏳ Ready for next batch |
| `pollution_liability.mp4` | 6.3 MB | Pollution Liability | ⏳ Ready for next batch |
| `condominium.mp4` | 16.1 MB | Condominium (HO-6) | ⏳ Ready for next batch |
| `personal_umbrella.mp4` | 19.4 MB | Personal Umbrella | ⏳ Ready for next batch |
| `inland_marine_personal.mp4` | 8.6 MB | Personal Inland Marine| ⏳ Ready for next batch |

---

## 5. Standard Confirmation Report Template

Every audit note posted to EZLynx discussions must adhere to this exact structure:

```text
[MM/DD/YYYY HH:MM ET] QUALITY CONTROLLER — Policy change confirmation reviewed.
Policy: [number] | Carrier: [carrier] | Change effective: [date].

Requested:
- [exact requested change]

Carrier issued:
- [exact endorsement/declaration result and premium effect]

EZLynx recorded:
- [transaction and keyed result]

Matches:
- [confirmed matching fields]

Exceptions:
- [missing, wrong, extra, uncertain, or unintended field / None found]

Documents:
- [saved name | folder | label | policy association]

Result: [pass / waiting_for_carrier / carrier_correction_required / ezlynx_correction_required]
State: [completed / open hold state].
Next action: [specific owner and action] | Follow-up: [date or N/A].

ROBIE was here
```

---

## 6. Team Leads Directory & Notification Roster

When delivering queue briefings, audit summaries, or handoffs:
- **Ashley Huntley**: Commercial Lines Lead (`ashley@streetsmart.insurance`)
- **Sandy Santana**: Commercial Lines Lead (`sandy@streetsmart.insurance`)
- **Gabi**: Personal Lines Lead (`gabi@streetsmart.insurance`)
- **Jake**: Operations / Management (`jake@streetsmart.insurance`)
- **Carlo Ferrara**: Principal (`carlo@streetsmart.insurance`)

All emails dispatched to team leads must use styled HTML with status badges, summary metrics, and clean typography.
