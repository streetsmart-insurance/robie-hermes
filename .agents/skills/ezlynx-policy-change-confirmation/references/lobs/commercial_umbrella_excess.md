# Commercial Umbrella & Excess Liability SOP

## Line of Business Overview
Commercial Umbrella and Excess Liability policies provide catastrophic limits above primary underlying policies (Commercial General Liability, Commercial Auto, and Employer's Liability). A policy change on any underlying policy (or a change in excess limit) requires rigorous verification of the Underlying Insurance Schedule.

---

## 1. Primary Change Types & Required Data Elements

### A. Limit Increases / Decreases
- **Policy Limit**: Common excess limits: $1M, $2M, $5M, $10M.
- **Underlying Minimum Requirements**:
  - General Liability: Typically $1M Each Occurrence / $2M General Aggregate / $2M Products-Completed Ops.
  - Commercial Auto: Typically $1M Combined Single Limit (CSL).
  - Employer's Liability: Typically $500k/$500k/$500k or $1M/$1M/$1M.
- **Wholesaler / MGA Binding Authority**: Many excess policies (e.g., Markel via JIMCOR) require underwriter sign-off if aggregate limits exceed standard brokerage authority.

### B. Underlying Policy Schedule Synchronization
- **Carrier Name & NAIC / AM Best Rating**: Underlying carrier must meet minimum AM Best rating (usually A- VII or better).
- **Underlying Policy Number & Effective Dates**: Must accurately reflect renewed or endorsed underlying terms.
- **Gaps in Coverage**: Effective date of underlying change must align with excess endorsement to avoid uninsured exposure gaps.

---

## 2. Three-Way Verification Matrix (Umbrella / Excess)

| Element | Client Request / Ticket | Carrier Endorsement / Dec | EZLynx Policy Record |
| :--- | :--- | :--- | :--- |
| **Excess Limit** | Requested total umbrella limit | Declarations / Endorsement Schedule | Policy coverage limit field |
| **Schedule of Underlying**| Underlying GL, CA, WC policies | Carrier Schedule of Underlying | Underlying insurance tab in EZLynx |
| **Effective Date** | Requested endorsement date | Endorsement effective date | Transaction effective date |
| **Premium Change** | Quoted AP / RP | Carrier endorsement premium invoice | EZLynx Transaction premium |

---

## 3. Major Carrier & Broker Specifics

### JIMCOR Agencies / Markel
- **Contact**: `ARivera@jimcor.com` (Arlene Rivera).
- Excess limit increases (e.g. to $5M) require underwriter submission and review if exceeding broker binding limits.
- Always monitor until formal carrier binder or endorsement schedule is issued before closing discussion.

### Selective / Travelers / Nationwide
- Monoline excess or package umbrella endorsements issued directly or via download.

---

## 4. Video SOP: Step-by-Step EZLynx Policy Entry & Endorsement Rules

> [!NOTE]
> **Source Walkthrough**: [Commercial Umbrella Loom Walkthrough](https://www.loom.com/share/a491c3ab97dd4f33b3a3aa8e077f230c) demonstrated on *A To Z Complete Construction LLC* (`EZX53030938`, Evanston Insurance Company / Markel via Johnson & Johnson).
> **User Guidance**: Do NOT dwell on or require SIC codes during policy entry / audit.

### Step 1: Policy Info & Location Requirement
- Confirm legal entity name and mailing address against the Excess Declarations Page.
- **Location Setup**: Even though Umbrella/Excess is a follow-form liability policy, a valid physical **Location** must be added in EZLynx (e.g., `40 Sequoia Dr, Old Bridge NJ` or `258 Brookfield Dr, Jackson NJ`).

### Step 2: Coverages & Retained Limit (SIR)
- **Transaction / Coverage Form**: Select **Occurrence** (matching primary GL).
- **Limits of Liability**:
  - Each Occurrence: `$1,000,000` (or total umbrella limit, e.g., $2M, $5M).
  - Aggregate Limit: `$1,000,000` (or applicable policy aggregate).
- **Retained Limit / Self-Insured Retention (SIR)**:
  - Check the declarations page under *Retained Limit / Each Occurrence*.
  - If `$0` or unstated, enter `$0`. If the carrier dec specifies an SIR (e.g., $5,000 or $10,000), enter that exact figure into the **Retained Limit** field.
- **First Dollar Defense**: Typically select **No** unless the carrier contract explicitly provides defense outside the retained limit.

### Step 3: Schedule of Underlying Insurance (CRITICAL AUDIT STEP)
- Navigate to the **Underlying Insurance** tab:
  - Open the carrier policy's **Schedule of Underlying Insurance** (form `MUB 1800 04 17` or `MUB 1214 09 17`).
  - Compare the carrier's schedule line-by-line with EZLynx:
    - **General Liability**: Ensure carrier name (e.g., Evanston Ins Co), policy number, effective/expiration dates, and limits ($1M Each Occ, $2M Gen Agg, $2M Prod-Comp Agg) match.
    - **Automobile Liability**: If Auto Liability is covered under the umbrella, link the active Auto policy; if not covered by the carrier, do NOT enter an Auto line.
    - **Employers' Liability (Workers' Comp)**:
      - **CRITICAL E&O RULE**: In the walkthrough, an expired Employers' Liability record sat on the EZLynx underlying schedule even though the carrier's Schedule of Underlying Insurance listed **only General Liability**.
      - **Mandatory Action**: If an underlying policy is **NOT on the carrier's issued Schedule of Underlying Insurance, it MUST BE REMOVED from EZLynx immediately**. Showing an underlying line in EZLynx that the carrier does not recognize creates severe E&O exposure and causes certificates of insurance to misstate excess coverage.

---

## 5. Discussion & Closing Protocol
- **Activity Note**: Always note: `"Commercial Umbrella/Excess Endorsement verified: [Limit increased/decreased to $XXM / Underlying schedule updated]. Underlying: [GL, CA, WC verified]. Premium AP/RP: [$XX]. Robie was here"`

