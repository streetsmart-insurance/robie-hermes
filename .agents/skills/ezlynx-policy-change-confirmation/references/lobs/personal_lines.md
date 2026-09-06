# Personal Lines (Homeowners, Personal Auto, Dwelling Fire) SOP

## Line of Business Overview
Personal Lines cover individuals and families against loss of private residences (HO-3, HO-5, HO-6, DP-3), personal vehicles, watercraft, and personal liability. Changes primarily involve mortgage refinances/escrow updates, adding teen or resident drivers, new vehicle purchases, and dwelling value / roof updates.

---

## 1. Primary Change Types & Required Data Elements

### A. Homeowners / Dwelling Fire
- **Mortgagee Changes**: Loan number, escrow billing address, new lender legal name.
- **Coverage A (Dwelling Limit)**: Adjustments based on 360Value or Marshall & Swift / Boeckh replacement cost calculations.
- **Water Backup / Sump Overflow**: Standard endorsement limit ($5,000, $10,000, $25,000).
- **Mailing Address vs. Location**: In rental dwellings (DP-3), landlord mailing address must be distinguished from tenant residence address.

### B. Personal Auto
- **Vehicle Changes**: 17-digit VIN, Lienholder/Loss Payee, Comprehensive/Collision deductibles, glass coverage.
- **Driver Changes**: Resident family members reaching driving age, marital status updates, license number verification.

---

## 2. Strict Wholesaler & MGA Routing Boundaries

### RT Specialty / QuickHome (CRITICAL RULE)
- **QuickHome IS EXCLUSIVELY PERSONAL LINES**:
  - `QuickHomeEndorsements@rtspecialty.com`
  - `quickhome@allrisks.com`
  - `QuickHomeQuotes@rtspecialty.com`
- **Rule**: If a policy is **Personal Lines** (Personal Umbrella, Coastal Homeowners, Vacation Home), route to QuickHome. If the policy is **Commercial Lines**, it MUST NEVER be sent to QuickHome. Commercial must go to `Caroline.shaddow@rtspeciality.com` or `interstate.endorsements@rtspecialty.com`.

### Progressive Personal / Plymouth Rock / Travelers Personal / Franklin Mutual
- Standard personal lines downloads via IVANS directly into EZLynx.

---

## 3. Video SOP: Step-by-Step EZLynx Policy Entry & Endorsement Rules

### A. Personal Auto & Collector Vehicle Policies

> [!NOTE]
> **Source Walkthrough**: [Personal Auto Loom Walkthrough](https://www.loom.com/share/efbd57168ba240a39f8823c614e6da1b) demonstrated on *Joszef Petkes & Tunde Sokoli* (`054545388`, Classic Auto Insurance / American Modern Home Insurance).

1. **Named Insured & Garaging Address**:
   - Ensure primary insured and spouse/co-applicant are listed as drivers.
   - Verify the vehicle garaging address matches where exotic/classic vehicles are stored.
2. **Collector Vehicle Valuation (Agreed Value / Stated Amount)**:
   - For classic/exotic cars written on an agreed value basis (e.g., 2018 Lamborghini Huracan Performante, 2015 Porsche 911 GT3):
     - **CRITICAL**: Enter the exact agreed valuation (e.g., `$322,085` or `$123,000`) into the **Stated Amount / ACV unless Stated** field in EZLynx.
     - For standard everyday vehicles rated on Actual Cash Value (ACV), leave this field blank.
   - **Annual Mileage & Usage Tiers**:
     - Key annual mileage restrictions (e.g., `3,000` annual miles) and set Usage to **Pleasure**.
3. **Liability Limits & New Jersey PIP**:
   - Split limits: Bodily Injury `$15,000 / $30,000`, Property Damage `$5,000`.
   - Uninsured/Underinsured Motorists: `$15,000 / $30,000 / $5,000`.
   - **Personal Injury Protection (NJ PIP)**:
     - Medical Expense Limit: `$15,000` (or elected limit up to $250,000).
     - PIP Deductible: `$250` (or $500, $1,000, $2,500).
     - PIP Lawsuit Threshold: Typically select **Lawsuit Threshold / Limitation on Lawsuit**.
4. **Policy-Level Additional Coverages**:
   - If included on the carrier declaration, add under **Additional Coverages**:
     - Nationwide Roadside Assistance: `$10.00` premium, `$200` per occurrence limit.
     - Spare Parts: `$2,000` per occurrence.
     - Trip Interruption: `$150` per day / `$600` per occurrence.
5. **Lienholder Vehicle Linking**:
   - Link financing institutions (e.g., TD Auto Finance) directly to their respective vehicle unit number. Delete outdated banks from prior vehicles.

---

### B. Homeowners (HO-3) & Dwelling Fire (DP-1 / DP-3) Policies

> [!NOTE]
> **Source Walkthrough**: [Homeowners Loom Walkthrough](https://www.loom.com/share/f05774171d434032811b2e3eb9fd5734) demonstrated on *Jonathan Haviland* (`FLH-0010337`, All Risks / Lloyd's of London) and *Shafar's Masonry LLC* (`HDNJ2015070057-20`, Hyundai Marine & Fire).

1. **Dwelling Address vs. Mailing Address**:
   - Differentiate applicant mailing address (e.g., PO Box or secondary home) from Location #1 (the physical risk address).
   - Always click **Validate** to standardize the street address with the USPS database.
2. **Dwelling Construction & Underwriting Info**:
   - Ensure dwelling details (Year Built `1920`, Total Living Area `2,380 sq ft`, Construction Type `Wood/BM`, Roof `Asphalt Shingles`, Heating `Gas-Forced Air`, Distance to Hydrant `500 ft`, Protection Class `4`) pull from the EZLynx rater. If blank, copy from the application tab into the policy record so future remarketing retains property data.
3. **Coverage Limits & Loss Settlement Options**:
   - Match Section I limits: Coverage A (Dwelling), Coverage B (Other Structures - typically 10%), Coverage C (Personal Property - typically 50%), Coverage D (Loss of Use - typically 20%).
   - Match Section II limits: Coverage E (Personal Liability, e.g., `$500,000`), Coverage F (Medical Payments, e.g., `$5,000`).
   - **Loss Settlement Option**: Always select **Replacement Cost - Dwelling** and **Replacement Cost - Contents**. **NEVER select Full Value**.
4. **Deductibles & Form Types**:
   - Enter All Peril Deductible (e.g., `$1,000`).
   - If Wind/Hail has a separate percentage deductible (e.g., `1%` or `2%`), key the percentage directly into the Wind/Hail % column.
   - **Policy Form Type**: Never leave as `Unknown`. Set to **Special (HO-3)** for standard homeowner policies; set to **Basic (DP-1 / HO-1)** for builder's risk or vacant home surplus lines policies.
5. **Endorsement Verification (Water Backup)**:
   - Check carrier dec page before selecting Water Backup of Sewers & Drains. If not on the carrier policy dec, **uncheck it** in EZLynx so client summaries do not falsely show water backup coverage.
6. **Mortgagee Billing (Escrow Linking)**:
   - When adding a bank (e.g., Flagstar Bank FSB ISAOA/ATIMA, Loan # `0504831806`), check **Send Bill** under Additional Interest.
   - This automatically switches the EZLynx policy payor to **Mortgagee / Escrow**.

---

## 4. Discussion & Closing Protocol
- **Activity Note**: Always note: `"Personal Lines Endorsement verified: [Mortgagee updated / Vehicle added / Driver added]. Premium AP/RP: [$XX]. Client decs uploaded. Robie was here"`

