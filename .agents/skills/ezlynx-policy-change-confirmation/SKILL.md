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
- **HARDENED INVARIANT 1 — EZLynx Change Request State**: Never close a confirmation task or mark an audit passed if the policy card exhibits `hasPendingChangeRequest: True` or displays the purple badge `Open Change Request effective MM/DD/YYYY`. The Change Request row in the policy History tab must be formally confirmed (`Actions -> Confirm Change -> Apply Download`) before the task can be marked completed.
- **HARDENED INVARIANT 2 — Dec-Less Carrier Verification Gate**: When carriers do not issue endorsement declaration pages for driver or schedule modifications (e.g., Merchants Insurance Group), verification MUST be performed via direct carrier portal inspection (e.g. `files.merchantsgroup.com` / `secure.merchantsgroup.com`) or explicit underwriter confirmation. A generic `$0.00` IVANS download without portal/transaction verification CANNOT be assumed to confirm the change.
- **HARDENED INVARIANT 3 — Anti-Hallucination Baseline Verification for Removals**: Never deduce that a driver or vehicle was removed merely because it is absent from the current EZLynx summary screen. The auditor must verify whether the entity was present in the prior active policy baseline. If an entity was never keyed into EZLynx, its absence is an agency database artifact, not proof of carrier endorsement.
- **HARDENED INVARIANT 4 — Carrier Self-Service Portal Preemption Gate**: When a carrier provides an active, real-time agent portal with online endorsement capabilities (e.g., Berkshire Hathaway GUARD Agency Service Center `gigezrate.guard.com`, Progressive FAO, BHHC), Robie must verify whether the endorsement was entered directly online. If the change was merely emailed or sent manually to an underwriter inbox rather than entered in the portal, Robie detects this absence in the portal's policy history and automatically alerts the assigned CSR and Producer to enter it online for faster turnaround.
- **HARDENED INVARIANT 5 — Intake Channel Disambiguation & Dispatch Matrix**: Systematically classify accounts by carrier intake architecture:
  - *Direct Carrier Portal First* (Progressive FAO, Guard, BHHC, Travelers): Key changes online; retrieve decs via automated portal scraping. Direct calling is bypassed unless portal errors.
  - *Wholesale Broker / MGA Desk* (Jimcor, RT Specialty, TAPCO, Burns & Wilcox): Changes require underwriter negotiation; automated email cadence + autonomous voice AI calling to underwriter desks upon office reopenings.
  - *Dec-Less Driver Schedules* (Merchants Insurance Group): Verify active driver roster on portal; no dec required.
- **HARDENED INVARIANT 6 — Anti-Premature Follow-up (Business Day Turnaround Gate)**: Factor in carrier SLAs (24-48 business hours) and holiday weekends (e.g. Labor Day). Do not flag recent submissions as overdue before carrier SLA has elapsed, and synchronize automated follow-ups with the CSR's scheduled due date.
- If carrier documents are missing, use the live EZLynx Directory **Document Download** instructions to retrieve them. Never guess a portal or recipient.
- Save documents in the corresponding folder, rename them clearly, apply the correct label, associate them with the policy, and verify they open.
- Add every research step, carrier action, comparison result, correction, wait state, escalation, and completion result to the applicable discussion.
- End every EZLynx note with the exact separate line `ROBIE was here`.
- Leave the task open when documents, evidence, corrections, or approvals remain outstanding.

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

7. **Commercial Inland Marine**: [references/lobs/commercial_inland_marine.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/ezlynx-policy-change-confirmation/references/lobs/commercial_inland_marine.md)  
   - Contractor's Equipment Floatters, 5-Year Replacement Cost rule vs ACV, serial numbers, loss payees, small tools floater.

8. **Errors & Omissions (E&O)**: [references/lobs/commercial_errors_and_omissions.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/ezlynx-policy-change-confirmation/references/lobs/commercial_errors_and_omissions.md)  
   - Strict LOB selection ("Errors and Omissions" NOT Professional Liability), defense costs inside/outside, retroactive date preservation.

9. **Garage & Dealers Policy**: [references/lobs/commercial_garage_and_dealers.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/ezlynx-policy-change-confirmation/references/lobs/commercial_garage_and_dealers.md)  
   - Operations split (Auto Service vs Dealership), Symbols 29 & 30, Garagekeepers legal liability, mandatory driver listing.

10. **Video Walkthroughs & Training Catalog**: [references/video_walkthroughs.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/ezlynx-policy-change-confirmation/references/video_walkthroughs.md)  
    - Step-by-step video recordings (Loom, Google Drive) demonstrating manual portal entries and EZLynx transactions for edge cases.

11. **Carrier & Wholesaler Routing Directory SOP**: [references/carrier_directory_sop.md](file:///Users/carloferrara/Documents/antigravity/happy-fermi/.agents/skills/ezlynx-policy-change-confirmation/references/carrier_directory_sop.md)  
    - Authoritative routing addresses, portal links, and endorsement follow-up schedules.

12. **Automated Verification Pipeline**: `scripts/policy_change_verification_pipeline.py`  
    - CLI execution engine for automated three-way verification and confirmation reporting.

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

### Obtain carrier evidence & Hand-off Protocol

1. Check the account's policy-change folder, declarations folder, and carrier eDocs for an issued endorsement, revised declarations, schedule, confirmation, invoice, or premium notice.
2. Confirm the document belongs to the correct insured, policy, effective date, and change.
   - **Check policy Named Insured**: Open the specific policy's **Summary tab** and check the **"Named Insured As Listed On The Policy"** field — this is the authoritative name and can differ from the account-level linked-applicants sidebar.
3. **If carrier documents are missing**:
   - Route and delegate this account to the dedicated **`carrier-policy-document-retrieval`** skill (`.agents/skills/carrier-policy-document-retrieval/SKILL.md`).
   - The retrieval skill executes the tri-channel hunting pipeline:
     1. **Carrier Portals & IVANS eDocs**: Direct download (e.g., FAO, eSelect, Coterie, Geico, AmTrust).
     2. **EZLynx Carrier Email Follow-Up**: Using official template `Policy Change Request Change Request Follow up Email Templates (Carrier)` (strict carrier-only routing; never client; personal vs commercial routing rules).
     3. **Autonomous Voice AI Phone System**: Outbound call via `scripts/carrier_policy_change_caller.py` (Bland AI `+17322986745`) to carrier endorsement/servicing desks when written follow-ups are overdue (>3 business days) or carrier services by phone.
   - Leave the task open in state `waiting_for_carrier` or `carrier_followup_dispatched`.
4. **When documents are retrieved and filed**:
   - Status transitions to `ready_for_confirmation`.
   - Proceed immediately to **Document control** and **Three-way verification** below.

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
