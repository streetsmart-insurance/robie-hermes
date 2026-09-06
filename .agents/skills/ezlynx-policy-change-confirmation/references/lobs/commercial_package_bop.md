# Business Owners Policy (BOP) & Commercial Package SOP

## Line of Business Overview
BOP and Commercial Package policies combine property coverage (Building, Business Personal Property - BPP, Business Income) and liability coverage into a unified contract. Policy changes frequently involve limit modifications, adding mortgagees / loss payees, changing physical locations, or adding specialized property endorsements.

---

## 1. Primary Change Types & Required Data Elements

### A. Property Limits & Valuations
- **Building Coverage**: Replacement Cost (RC) vs. Actual Cash Value (ACV).
- **Business Personal Property (BPP)**: Inventory, equipment, stock, tenant improvements & betterments (TIB).
- **Business Income & Extra Expense**: Actual Loss Sustained (ALS) or specified monthly limitation (e.g., 1/3, 1/4, 1/6) or dollar limit.
- **Co-insurance Requirement**: Typically 80%, 90%, 100%, or Agreed Value. If limits change, verify adequacy to avoid co-insurance penalties at loss.
- **Deductibles**: Standard property deductible ($1,000, $2,500, $5,000), Wind/Hail deductible, Water Damage deductible.

### B. Mortgagee & Loss Payee Clauses
- **Mortgagee (Real Property / Buildings)**: Must include loan number, exact corporate mortgagee name, and billing/notice address.
- **Loss Payee (Personal Property / Financed Equipment)**: Lender's Loss Payable endorsement (`CP 12 18`).
- **Contract of Sale / Landlord Additional Insured**: Building owner where tenant is named insured.

### C. Common Property Endorsements
- **Water Backup & Sump Overflow**: Stated limit ($10,000, $25,000, $50,000).
- **Spoilage / Temperature Change**: Perishable goods, restaurant food stock.
- **Equipment Breakdown / Boiler & Machinery**: Built-in or separate sub-limit.
- **Protective Safeguards**: Warranties for central station burglar/fire alarms, sprinkler systems (P-1, P-2, P-9 symbols). Must verify compliance before adding/removing.

---

## 2. Three-Way Verification Matrix (BOP/Package)

| Element | Client Request / Ticket | Carrier Endorsement / Dec | EZLynx Policy Record |
| :--- | :--- | :--- | :--- |
| **Building / BPP Limits** | Requested dollar limit | Property declarations schedule | Coverage limits tab |
| **Mortgagee / Loss Payee** | Lender letter / Loan # | Schedule of Mortgagees | Additional Interests tab |
| **Deductibles** | Requested deductible | Endorsement decs | Policy deductible fields |
| **Effective Date** | Closing date or request date | Endorsement effective date | Transaction effective date |
| **Premium Change** | Quoted AP / RP or $0 | Endorsement premium ($ AP/RP) | EZLynx Transaction premium |

---

## 3. Major Carrier Specifics

### Franklin Mutual Insurance (FMI)
- **Email for Underwriting**: `clunderwriting@fmiweb.com`
- **Mailing Address & Location Updates**: Verify dwelling or commercial property address vs. insured billing address.
- FMI requires written confirmation or underwriter sign-off for mid-term limit changes over threshold.

### Guard (Berkshire Hathaway) / Selective / Utica First
- Fast turnaround on mortgagee/loss payee additions via agent portal.
- Always download updated decs page and attach to the EZLynx document library under `Policy Changes/Declarations`.

---

## 4. Video SOP: Step-by-Step EZLynx Policy Entry & Endorsement Rules

> [!NOTE]
> **Source Walkthrough**: [Business Owner Policy Entry Loom Walkthrough](https://www.loom.com/share/dc0d74328429498183e342992fcb4aa7) demonstrated on *Saint Paul's House Condominium Association LLC* (`SABP019166`) and *Kevin Hill Plumbing & Heating* (`KEBP129978`) with AmGUARD / InterGUARD.
> **User Guidance**: Do NOT dwell on or require SIC codes during policy entry / audit.

### Step 1: Policy Info & Legal Entity Verification
- Verify Named Insured, DBA, and Mailing Address against the Carrier Declarations Page.
- Confirm Legal Entity (LLC, Sole Proprietorship, Corporation).
- Written Premium, Term, Billing Type (Direct vs Agency), and Commission (typically 15%).

### Step 2: Locations & Mandatory Buildings
- Every location entered **must have a building added** (`Building # 1`).
- For single-location risks, verify `Check if Primary Premises` is checked.
- Policy Type: Typically written on `Special Form`.

### Step 3: Policy-Level Liability Coverages
- **Standard BOP Limits**:
  - Bodily Injury & Property Damage (Occurrence): $1,000,000.
  - General Aggregate: $2,000,000.
  - Products & Completed Operations: $2,000,000.
  - Personal & Advertising Injury: $1,000,000 (or enter `Include`).
  - Medical Expense (Per Person): $5,000.
- **MedPay Keying Rule**:
  - If Medical Payments does not populate in the main liability block, add it under **Liability - Policy Level Additional Coverages** with Coverage Code `MED PAY`, Limit `$5,000`.

### Step 4: Property Entry — Building/Condo vs Contractor Differences
The entry workflow diverges significantly depending on the nature of the business:

#### Case A: Real Property & Habitational / Condos (e.g., Saint Paul's House Condo)
- Navigate to **Premises Information -> Location 1 -> Building 1 -> Edit**:
  - **Building Coverage**: Key exact dec page limit (e.g., $458,003), Valuation = Replacement Cost (`RC`), Deductible = $500, Coinsurance = 100% (or Agreed Value).
  - **Business Personal Property (BPP)**: Key limit (e.g., $10,000), Valuation = `RC`, Deductible = $500.
  - **Business Income**: Actual Loss Sustained (`ALS`), # of Months = `12`.
  - **GL Class Code**: Enter class code from dec page (e.g., `69145` - Condominium Residential Association Risk Only).

#### Case B: Trade Contractors on BOP (e.g., Kevin Hill Plumbing & Heating)
- **Do Not Force Building / BPP**:
  - Contractors typically do **not** have building coverage and minimal BPP. Do not waste time trying to fill out complex building structure/construction fields if not on the dec page.
- **GL Class Code & Exposure**:
  - Enter class code (e.g., `75781` - Plumbing Office).
  - Premium Basis: Select `Payroll` (Exposure: `0` or flat if rated on office/minimum).
- **Contractor Tools & Floaters**:
  - In dec page "Contractors' Installation, Tools and Equipment Coverage" ($3,000 Blanket Tools, $5,000 per unscheduled tool):
  - Enter under **Property - Additional Coverages (Policy Level)**:
    - Floaters -> **Contractor's Equipment**: `$3,000`.
    - Floaters -> **Transit**: `$5,000`.

### Case C: Commercial Package Policy (CPP) Multiline Bundling
> [!NOTE]
> **Source Walkthrough**: [Commercial Package Loom Walkthrough](https://www.loom.com/share/50e461126ea5412fa87289ab06cd3469) demonstrated on *SJAY LLC and WU Family Investments LLC DBA JEI Learning Center Ashburn* (`CP1731905`, USLI / Johnson & Johnson).

- **Package Conversion & Line Bundling**:
  - A Commercial Package policy combines multiple lines into a single master policy.
  - In EZLynx, when converting an application or adding a policy, select **Package** (not Monoline), then check the included lines (**General Liability** and **Commercial Property**). Additional lines (like Inland Marine, Commercial Auto, Crime) can also be toggled.
  - Policy summary displays distinct sub-sections: `Details - Line of Business: COMMERCIAL PROPERTY` and `Details - Line of Business: GENERAL LIABILITY`.
- **General Liability Extensions as Class Codes**:
  - Primary Class: `47474` (Schools - Tutoring Centers - Other than Not-For-Profit), Exposure: Gross Sales `$15,000`.
  - Specialized endorsements (Professional Liability, Abuse & Molestation, EPLI) are keyed directly as class code lines under the GL section:
    - Professional Liability: Class `72990`, Premium Basis: `Unit 1`, Premium: `$0 / Included`.
    - Abuse and Molestation Liability: Class `41799`, Premium Basis: `Unit 1`, Premium: `$325.00` (flat endorsement charge).
- **Commercial Property Section**:
  - Subject of Insurance: **Business Personal Property (BPP)** `$30,000` limit, Valuation = Replacement Cost (`RC`), Coinsurance = `100%`, Cause of Loss = `Special`, Deductible = `$500` Flat (verify carrier dec rather than leaving default $1,000).
  - Building details: Distance to Hydrant `1,000 ft`, Distance to Fire Station `2 miles`, Construction Type `Joisted Masonry`, Year Built `2000`, Area `1,200 sq ft`.

---

### Case D: Commercial Property (Multi-Building Complex Rules)
> [!NOTE]
> **Source Walkthrough**: [Commercial Property Loom Walkthrough](https://www.loom.com/share/48d74e22a77a481d8268fe30c6947376) demonstrated on *SK Direct LLC, Koudelle Inc* (`JTA1500394`, Ironshore Europe / Johnson & Johnson).

- **Multi-Building Structuring at a Single Location**:
  - When multiple structures exist on the same parcel (e.g., hotel building + detached restaurant building):
    - Do NOT create separate locations for each building if they share the same physical address.
    - Structure them as `Location 1, Building 1` and `Location 1, Building 2`.
- **Subject of Insurance Allocation**:
  - **Building 1 (Hotel Suites, 11-30 Units)**:
    - Building (`BLDG`): `$600,000`, RC, Special, `$1,000` Flat Deductible, Wind/Hail Deductible (`WHDED`).
    - Business Personal Property (`BPP`): `$26,000`, 100% Coinsurance, RC, Special, `$1,000` Flat.
    - Business Income (`BUSIN`): `$80,000`, 100% Coinsurance, RC, Special, `$1,000` Flat.
  - **Building 2 (Restaurant with Cooking)**:
    - Building (`BLDG`): `$400,000`, RC, Special, `$1,000` Flat Deductible, Wind/Hail Deductible.
    - Business Personal Property (`BPP`): `$24,000`, RC, Special, `$1,000` Flat.
- **CRITICAL IVANS Download Overwrite Warning**:
  - When an electronic policy download arrives via IVANS from a carrier/MGA, it can sometimes overwrite or wipe out detailed building improvements (roof year, plumbing year, wiring year, heating year).
  - During policy change audits, verify that detailed property underwriting data has not been cleared by an incoming download transaction.

---

## 5. Discussion & Closing Protocol
- **Activity Note**: Always note: `"BOP/Package Endorsement verified: [Limit Changed / Mortgagee Added / Location Updated]. New Limits: [Bldg $XX, BPP $XX]. Mortgagee: [Name & Loan #]. Premium AP/RP: [$XX]. Decs attached. Robie was here"`


