---
name: ezlynx-audit-results-posting
description: "Process completed carrier audit results into EZLynx: rename and file audit statements/invoices into policy folders, post the audit transaction to the policy ledger, record auditable discussion notes with ROBIE was here, deliver audit statements and invoices to clients via built-in templates, and trigger accounting invoicing for agency-billed audits."
---

# EZLynx Audit Results & Invoice Posting SOP

This skill defines the agency operating procedure for posting completed commercial lines and workers' compensation premium audit results into EZLynx, delivering invoices to clients, and updating policy ledgers.

---

## 1. Operating Safeguards & Boundary Rules

1. **Built-in EZLynx Delivery**:
   Always send client-facing audit communications from inside EZLynx using the built-in **Audit** template (`Audit Results (Completed Statement)`) or from authorized agency inboxes (`hello@streetsmart.insurance` / `carlo@streetsmart.insurance`). Never use unmonitored external email clients.
2. **Mandatory Signature**:
   All triage notes, activity logs, and discussion notes posted to EZLynx must conclude with the mandatory standalone signature line:
   ```text
   ROBIE was here
   ```
3. **Dispute Window & Uncollectible Form**:
   Commercial audit invoices typically carry a strict 30-day payment or dispute window. If an insured disputes audit payroll or gross sales figures, verifiable documentation (subcontractor Certificates of Insurance, quarterly 941s, or CPA P&L statements) must accompany the dispute submission. For agency-billed uncollectible audits, the carrier commission waiver form must be executed prior to the carrier due date.

---

## 2. Standard Document Naming & Categorization Matrix

| Incoming Carrier Document | Agency Standard Naming | EZLynx Folder / Category | Policy Link |
| :--- | :--- | :--- | :--- |
| Insured Audit Statement / Summary Breakdown | `Audit Results.pdf` | `Service-Audit Results` | Link to audited policy term |
| Carrier Audit Billing / Broker Invoice | `Audit - Invoice.pdf` | `Service-Audit Results` | Link to audited policy term |
| Audit Workpapers / Auditor Calculations | `Audit - Workpapers.pdf` | `Service-Audit Results` | Link to audited policy term |
| Insured Dispute Documentation (P&L, 941, COIs) | `Audit Dispute - [Insured Name].pdf` | `Service-Audit Dispute` | Link to audited policy term |

---

## 3. End-to-End Workflow Execution

### Step 1: Document Ingestion & Preparation
1. Extract audit statements and invoices from incoming carrier emails or carrier portal downloads.
2. Verify:
   - Named Insured matches EZLynx applicant.
   - Policy Number and Audited Term match the historical term on file.
   - Gross premium adjustment, applicable state surplus lines taxes, and net invoice balances.
3. Save attachments using standard filenames (`Audit Results.pdf` and `Audit - Invoice.pdf`).

### Step 2: Upload Documents to EZLynx
1. Open the applicant's account in EZLynx.
2. Navigate to the **Documents** tab.
3. Upload `Audit Results.pdf` and `Audit - Invoice.pdf`.
4. Ensure both documents are linked to the specific policy and term audited under folder `Service-Audit Results`.

### Step 3: Post the Audit Transaction to the Policy Ledger
1. In the applicant's **Policies** tab, locate and open the policy term that was audited.
2. Click **Actions** → **Add Policy Transaction** (or **Manual Transaction**).
3. Complete transaction fields:
   - **Transaction Type**: `Audit` (or `Premium Audit`).
   - **Effective Date**: The audited term effective date (or invoice transaction effective date).
   - **Transaction Date**: Current processing date.
   - **Premium Adjustment**: Pure premium change (`+$` for Additional Premium, `-$` for Return Premium).
   - **Taxes & Surcharges**: Enter state surplus lines tax / administrative surcharges if applicable.
   - **Total Transaction Premium**: Net financial change to the policy.
   - **Billing Type**:
     - `Agency Bill`: If carrier invoices the agency (e.g. Tuscano, wholesale brokers).
     - `Direct Bill`: If carrier bills the insured directly.
   - **Description / Memo**: Detail the carrier, invoice number, line of business, and due date.
4. Save the transaction to update the EZLynx accounting ledger.

### Step 4: Record Auditable Discussion Card
Thread into the account's existing **Audit** discussion card (or create one with title `Audit - [Policy Number]`):
```text
Policy: #[Policy Number] ([Line of Business] - [Carrier])
Audit Verification - Term: [Term Start] to [Term End]
Date: [YYYY-MM-DD]

Status: Final audit completed by carrier; [Additional Premium Due / Return Premium Credit].
- Audited Premium Adjustment: $[Amount]
- Taxes & Fees: $[Amount]
- Total Insured Balance: $[Total]
- Invoice #: [Invoice Number] | Due Date: [Due Date]
- Billing Method: [Agency Bill / Direct Bill]
- Documents Filed: Audit Results.pdf & Audit - Invoice.pdf

Next Steps: Audit statement delivered to client. [Notify Accounting for agency billing collection / Monitor direct draft].

ROBIE was here
```

### Step 5: Deliver Audit Statement & Invoice to Client
1. From the applicant's Overview page, click the **Email** envelope icon.
2. Select the built-in template: **`Audit Results (Completed Statement)`**.
3. Attach `Audit Results.pdf` and `Audit - Invoice.pdf`.
4. Ensure the email clearly highlights:
   - Total balance due and carrier invoice due date.
   - Payment options (online link, phone, or agency remit).
   - 30-day dispute requirements (submitting subcontractor COIs, 941 payroll reports, or P&L).
5. Always CC the primary producer/account manager and agency management as required.

### Step 6: Agency Billing & Accounting Routing
If the policy is **Agency Bill**:
1. Create a task assigned to `Accounting Team`:
   - **Task Subject**: `Agency Bill Audit Invoicing - [Insured Name] - $[Total]`
   - **Due Date**: 5 business days prior to carrier invoice due date.
   - **Notes**: Specify net payable to carrier, gross receivable from insured, and attach invoice.
