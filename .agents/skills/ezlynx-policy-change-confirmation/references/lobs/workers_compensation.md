# Workers' Compensation (WC) Policy Change & Endorsement SOP

## Line of Business Overview
Workers' Compensation provides statutory coverage for wage replacement and medical benefits to employees injured in the course of employment, along with Employer's Liability (Part Two). Changes routinely involve estimated payroll modifications, class code additions, officer inclusions/exclusions, and state-specific waivers of subrogation.

---

## 1. Primary Change Types & Required Data Elements

### A. Corporate Officer / LLC Member / Partner Status
- **Inclusion / Exclusion Rules**: Vary strictly by state:
  - **New Jersey**: Sole proprietors and partners are excluded unless elected. LLC members and corporate officers are included unless valid exclusion form (e.g., NJ WC-106) is filed and approved.
  - **New York**: Corporate officers with >= 50% shares may exclude using NY Form C-105.21.
- **Audited Payroll Caps**: State regulatory boards set minimum and maximum payroll limits for executive officers and LLC members annually. Ensure correct cap is applied.

### B. Classification Code & Payroll Adjustments
- **Class Code**: 4-digit NCCI or state rating bureau code (e.g., 5403 - Carpentry, 5645 - Residential Carpentry, 8810 - Clerical Office).
- **Estimated Annual Remuneration (Payroll)**:
  - Mid-term payroll increases/decreases adjust monthly reporting or installment schedule.
  - Subcontractor exposure: Uninsured subcontractors require 100% payroll inclusion under the applicable trade class unless valid certificates of insurance are maintained.

### C. Waivers of Subrogation (WC 00 03 13)
- Blanket Waiver of Subrogation vs. Scheduled Specific Job Waiver.
- Direct statutory surcharge applied (typically 2% to 5% of manual premium on the applicable payroll or flat charge).

---

## 2. Three-Way Verification Matrix (WC)

| Element | Client Request / Ticket | Carrier Endorsement / Dec | EZLynx Policy Record |
| :--- | :--- | :--- | :--- |
| **Class Codes** | Specific trade or clerical roles | Information Page Schedule | Class code lines in Policy Details |
| **Estimated Payroll** | Revised annual payroll projection | Endorsement Information Page | Total estimated payroll |
| **Officer Exclusion** | Signed state waiver form | Exclusion endorsement form | Officer inclusion/exclusion list |
| **Waiver of Subrogation**| Job contract / Certificate holder | WC 00 03 13 endorsement schedule | Policy endorsement list |
| **Effective Date** | Requested change date | Endorsement effective date | Transaction effective date |
| **Premium Change** | Quoted AP / RP | Carrier invoice / endorsement premium | EZLynx Transaction premium |

---

## 3. Major Carrier Specifics

### AmTrust North America
- **Portal**: AmTrust Online (`agents.amtrustgroup.com`).
- Direct endorsement requests processed via portal or underwriter email.
- Instant endorsement quote generation for officer changes and payroll updates.

### Guard (Berkshire Hathaway Guard)
- **Portal**: Guard Policy Management.
- Fast turnaround on payroll adjustments; automatic policy change documents uploaded.

### The Hartford / Travelers / Selective
- Strict adherence to NCCI class codes and annual officer payroll minimums/maximums.

---

## 4. Video SOP: Step-by-Step EZLynx Policy Entry & Endorsement Rules

> [!NOTE]
> **Source Walkthrough**: [Workers' Compensation Loom Walkthrough](https://www.loom.com/share/0b7a9f616b17456a84a4e4db2153d289) demonstrated on *Yes We Do LLC* (`46-659953-01-04`, Continental Casualty / Applied Underwriters).
> **User Guidance**: Do NOT dwell on or require SIC codes during policy entry / audit.

### Step 1: Named Insured & Mandatory Building on Locations
- **Named Insured & FEIN**: Confirm legal entity (LLC, Sole Proprietorship, Corp) and FEIN against the WC Information Page.
- **Mandatory Buildings on Locations**:
  - Even though Workers' Comp does not insure physical property, in EZLynx **every location entered must have an active building added** (`Building # 1`).
  - Without a building record, attempting to assign class codes to that location will result in an empty location dropdown or a save error.

### Step 2: Part 1 Statutory & Part 2 Employers' Liability Limits
- **Part 1**: Provides statutory Workers' Compensation benefits mandated by state law.
- **Part 2 (Employers' Liability)**:
  - Bodily Injury by Accident: `$1,000,000` Each Accident
  - Bodily Injury by Disease: `$1,000,000` Policy Limit
  - Bodily Injury by Disease: `$1,000,000` Each Employee

### Step 3: Part 3 - Other States Insurance (3.C)
- Inspect Item 3.C on the carrier Information Page:
  - Standard carrier language typically reads: *"Part Three of the policy applies to the states, if any, listed here: ALL STATES EXCEPT ND, OH, WA, WY"* (the four monopolistic state funds).
  - In EZLynx under **Coverages -> Part 3 - Other States**: Use "Uncheck All" and ensure the monopolistic state funds (North Dakota, Ohio, Washington, Wyoming) are excluded while all eligible broad-form states remain selected.

### Step 4: Class Codes, Estimated Payroll & Rating Factors
- **Class Code Lines**:
  - Key each trade classification (e.g., `5474` - Painting/Paperhanging, `5645` - Residential Carpentry, `8810` - Clerical Office).
  - Enter Estimated Annual Remuneration (Payroll) and the carrier's approved rate per $100 of payroll.
- **Experience Modification Factor**:
  - Key the approved NCCI / state bureau Mod Factor (e.g., `1.104` or `0.92`) into the **Experience Modification** field.
- **Surcharges & State Funds**:
  - Ensure applicable surcharges (e.g., New Jersey Second Injury Fund `0935`, Terrorism `9740`, Catastrophe `9741`, and Expense Constant `0900`) match the carrier's premium breakdown.

### Step 5: Owners & Executive Officers (Included vs. Excluded)
- **NJ PP1-B Form Verification**:
  - In New Jersey, sole proprietors, partners, and LLC members must file a **PP1-B Election of Coverage Form** (`WC 29 03 07`) to be included or excluded.
  - In EZLynx under **Individuals Included / Excluded**:
    - Record owner name (e.g., Cindy Deeny, Owner, 100% ownership).
    - Status: Explicitly select **Included** or **Excluded**.
    - If Included: Apply the state-mandated executive remuneration cap (e.g., `$33,500`) and the appropriate classification code (e.g., `5606`).
- **Policy Contacts**:
  - Always add an **Accounting Contact** (name, phone, email) under Policy Contacts so audit correspondence and annual payroll adjustment forms reach the client's bookkeeper/CPA promptly.

---

## 5. Discussion & Closing Protocol
- **Activity Note**: Always note: `"Workers Comp Endorsement verified: [Class code / Payroll / Officer status change]. Revised Payroll: [$XX,XXX]. Premium AP/RP: [$XX]. Information Page attached. Robie was here"`

