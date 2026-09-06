# Commercial General Liability (CGL) Policy Change & Endorsement SOP

## Line of Business Overview
Commercial General Liability policies protect businesses against bodily injury, property damage, and advertising injury claims arising from operations, premises, and products-completed operations. Common endorsements include adding Additional Insureds, issuing Certificates of Insurance with special endorsements, updating classification codes, and adjusting payroll/gross sales rating bases.

---

## 1. Primary Change Types & Required Data Elements

### A. Additional Insured (AI) Endorsements
- **Form Editions**:
  - `CG 20 10` (Ongoing Operations / Premises)
  - `CG 20 37` (Products-Completed Operations)
  - `CG 20 26` (Designated Person or Organization)
  - Blanket AI Endorsements (e.g. "when required by written contract")
- **Wording Requirements**:
  - **Primary & Non-Contributory**: Ensures the insured's CGL pays first before the certificate holder's own policy without contribution.
  - **Waiver of Transfer of Rights of Recovery (Waiver of Subrogation)** (`CG 24 04`): Carrier waives rights to sue the certificate holder for losses paid.
  - **30-Day Notice of Cancellation**: Often requested by general contractors/landlords (typically 30 days except 10 days for non-payment).

### B. Classification Code & Exposure Base Changes
- **Class Code**: 5-digit ISO or carrier code representing business trade (e.g., 91580 - Contractors / Executive Supervisors, 91340 - Carpentry).
- **Exposure Base**:
  - **Payroll**: For contracting trades (gross wages excluding overtime premium discount).
  - **Gross Receipts / Sales**: For retail, manufacturing, restaurants, distributors.
  - **Units / Area**: E.g., square footage for real estate / commercial buildings.
- **Interim Audits**: Any mid-term adjustment will generate an additional or return premium (AP/RP) calculated pro-rata to the policy expiration date.

### C. Location / Territory Additions
- Physical address of new job site, storage yard, or business premises.
- Construction type, square footage, occupancy, fire protection class.

---

## 2. Three-Way Verification Matrix (CGL)

| Element | Client Request / Ticket | Carrier Endorsement / Dec | EZLynx Policy Record |
| :--- | :--- | :--- | :--- |
| **Additional Insured** | Name, address, and contract requirements | Specific AI endorsement schedule or form number | Certificate holder / Named AI in policy forms |
| **Form Numbers** | CG 20 10, CG 20 37, CG 24 04 | Attached endorsement form codes & edition dates | Forms schedule in EZLynx |
| **Limits of Liability** | Each Occurrence / General Aggregate / Prod-Comp Agg | Policy decs page limit schedule | Coverage limits fields |
| **Effective Date** | Job start date or request date | Endorsement effective date | Transaction effective date |
| **Premium Change** | Quoted AP / RP or $0 (Blanket AI) | Endorsement premium invoice | EZLynx Transaction premium |

---

## 3. Major Carrier & MGA Specifics

### RT Specialty / Interstate MGA
- **Routing Guardrail**:
  - **Commercial Lines ONLY**: Underwriting contact `Caroline.shaddow@rtspeciality.com`, Endorsement inbox `interstate.endorsements@rtspecialty.com`.
  - **NEVER** send commercial CGL endorsements to `QuickHome` (`QuickHomeEndorsements@rtspecialty.com`). QuickHome is strictly Personal Lines.

### Coterie Insurance
- **Portal**: Coterie Agent Dashboard (`dashboard.coterieinsurance.com`).
- Direct endorsement of class code, payroll, and additional insureds with real-time endorsement PDF download.

### Selective / AmTrust / Utica First
- Standard ISO endorsements (`CG 20 10`, `CG 20 37`, `CG 24 04`).
- Many contractor policies have blanket endorsements already attached—verify whether a separate manuscript endorsement is required or if a standard COI referencing the blanket endorsement suffices.

---

## 4. Video SOP: Step-by-Step EZLynx Policy Entry & Endorsement Rules

> [!NOTE]
> **Source Walkthrough**: [General Liability Loom Walkthrough](https://www.loom.com/share/b507be54b3df45e3afcb95f37fa39015) demonstrated on *Fly with Freedom LLC* and *E&B Development Group Inc* with Johnson & Johnson / Scottsdale Insurance.
> **User Guidance**: Do NOT dwell on or require SIC codes during policy entry / audit.

### Step 1: Named Insured & Address Setup
- **Mailing vs. Business Address**:
  - In 9 out of 10 accounts, the mailing address and business premises are identical.
  - If different (e.g., PO Box 529 for mail vs. 3 Jesse Rd for the physical yard), ensure the primary applicant record reflects the mailing address, and the business yard is added under **Locations**.
- **Mandatory Buildings on Locations**:
  - Even for purely operational contractors, every location entered **must have an associated building record** (`Building # 1`).
  - If missing: Click the location row -> Actions -> Edit -> **Save and Add Building**. Missing building records cause class code assignment errors and rate failures in EZLynx.

### Step 2: Coverage Selection & Standard Liability Limits
- **Coverage Form**: Commercial General Liability is typically written on an **Occurrence** form.
- **Standard Limits Schedule**:
  - General Aggregate: `$2,000,000`
  - Products & Completed Operations Aggregate: `$2,000,000`
  - Each Occurrence: `$1,000,000`
  - Personal & Advertising Injury: `$1,000,000`
  - Damage to Rented Premises: `$100,000`
  - Medical Expense (Any One Person): `$5,000`
- **Deductibles & Deductible Basis**:
  - Enter the property damage / bodily injury deductible (e.g., `$1,000`).
  - **CRITICAL**: Always select the **Deductible Type** from the dropdown (**Per Claim** vs. **Per Occurrence**).
- **Limit Applies Per**:
  - Default is **Policy**. If the quote/dec page specifies a designated construction project endorsement (`CG 25 03`) or designated location endorsement (`CG 25 04`), change to **Project** or **Location**.

### Step 3: GL Class Codes, Payroll & Blanket AI Entry
- **Hazard #1 (Primary Operational Class)**:
  - Enter primary classification (e.g., `99777` - Tree Pruning, Dusting, Spraying, Trimming).
  - Premium Basis: Select `Payroll` (e.g., `$16,150` gross remuneration), Rate: `$54.00`, Premium: `$872.10`.
- **Hazard #2 (Secondary / "If Any" Operations)**:
  - Enter incidental classes (e.g., `97047` - Landscape Gardening).
  - For "If Any" classifications where no initial payroll is allocated, enter Exposure as `1` with flat rate or $0.
- **Blanket Additional Insured Keying Shortcut**:
  - If a policy includes blanket additional insured for ongoing operations (`CG 20 10`) or blanket waiver of subrogation, add it directly under **GL Class Codes**:
    - Location 1, Hazard #3, Class Code: Text `Blanket Additional Insured`.
    - Premium Basis: Select `Unit`, Exposure: `1`, Premium: `$250.00` (flat endorsement charge).

### Step 4: Sub-Contractor Underwriting Block
- In the **Contractors - Products / Completed Operations** tab:
  - Check: *"Does the applicant use sub-contractors?"*
  - If Yes: Record the percentage of work subbed out, annual subbed cost, and confirm certificates of insurance (COIs) with equal limits and AI endorsements are required from all trade subs.

### Step 5: Prior Carrier & Additional Interests
- **Prior Carrier Dropdown**: If rewriting or renewing an existing agency account, pull policy information directly from the existing policy record to auto-fill prior carrier, policy number, and premium.
- **Policy Contacts**: Always record the **Inspection Contact** (name, mobile phone, and email) under Policy Contacts so carrier loss control inspectors reach the right person.
- **Additional Interests**: Add certificate holders, landlords, and financial institutions under Additional Interests; use "Import Additional Interest" to clone existing entities from other lines.

---

## 5. Discussion & Closing Protocol
- **Activity Note**: Always note: `"CGL Endorsement verified: [AI Added / Class Code Changed / Exposure Adjusted]. Forms: [CG 20 10 / CG 20 37 / CG 24 04]. Premium: [$XX AP/RP]. COI issued if applicable. EZLynx updated. Robie was here"`
- **Client Email**: If processed via Automation Center, ensure no redundant manual emails are sent.

