# Commercial Auto (CA) Policy Change & Endorsement SOP

## Line of Business Overview
Commercial Automobile policies cover business-owned vehicles, hired/non-owned autos, and associated drivers. Policy changes are predominantly vehicle additions/deletions, driver additions/exclusions, limit increases, or garaging address adjustments.

---

## 1. Primary Change Types & Required Data Elements

### A. Vehicle Additions
- **VIN (Vehicle Identification Number)**: Must be 17 characters (1981+). Validate VIN format and decode year/make/model.
- **Year, Make, Model, Body Type, Gross Vehicle Weight (GVW)**.
- **Garaging Zip Code & Physical Address**: Crucial for territorial rating.
- **Coverage & Deductibles**:
  - Comprehensive / Other Than Collision deductible ($500, $1,000, etc.).
  - Collision deductible ($500, $1,000, etc.).
  - Stated Value / Stated Amount vs. Actual Cash Value (ACV).
  - Hired / Non-Owned Auto Liability (HNOA) if applicable.
- **Lienholder / Loss Payee / Additional Insured - Lessor**: Name and full mailing address.
- **Auto ID Card Issuance**: Must be issued immediately upon vehicle binding.

### B. Vehicle Deletions
- **VIN & Unit Number**: Must exactly match the carrier's active schedule of vehicles.
- **Effective Date of Removal**: Ensure date matches client request or bill of sale.
- **Lienholder Notification**: Note if lender requires certificate of cancellation or release of interest.

### C. Driver Additions / Exclusions
- **Full Legal Name** (First, Middle, Last).
- **Date of Birth (DOB)**.
- **Driver's License Number & State of Issuance**.
- **Motor Vehicle Record (MVR) Status**: Carrier will run MVR; flag major violations (DUI, reckless driving, suspension).
- **Excluded Driver Endorsements**: If a driver is excluded, carrier requires signed **Named Driver Exclusion Form** before endorsement issue.

---

## 2. Three-Way Verification Matrix (CA)

| Element | Client Request / Ticket | Carrier Endorsement / Dec | EZLynx Policy Record |
| :--- | :--- | :--- | :--- |
| **VIN** | 17-digit string from registration | Schedule of Covered Autos | Vehicle schedule in Policy Details |
| **Year/Make/Model** | Registration / Bill of Sale | Schedule of Covered Autos | Policy vehicle list |
| **Garaging Address** | Insured's yard or premises | Garaging Schedule | Location schedule |
| **Comprehensive/Collision**| Requested deductibles | Vehicle coverage block | Vehicle coverage fields |
| **Effective Date** | Requested change date | Endorsement effective date | Transaction effective date |
| **Premium Change** | Quoted AP / RP or $0 | Endorsement premium ($ AP/RP) | EZLynx Transaction premium |

---

## 3. Major Carrier Specifics

### Progressive Commercial
- **Portal URL**: ForAgentsOnly (FAO)
- **Direct Processing**: Most vehicle & driver changes can be endorsed directly in FAO with instant updated ID cards and endorsement decs.
- **Downloads**: Progressive typically downloads endorsements via IVANS overnight. If immediate dec is needed, download from FAO Policy Documents.

### Selective Insurance
- **Portal**: Selective eSelect
- **Policy Prefix**: Typically `S` (e.g., `S 2472200`).
- **Processing**: Direct online endorsement or email to Commercial Underwriter. Vehicle endorsements trigger overnight eDocs download.

### Travelers / National General / Guard
- Verify driver eligibility and vehicle class codes (service, commercial, retail).

---

## 4. Video SOP: Step-by-Step EZLynx Policy Entry & Endorsement Rules

> [!NOTE]
> **Source Walkthrough**: [Commercial Auto Contractors Loom Walkthrough](https://www.loom.com/share/0f1a1709f5264e15939b0c048cea6028) demonstrated on *Maier Solar Engineering LLC DBA Solar Me* (`MAAU169215`, AmGUARD / InterGUARD).
> **User Guidance**: Do NOT dwell on or require SIC codes during policy entry / audit.

### Step 1: Policy Info & Garaging Locations
- **Named Insured & Tax ID**: Confirm legal entity name and FEIN against the Carrier Declarations page.
- **Mandatory Buildings on Garaging Locations**:
  - In EZLynx, every location entered **must have a building added** (`Building # 1`), even for parking lots or secondary yards (e.g., `110 Main St, South Amboy` vs `2138 Driscoll Rd, Toms River`).
  - Vehicles are garaged at specific locations, which drives the territory rating and premium. Every garaging location must have an active building record to link vehicles properly.

### Step 2: Policy-Level Coverages & Auto Symbols
- **Covered Auto Symbols**:
  - **Liability**: Symbol `7` (Specifically Described Autos), Symbol `8` (Hired Autos), Symbol `9` (Non-Owned Autos).
  - **Physical Damage (Comp / Collision)**: Symbol `7`.
- **Liability Limits & UM/UIM Relationship**:
  - Commercial Auto typically written as Combined Single Limit (CSL), e.g., $500,000 or $1,000,000.
  - **Rule**: Uninsured / Underinsured Motorist limits must always be **equal to or less than** the Bodily Injury Liability limit (e.g., $100k UM/UIM on $500k CSL). It can never be higher than Liability.
- **Hired & Non-Owned Auto (HNOA)**:
  - If covered, mark Hired Auto Liability = Yes (Cost of Hire / "If Any") and Non-Owned Liability = Yes (select operating state e.g., NJ, and number of employees e.g., 25).
- **Business Auto Broad Form Endorsement**:
  - Enter broad form enhancements (supplementary payments, transportation expense limits, hired physical damage) directly into the **Endorsements / Remarks** free-form text box.

### Step 3: Vehicle Entry & Fleet Coverage Copying
- **VIN Lookup**: Use the 17-digit VIN lookup to auto-populate year, make, and model.
- **Vehicle Type**: Classify accurately as `Private Passenger` (sedans/standard cars), `Commercial`, or `Truck/Tractor/Trailer`.
- **Garaging Assignment**: Select the exact location (`Location 1` vs `Location 2`) where the unit is garaged.
- **Valuation**: Set to Actual Cash Value (ACV) unless the dec page specifies Stated Amount or Agreed Value.
- **Fleet Shortcut**: When multiple vehicles share deductibles ($500 Comp / $500 Coll), use the **"Apply Additional Coverages From"** dropdown to clone coverages from Vehicle 1 rather than entering them manually.

### Step 4: Driver Management & Exclusions
- Enter full name, DOB, state, and driver's license number.
- **Excluded Driver Rule**:
  - Illustrated with excluded drivers (e.g. Kyle Clarkson, Jesse Daniel).
  - **CRITICAL**: You **CANNOT delete an excluded driver** from EZLynx. They must be kept on the schedule with `Excluded Driver? = Yes`. Deleting an excluded driver creates an E&O gap and causes certificate discrepancies.

### Step 5: Lienholder & Additional Interest Linking
- **Unit Linking Required**:
  - Illustrated with Chrysler Capital and Ally Financial.
  - In EZLynx, every lienholder / loss payee **must be explicitly linked to its specific Vehicle #** (e.g., Chrysler Capital linked to Vehicle #15: 2020 RAM ProMaster City, VIN 9988).
  - Linking ensures that auto-issued ACORD 23 / 25 certificates automatically populate the correct vehicle and lender without manual editing.

---

## 5. Discussion & Closing Protocol
- **Activity Note**: Always note: `"Comm Auto Endorsement verified: Added/Removed [Year Make Model VIN: XXXX]. Deductibles: [Comp/Coll]. Premium AP/RP: [$XX]. Auto ID card issued to client. EZLynx vehicle schedule updated. Robie was here"`
- **Client Email**: If Automation Center does not auto-dispatch, or for manual policies, verify client received Auto ID Card.

