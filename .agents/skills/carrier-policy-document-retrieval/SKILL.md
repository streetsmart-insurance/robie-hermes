---
name: "carrier-policy-document-retrieval"
description: "Retrieve missing carrier policy change documents (endorsements, revised declarations, schedules, confirmations) across Commercial and Personal Lines via carrier portals, EZLynx carrier email templates, and the autonomous voice AI phone system. Files documents into EZLynx and transitions ready cases to 3-way match confirmation."
---

# Carrier Policy Change Document Retrieval & Tri-Channel Follow-Up

This skill defines the autonomous operations workflow to identify open policy change requests lacking carrier evidence, retrieve the issued documents through three coordinated channels (Portals, Email, and Voice AI), file documents compliant with agency upkeep standards, and hand off ready cases for 3-way confirmation QC.

---

## 1. Core Operating Principles

1. **Carrier-Only Communications**:
   - All retrieval communications (email, phone, portal inquiries) are directed **strictly to carriers, underwriters, and MGAs**.
   - **NEVER contact or email the insured/client** for carrier endorsements or declarations.
   - When composing emails or placing calls, verify that the client email/phone is completely removed from all recipient lists.
2. **Tri-Channel Retrieval Sequence**:
   - **Channel 1 (Portals & IVANS)**: Always check local EZLynx account folders, eDocs feeds, and direct carrier portals first.
   - **Channel 2 (EZLynx Carrier Email Templates)**: If online download is unavailable, dispatch an email follow-up using the official EZLynx template (`Policy Change Request Change Request Follow up Email Templates (Carrier)`).
   - **Channel 3 (Voice AI Phone System)**: When written follow-up is pending for >3 business days, or the carrier services endorsements primarily via phone, dispatch an autonomous voice AI call via the Bland AI engine (`scripts/carrier_policy_change_caller.py`).
3. **Line of Business Routing Rules (RT Specialty vs QuickHome)**:
   - `quickhome@allrisks.com` is **strictly Personal Lines**. Never route Commercial Lines inquiries to QuickHome.
   - Commercial Lines endorsements route to `Caroline.shaddow@rtspeciality.com`, `stephanie.tower@rtspecialty.com`, or `interstate.endorsements@rtspecialty.com`.
4. **Permanent Discussion Audit Trail**:
   - Every retrieval attempt (portal check, email dispatch, phone call dispatch, call summary, transcript) must be logged into the applicable EZLynx policy change discussion card.
   - Every note must conclude with the mandatory agency signature line:
     ```text
     ROBIE was here
     ```

---

## 2. Reference Catalogs & Matrices

- **Carrier & MGA Phone Directory & IVR Guidance**: [references/carrier_calling_matrix.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/carrier-policy-document-retrieval/references/carrier_calling_matrix.md)
- **Carrier Portals & Direct Download Matrix**: [references/carrier_portals_matrix.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/carrier-policy-document-retrieval/references/carrier_portals_matrix.md)

---

## 3. Workflow Procedures

### Phase 1: Intake & Triage
1. Review the open policy change request from the EZLynx queue report.
2. Inspect the applicant's **Documents** tab and subfolders:
   - Check `Policy Changes`, `Declarations`, and `eDocs`.
   - Verify whether an endorsement or revised declaration matching the requested change date is already on file.
3. If valid carrier evidence is already present:
   - Verify readability, term, policy number, and insured name.
   - Set status to `ready_for_confirmation` and hand off to `ezlynx-policy-change-confirmation`.
4. If carrier evidence is missing:
   - Proceed to **Phase 2: Channel Execution**.

---

### Phase 2: Channel Execution

#### Channel 1: Portal & IVANS eDocs Retrieval
1. Consult [references/carrier_portals_matrix.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/carrier-policy-document-retrieval/references/carrier_portals_matrix.md).
2. For portal-enabled carriers (e.g., Progressive FAO, Selective eSelect, Coterie, Geico Agent Gateway, AmTrust Online, FMI Innovation):
   - Access the carrier portal using agency credentials.
   - Search the policy number and download the endorsement schedule / revised dec page.
3. If successfully downloaded, proceed directly to **Phase 3: Document Upkeep & Filing**.
4. If portal indicates pending underwriter review or download is unavailable, transition to Channel 2 or Channel 3.

#### Channel 2: EZLynx Carrier Follow-Up Email
1. Open the applicant file in EZLynx.
2. Click the email icon in the upper-right corner.
3. Load the official agency template:
   - **Template**: `Policy Change Request Change Request Follow up Email Templates (Carrier)`
4. Set recipient:
   - **To**: Carrier / MGA underwriter endorsement inbox (consult [references/carrier_calling_matrix.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/carrier-policy-document-retrieval/references/carrier_calling_matrix.md)).
   - **CC**: Assigned CSR or producer (e.g., `lenin@streetsmart.insurance`).
   - **REMOVE**: Any client email addresses.
5. Populate merge fields:
   - Subject: `[Account Name] [Policy #] - Policy Change Request Follow Up`
   - Specify the exact change submitted, effective date, and request for endorsement/declarations.
6. Send email directly from EZLynx to generate a permanent activity record.
7. Log a note on the policy change discussion card noting email dispatch and setting follow-up date (+3 business days).

#### Channel 3: Autonomous Voice AI Phone Follow-Up
When email follow-up is unresponsive after 3 business days, or when dealing with immediate phone-servicing carriers (e.g., Merchants, Progressive service line, Selective agency desk):

1. **Hydrate Context**:
   - Extract policy number, carrier name, applicant legal name, change details (e.g., vehicle added, driver removed, address updated), and submission date.
   - Look up carrier phone, agency code, and IVR menu in [references/carrier_calling_matrix.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/carrier-policy-document-retrieval/references/carrier_calling_matrix.md).
2. **Execute Call Dispatcher**:
   ```bash
   python3 scripts/carrier_policy_change_caller.py \
     --policy-number "<POLICY_NUMBER>" \
     --carrier "<CARRIER_NAME>" \
     --change-summary "<SUMMARY_OF_CHANGE>" \
     [--dry-run]
   ```
3. **Voice AI Objectives**:
   - Navigate carrier phone trees to Commercial/Personal Policy Servicing.
   - Hold through queue chimes until greeted by a live representative.
   - Inquire whether the change was issued and request documents emailed to `robie@streetsmart.insurance`.
   - If pending, capture missing requirements and underwriter contact info.
4. **Post-Call Logging**:
   - Capture call recording URL, transcript, and outcome.
   - Post confirmation note to EZLynx discussion card:
     ```text
     [MM/DD/YYYY HH:MM ET] CARRIER PHONE FOLLOW-UP — Endorsement Status Check
     Carrier: [Carrier Name] | Phone: [Phone Number] | Rep: [Rep Name]
     Policy: [Policy #] | Insured: [Insured Name]
     Outcome: [Issued / Underwriting Review / Additional Info Required]
     Notes: [Call summary]
     Next Action: [e.g. Awaiting emailed endorsement to robie@streetsmart.insurance]

     ROBIE was here
     ```

---

### Phase 3: Document Upkeep & Filing Standards

For every document retrieved across any channel:
1. **Validation**:
   - Open and inspect PDF.
   - Confirm Named Insured matches the policy's **Summary Tab** ("Named Insured As Listed On The Policy").
   - Confirm policy number and change effective date match the request.
2. **Storage Location**:
   - Account folder: `Policy Changes` (or `Declarations` for complete policy packages).
3. **Descriptive File Naming**:
   - Format: `[Carrier] - [Change Type / Details] - [Effective Date].pdf`
   - Example: `National General - Add 2015 RAM ProMaster - 2026-08-10.pdf`
4. **Metadata & Labeling**:
   - Label: `Endorsement` (or `Signed Change Form` / `Auto ID Card`).
   - Policy Association: Link explicitly to the active policy record.
5. **State Transition**:
   - Update discussion: `Carrier endorsement retrieved, verified, and filed. Ready for 3-way match.`
   - Set status to `ready_for_confirmation`.
   - Hand off to `ezlynx-policy-change-confirmation` for downstream verification and task closure.
