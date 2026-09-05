---
name: "ezlynx-policy-change-confirmation"
description: "Review StreetSmart Insurance EZLynx open policy change requests across all Lines of Business (Auto, GL, BOP, WC, Excess, Personal), obtain missing carrier endorsements or declarations, verify the issued change against the original request, confirm or correct the EZLynx transaction, document exceptions, and close only completed confirmation tasks. Use for Policy Change Request OPEN reports, endorsement follow-up, post-change audits, carrier document retrieval, or policy-change confirmation queues."
---

# EZLynx Policy Change Confirmation

Use the existing policy-change task and corresponding discussion as the permanent case record.

## Core controls

- Pull the **Policy Change Request OPEN** report and process every open item.
- Treat the original request as the requested intent, the carrier document as the issued contract, and the EZLynx transaction as the agency record.
- Require a three-way comparison before closing:
  1. Original request and supporting evidence.
  2. Carrier-issued endorsement, declarations, schedule, confirmation, and premium change.
  3. EZLynx transaction and keyed policy data.
- If carrier documents are missing, use the live EZLynx Directory **Document Download** instructions to retrieve them. Never guess a portal or recipient.
- Save documents in the corresponding folder, rename them clearly, apply the correct label, associate them with the policy, and verify they open.
- Add every research step, carrier action, comparison result, correction, wait state, escalation, and completion result to the applicable discussion.
- End every EZLynx note with the exact separate line `ROBIE was here`.
- **MANDATORY Policy Association**: Every note must explicitly associate to the policy via `--policy-number <Policy#>` and include the top header `Policy: #{policy_number} ({line_of_business} - {carrier_name})`.
- Leave the task open when documents, evidence, corrections, or approvals remain outstanding.
- **EZLynx API Utilities (Fast Operations)**:
  - Check applicant details & assigned agent: `scripts/ezlynx_cli.py applicant {applicantId} --json`
  - List documents in Document Library: `scripts/ezlynx_cli.py documents {applicantId} --json`
  - Verify active policies and terms: `scripts/ezlynx_cli.py policies {applicantId} --json`
  - Post discussion audit note (policy associated): `scripts/ezlynx_cli.py note {applicantId} "{NoteText}" --policy-number {policyNumber} --lob "{lob}" --carrier "{carrier}"`

## Lines of Business (LOB) Reference Architecture & Video SOPs

Manual renewals and policy changes span multiple Lines of Business with distinct rating schedules, carrier portals, and validation rules. Detailed reference guides and video training catalogs are modularized in the `references/` directory:

1. **Commercial Automobile**: [references/lobs/commercial_auto.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/ezlynx-policy-change-confirmation/references/lobs/commercial_auto.md)  
   - Vehicle additions/deletions (17-digit VIN decode, garaging zip, comp/coll deductibles).
   - Driver additions/exclusions (DOB, license #, state, MVR checks, signed exclusion forms).
   - Auto ID cards generation and carrier schedules (Progressive FAO, Selective eSelect).

2. **Commercial General Liability**: [references/lobs/commercial_general_liability.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/ezlynx-policy-change-confirmation/references/lobs/commercial_general_liability.md)  
   - Additional Insured forms (`CG 20 10`, `CG 20 37`), Primary & Non-Contributory, Waiver of Subrogation (`CG 24 04`).
   - Exposure basis adjustments (payroll vs. gross receipts) and classification codes.
   - Carrier rules: Coterie direct portal, RT Specialty Interstate MGA vs. QuickHome.

3. **Business Owners Policy & Commercial Package**: [references/lobs/commercial_package_bop.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/ezlynx-policy-change-confirmation/references/lobs/commercial_package_bop.md)  
   - Building and Business Personal Property (BPP) limits, co-insurance clauses.
   - Mortgagee clauses, Lender's Loss Payable (`CP 12 18`), protective safeguards.
   - Franklin Mutual (FMI), Selective, Guard, Utica First.

4. **Workers' Compensation**: [references/lobs/workers_compensation.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/ezlynx-policy-change-confirmation/references/lobs/workers_compensation.md)  
   - Officer/partner inclusion & exclusion forms (NJ WC-106, NY C-105.21), payroll caps.
   - Remuneration adjustments, NCCI class codes, statutory waivers of subrogation (`WC 00 03 13`).
   - AmTrust Online, Berkshire Hathaway Guard, The Hartford.

5. **Commercial Umbrella & Excess Liability**: [references/lobs/commercial_umbrella_excess.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/ezlynx-policy-change-confirmation/references/lobs/commercial_umbrella_excess.md)  
   - Schedule of Underlying insurance synchronization (GL, Auto, Employer's Liability).
   - Excess limit changes ($1M to $10M), MGA binding authority thresholds (JIMCOR / Markel).

6. **Personal Lines (Homeowners, Personal Auto, Dwelling Fire)**: [references/lobs/personal_lines.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/ezlynx-policy-change-confirmation/references/lobs/personal_lines.md)  
   - Mortgage refinances/escrow, dwelling Coverage A, personal auto drivers/vehicles.
   - **QuickHome Routing Rule**: AllRisks / RT Specialty QuickHome is strictly Personal Lines.

7. **Video Walkthroughs & Training Catalog**: [references/video_walkthroughs.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/ezlynx-policy-change-confirmation/references/video_walkthroughs.md)  
   - Step-by-step video recordings (Loom, Google Drive) demonstrating manual portal entries and EZLynx transactions for edge cases.

8. **Carrier & Wholesaler Routing Directory SOP**: [references/carrier_directory_sop.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/ezlynx-policy-change-confirmation/references/carrier_directory_sop.md)  
   - Authoritative routing addresses, portal links, and endorsement follow-up schedules.

---

## R&D boundary

During workflow development, perform read-only research and draft proposed notes, carrier actions, corrections, reassignment, and closure. Stop for confirmation before the first external email, portal submission, policy mutation, discussion-note write, reassignment, or task closure unless the user explicitly authorized that class of action.

## Workflow

### Queue

1. Run the EZLynx **Policy Change Request OPEN** report.
2. Confirm the report includes all open statuses and excludes completed/closed changes.
3. For each row, open the existing account, policy-change task, and corresponding discussion.
4. Capture the applicant, policy number, LOB, carrier, effective date requested, assigned CSR/producer, request date, task due date, and current owner.
5. Read the complete discussion and all attachments before taking action.

### Reconstruct the original request

Build a requested-change packet from the client's request, signed form, email, call note, application, schedule, or internal instruction. Record:

- Requested effective date.
- Add, delete, replace, or modify action.
- Exact affected person, vehicle, location, building, interest, equipment, coverage, limit, deductible, class, exposure, or endorsement.
- Requested values and identifiers.
- Requested premium or billing expectation when stated.
- Required evidence, signatures, approvals, and carrier submission date.

If the request is ambiguous or unsupported, set `request_unclear` and route it to the CSR before judging the carrier-issued change.

### Obtain carrier evidence

1. Check the account's policy-change folder and carrier eDocs for an issued endorsement, revised declarations, schedule, confirmation, invoice, or premium notice.
2. Confirm the document belongs to the correct insured, policy, effective date, and change.
   - **Check policy Named Insured**: Open the specific policy's **Summary tab** and check the **"Named Insured As Listed On The Policy"** field — this is the authoritative name and can differ from the account-level linked-applicants sidebar.
3. If missing, read the live carrier Directory **Document Download** entry.
4. If the method is **Website** or **Download**, retrieve the issued documents from the specified portal.
5. If the method is **Email**, return to the applicant/account **Overview**, select the email icon in the upper-right, and choose the built-in template whose name starts with **Policy Change**. Use the recipient authorized by the Directory entry.
   - **Carrier-Only Follow-Up Rule**: Always email the carrier or underwriter, NEVER the client/insured. Always remove the client's email from the 'To' or 'CC' list when dispatching carrier follow-up emails from EZLynx.
   - **Line of Business Routing Rules (RT Specialty / AllRisks)**:
     - `QuickHome` (`quickhome@allrisks.com`, `QuickHomeEndorsements@`, `QuickHomeQuotes@`) is strictly **Personal Lines**. Never route Commercial Lines to QuickHome inboxes.
     - RT Specialty Commercial Lines endorsements route to `Caroline.shaddow@rtspeciality.com`, `stephanie.tower@rtspecialty.com`, or `interstate.endorsements@rtspecialty.com`.
   - **Automation Center Completion Rule**: Do NOT manually email the client when a policy change is processed. EZLynx Automation Center automatically notifies the client upon transaction completion.
6. Before sending, verify the exact template name, recipient, policy, subject, merge fields, requested change, and attachments. Do not substitute a free-form email when the matching built-in workflow template exists.
7. If Directory instructions are missing or unclear, set `directory_incomplete`; do not guess.
8. If the carrier has not issued the change, set `waiting_for_carrier`, add evidence and a follow-up date, and leave the task open.

### Internal Email & Reporting Standards

When delivering audit reports, queue briefings, or handoffs to leadership and team leads:
- **Visual Formatting**: Emails must be clean, elegant, and visually appealing. Use styled HTML with modern card layouts, rounded borders, clear status badges (green for Matched, amber for Pending), formatted tables with shaded headers, and generous whitespace. Avoid raw markdown or plain text dumps.
- **Transparency**: Include concrete stats, policy numbers, verified fields, actions taken, and specific next steps.

### Document control

For every received document:

1. Use the corresponding policy-change or declarations folder identified by the live agency file-upkeep standard.
2. Rename the file descriptively using the established account/agency convention.
3. Apply the applicable label, normally **Endorsement**, **Change Request Form**, **Signed Change Form**, or another exact live label.
4. Associate it with the correct policy.
5. Open the saved file and verify readability, term, policy number, insured, and effective date.

### Three-way verification

Compare the original request, carrier-issued evidence, and EZLynx record field by field:

- Named insured, additional insured, mortgagee, loss payee, lienholder, or other interest.
- Driver, vehicle, VIN, garaging, use, symbols, limits, deductibles, and effective date.
- Location, building, occupancy, construction, protection, valuation, limits, deductibles, and forms.
- Equipment, scheduled property, serial numbers, values, deductibles, and coverage.
- Class code, operations, payroll, sales, subcontractor cost, area, units, and other rating exposures.
- Coverage added, removed, changed, or declined; limit and deductible.
- Endorsement/form number and edition.
- Premium, fee, billing, and installment effect.
- Cancellation, deletion, or replacement date.

Do not interpret "carrier processed" as proof that the issued change matches the request.

### EZLynx confirmation

- Locate the applicable policy transaction and confirm the correct transaction type and effective date.
- Verify EZLynx matches the carrier-issued document, including all schedules and affected fields.
- Do not key unsupported values or force values into unrelated fields.
- If EZLynx is missing or incorrect, set `ezlynx_correction_required` and leave the task open until an authorized user corrects it and QC rechecks the result.
- If the carrier issued the wrong change, set `carrier_correction_required`, contact the carrier through the approved route, and leave the task open.
- If the carrier issued the requested change but it creates a coverage concern, exclusion, or agency-standards issue, set `coverage_review_required` and route it to the CSR/producer.

### Confirmation report

Add this report to the applicable task discussion:

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
- [missing, wrong, extra, uncertain, or unintended field]

Documents:
- [saved name | folder | label | policy association]

Result: [pass / request_unclear / waiting_for_carrier / carrier_correction_required / ezlynx_correction_required / coverage_review_required]
State: [current state].
Next action: [specific owner and action] | Follow-up: [date or N/A].

ROBIE was here
```

Report `None found` under **Exceptions** only after completing the field-level comparison.

### Closure gate

Close the task only when:

- The original request is clear and supported.
- Carrier-issued documents are received, readable, correctly filed, labeled, and policy-associated.
- The carrier-issued change matches the request, or an approved documented variation.
- EZLynx matches the carrier-issued document.
- Premium/billing impact is recorded or confirmed not applicable.
- Any coverage or exclusion concern is resolved or formally handed off and acknowledged.
- The confirmation report and all actions are in the corresponding discussion.

The Quality Controller is the only role permitted to close the task.
