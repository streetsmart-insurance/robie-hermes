---
name: "streetsmart-policy-change-process"
description: "End-to-end operational execution, carrier portal automation, EZLynx tracking shell generation, document retrieval, and client confirmation for personal and commercial policy changes (Process, Retrieve, and Confirm)."
---

# StreetSmart Insurance: Policy Change Process (Process, Retrieve, Confirm)

This skill defines the authoritative, end-to-end agency operational standard for executing, tracking, and confirming insurance policy endorsements across carrier portals, the EZLynx management system, and client communications.

Every policy change follows a strict **3-Component Closed Loop System**:
1. **Process**: Execute change on the carrier portal and key the tracking change request in EZLynx.
2. **Retrieve**: Pull issued carrier documents (endorsement declarations, binders, auto ID cards) and trigger electronic ID card delivery.
3. **Confirm**: Update the EZLynx discussion/task card, post the comprehensive audit note, and dispatch the client confirmation email during business hours.

---

## Component 1: Process (Carrier & Management System Synchronization)

### 1.1 Carrier Portal Execution (e.g. National General / Integon)
- **Session & Navigation**:
  - Always search the active policy first via the carrier main search widget (`MainMenu.aspx` -> `#ctl00_MainContent_wgtMainMenuFindPolicy_txtSearchString`) to establish ASP.NET session state.
  - Trigger endorsement workflow via postback `ctl00$MainContent$WidgetINeedTo1$btnPolicyChange`.
- **Entity & Schedule Modifications**:
  - **Vehicles**: Input full 17-digit VIN, trigger decode, verify vehicle symbol, confirm garaging address, assign registered owner, assign primary driver, and select use (Pleasure/Commute/Local).
  - **Coverages**: Enforce requested limits (e.g., Liability-Only vs Full Physical Damage). For Liability-Only, set Comprehensive, Collision, Rental, and Towing to None/Declined. Align PIP deductibles and UM/UIM limits.
  - **Drivers**: Verify existing drivers; do not add new drivers unless explicitly requested.
- **Rating, Premium Calculation & Binding**:
  - Review rating worksheet and rate comparison.
  - Capture **Pro-Rated Change Amount** (itemizing vehicle premium delta and state/statutory fees) and the **Revised Full-Term Premium**.
  - Review Payment/Billing plan (e.g., Direct Bill vs Agency Bill).
  - Submit quote / commit endorsement (e.g., Quote #12 submitted and bound). Capture the final policy schedule screenshot.

### 1.2 EZLynx Change Request Keying (Tracking Shell)
- **Golden Rule**: **NEVER modify the active historical policy directly in EZLynx.** Modifying the base policy corrupts the historical term and prevents IVANS electronic download matching.
- **Tracking Shell Generation**:
  - Locate applicant policy via Policy Master ID (`hasPendingChangeRequest: True`).
  - Key the **Policy Change Request** with the exact effective date requested.
  - This generates the **yellow tracking shell** in EZLynx Policy History.
  - When the carrier transmits the change via IVANS Electronic Download (`PCH` transaction), EZLynx automatically reconciles the transaction against the tracking shell.

---

## Component 2: Retrieve (Carrier Evidence & ID Card Delivery)

### 2.1 Carrier Document Retrieval & Delivery
- **Carrier File Room**:
  - Access the carrier's document repository (`PolicySummary.aspx` or `File Room`).
  - Confirm newly issued Endorsement Policy Documents (`dgPolicyHistory` row with current effective date).
- **Auto Insurance ID Cards**:
  - Navigate to carrier ID Card Request module (e.g., `Summary/IDCardRequest.aspx`).
  - **Direct Carrier Electronic Dispatch**:
    1. Select the **Email** tab (`javascript:__doPostBack('...mnuRequestMethod','Email')`).
    2. Populate recipient name (`txtEmailToName`) and insured email (`txtEmailAddr`).
    3. Click `Send Email` (`#...btnSendEmail`) so National General sends the official electronic ID card directly to the client's inbox.
  - **Agency Document Archival**:
    - Download issued endorsement declarations / ID card PDFs to `/opt/renewal-automation-system/data/documents/` and upload to EZLynx account documents under the applicable policy folder.

---

## Component 3: Confirm (E&O Audit Trail, Discussion Note, & Client Email)

### 3.1 EZLynx Activity Stream & Task Updating
When a policy change request is keyed, the EZLynx Automation Center automatically generates a placeholder task card:
`Personal Auto Policy Change Request - CHANGE ME`

The agent must immediately update and document this card:
1. **Update Discussion Title**:
   - Change from `CHANGE ME` to descriptive syntax:
     `Personal Auto Policy Change Request - Add [Year Make Model], effective [MM/DD/YYYY], [coverage type].`
2. **Post Detailed Endorsement Note**:
   Post an authoritative audit note detailing:
   - **Carrier & Policy Number**: Integon National / National General, Policy #.
   - **Endorsement Status**: Quote/Endorsement # bound effective date.
   - **Added/Modified Entity**: Unit #, Year Make Model, VIN.
   - **Coverages Configured**: Exact limits, deductibles, and exclusions.
   - **Garaging & Drivers**: Garaging address, registered owner, driver assignment.
   - **Financials & Billing**: Pro-rated term delta, revised full-term premium, Direct Bill arrangement.
   - **Auto ID Card Status**: Electronic ID card dispatched from carrier to client email.
   - **Handoff**: Assigned to CSR (Ana Flores) for IVANS electronic download (PCH) reconciliation.

### 3.2 Client Confirmation Email Dispatch
- **Timing**: Must be dispatched **during business hours**.
- **Sender**: Assigned Producer (e.g., `jazmin@streetsmart.insurance`) or CSR (`service@streetsmart.insurance`) using Google Workspace Domain-Wide Delegation API.
- **Mandatory Subject Line Formatting**:
  Must strictly contain:
  `StreetSmart Insurance: Policy Change Confirmation & Auto ID Card - [Insured Name] - Policy #[Policy Number] ([Change Description])`
- **Mandatory Email Body Details**:
  1. **Policy & Vehicle Details**: Named Insured, Policy #, Carrier, Effective Date, Year Make Model, 17-digit VIN.
  2. **Coverages Configured**: Itemized limits (BI, PD, PIP medical/deductible) and confirmation of Liability-Only (No Comp/Collision).
  3. **Premium Adjustment & Billing**:
     - Additional Pro-Rated Premium (premium + fees).
     - Revised Total Full-Term Premium.
     - Billing Type: Direct Bill with carrier; monthly installment adjustment notice.
  4. **ID Card Delivery Notice**: Confirmation that official electronic ID cards were issued and emailed from the carrier to the client's address.
  5. **Sign-off**: Official agency signature with contact phone `(732) 462-8343`.

### 3.3 Close the Audit Loop in EZLynx
- Append a reply to the EZLynx task discussion noting the client email dispatch timestamp and Google Workspace Message ID (e.g. `1a08225aa8e5d271`) to provide an airtight E&O audit defense.

---

## Resources & Operational Questionnaires

Detailed questionnaires, intake questions, and operational protocols are hardcoded in the skill resources directory:

- **[Policy Change Intake Questionnaire & Questions (All Lines of Business)](resources/policy_change_intake_questionnaire.md)**:
  - Personal Auto: Vehicle Addition (17-digit VIN, trim, garaging, registered owner, use, primary driver, liability-only vs full coverage, comp/coll deductibles, lienholder details), Vehicle Deletion (disposition, proof of sale, replacement checks), Driver Addition (license details, DOB, violations, vehicle assignment), Driver Removal/Exclusion (proof of separate coverage, signed exclusion forms), Address/Garaging change.
  - Commercial Auto: GVWR, classification (service/commercial/retail), radius of operation, CDL credentials, stated value vs ACV.
  - Homeowners & Dwelling Fire: Mortgagee/Escrow clauses, loan numbers, Coverage A replacement cost evaluations (360Value), wind/hail deductibles, water backup limits.
  - Commercial General Liability & Package: Building construction, sprinkler protections, certificate holder requirements (CG 20 10, CG 20 37, Primary & Non-Contributory, Waiver of Subrogation), exposure/payroll adjustments.
- **[Policy Change Master Standard Operating Procedure (SOP)](resources/policy_change_sop_master.md)**:
  - Full operating lifecycle of the 3-Component Closed Loop System (Process, Retrieve, Confirm).
  - The Five Inviolable E&O Safeguards.
  - Business hours email dispatch mandate.
  - Reconciliation via IVANS Electronic Download (PCH) and EZLynx task closure.
