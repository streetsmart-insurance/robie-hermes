---
name: "ezlynx-policy-change-confirmation"
description: "Review StreetSmart Insurance EZLynx open policy change requests, obtain missing carrier endorsements or declarations, verify the issued change against the original request, confirm or correct the EZLynx transaction, document exceptions, and close only completed confirmation tasks. Use for Policy Change Request OPEN reports, endorsement follow-up, post-change audits, carrier document retrieval, or policy-change confirmation queues."
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
- Leave the task open when documents, evidence, corrections, or approvals remain outstanding.

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
   - **Gotcha — check the policy's own Named Insured field, not just the account-level Linked Applicants sidebar.** The account Overview's "Linked Applicants" list shows entities associated with the account in general (portfolio/holding LLCs, affiliated companies), and a carrier document's insured name can look like it doesn't match anything on that list. That is not proof of a real mismatch. Before flagging a discrepancy, open the specific policy's **Summary tab** and check the **"Named Insured As Listed On The Policy"** field — that is the authoritative name for this policy and it can legitimately differ from (or simply not appear on) the account-level linked-applicants sidebar while still being an exact match. Only treat it as a real mismatch, confirm with the carrier which entity/account the document applies to, and log it under Exceptions if the name still doesn't match after checking that specific field.
3. If missing, read the live carrier Directory **Document Download** entry.
4. If the method is **Website** or **Download**, retrieve the issued documents from the specified portal.
5. If the method is **Email**, return to the applicant/account **Overview**, select the email icon in the upper-right, and choose the built-in template whose name starts with **Policy Change**. Use the recipient authorized by the Directory entry.
6. Before sending, verify the exact template name, recipient, policy, subject, merge fields, requested change, and attachments. Do not substitute a free-form email when the matching built-in workflow template exists.
7. If Directory instructions are missing or unclear, set `directory_incomplete`; do not guess.
8. If the carrier has not issued the change, set `waiting_for_carrier`, add evidence and a follow-up date, and leave the task open.

### Document control

For every received document:

1. Use the corresponding policy-change or declarations folder identified by the live agency file-upkeep standard.
2. Rename the file descriptively using the established account/agency convention.
3. Apply the applicable label, normally **Endorsement**, **Change Request Form**, **Signed Change Form**, or another exact live label.
4. Associate it with the correct policy.
5. Open the saved file and verify readability, term, policy number, insured, and effective date.

### Three-way verification

Compare the original request, carrier-issued evidence, and EZLynx record field by field.

Check every applicable:

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

## Queue Execution Walkthrough

Here's what happens, step by step, when this skill runs on a real (non-test) queue:

1. **Pull the queue.** Run the EZLynx "Policy Change Request OPEN" report and confirm it's showing every open status, nothing already completed or closed.

2. **Open each item.** For every row: open the account, its policy-change task, and the discussion thread. Pull the applicant, policy number, line of business, carrier, requested effective date, assigned CSR/producer, request date, task due date, and current owner. Read the whole discussion and every attachment before touching anything.

3. **Reconstruct what was actually asked for.** From the client's request, signed form, email, call note, or internal instruction, build a clean record of: the effective date requested, whether it's an add/delete/replace/modify, exactly what's affected (driver, vehicle, location, coverage, limit, endorsement, etc.), the requested values, any premium/billing expectation, and what evidence or signatures are required. If this can't be pinned down, the item gets flagged `request_unclear` and goes back to the CSR — it doesn't move forward until it's resolved.

4. **Get the carrier's evidence.** Check the account's document folder and carrier eDocs for the issued endorsement, revised declarations, or confirmation. If it's not there, the skill goes to the carrier's live Directory entry to see how that carrier delivers documents — a portal download, or email using EZLynx's built-in "Policy Change" template if that's the approved channel. It never guesses a portal or invents a recipient. If the carrier hasn't issued anything yet, the item is flagged `waiting_for_carrier` and stays open with a follow-up date.

5. **File the document properly.** Any document that does come in gets saved to the right folder, renamed to the agency's naming convention, labeled correctly (Endorsement, Change Request Form, Signed Change Form, etc.), linked to the correct policy, and opened once to confirm it's readable and matches on term, policy number, and insured.

6. **Run the three-way match.** This is the core of the skill: the original request, the carrier's issued document, and what's actually keyed in EZLynx get compared field by field — named insured, drivers/vehicles, locations, coverages, limits, deductibles, endorsement/form numbers, premium impact, effective dates, all of it. "The carrier processed it" is explicitly not treated as proof it matches what was asked for.

7. **Confirm or flag the EZLynx transaction.** If EZLynx matches the carrier document, it gets confirmed. If EZLynx is wrong or missing something, that's `ezlynx_correction_required` and stays open for a human to fix and re-check. If the carrier itself issued the wrong thing, that's `carrier_correction_required` and goes back to the carrier. If everything matches but it creates a coverage problem, that routes to the CSR/producer as `coverage_review_required`.

8. **Write the confirmation report.** A structured note goes into the task discussion — timestamped, listing what was requested, what the carrier issued, what's recorded in EZLynx, what matched, what didn't (or "None found" only after an actual field-by-field check), the documents filed, and the result code — always ending with `ROBIE was here`.

9. **Close only when everything's actually done.** The task only closes when the request was clear, carrier docs are in and filed, the issued change matches (or has an approved documented exception), EZLynx matches, premium impact is accounted for, and any coverage concern is resolved. Only the Quality Controller role closes it.

One thing worth flagging again: the skill has a built-in R&D boundary — during any test/dev run it stays read-only (research and drafted notes only) and stops before sending an external email, submitting to a portal, mutating a policy, writing a discussion note, or closing a task, unless you've explicitly said to go ahead with that specific action. That's exactly the line we ran into during yesterday's test with the Robie sandbox account.
