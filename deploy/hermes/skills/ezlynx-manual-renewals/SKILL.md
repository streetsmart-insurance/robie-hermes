---
name: "ezlynx-manual-renewals"
description: "Process StreetSmart Insurance personal and commercial manual renewals in EZLynx. Use for Retention Center Manual-only queues, carrier renewal-document retrieval, underwriter requests, renewal entry, policy coverage QC, client delivery, or 30-day CSR escalation."
job_type: "manual_renewal_verification"
status: "Testing"
production_ready: false
---

# EZLynx Manual Renewals

Work inside the existing policy-linked **Renewal Manual** task and discussion.

## Safeguards

- Use the live carrier **Document Download** entry as the retrieval source of truth.
- Website/Download means use the carrier portal. Email means use the approved EZLynx carrier-request template and listed recipient.
- If the entry is missing or unclear, set `directory_incomplete`, ask Ana, Dani, and Gabriela in the operations Google Chat, identify yourself as **Robie on behalf of Carlo**, and keep the task open.
- Never include credentials in notes. Stop at login/MFA when access is required.
- Add all actions, waiting states, documents, handoffs, QC findings, and completion evidence to the existing task.
- End each discussion note with the exact separate line `ROBIE was here`.
- Close unnecessary tabs after preserving evidence.

## R&D boundary

During workflow development, perform read-only research and draft proposed notes and actions. Stop for confirmation before the first email send, portal submission, policy mutation, discussion-note write, reassignment, or task closure unless the user explicitly authorizes that class of action.

## Completion gate

Do not close until the renewal and invoice are obtained, documents are filed and verified, the future RWL transaction is accurate, coverage QC passes, required delivery is complete, and unresolved discrepancies are cleared.

## Workflow

### Queue

1. Open Retention Center.
2. Sort Renewal List by Policy Type.
3. Select Personal or Commercial.
4. Open Expiration List.
5. Filter **Manual only**.
6. Repeat for the other policy type.

Personal tasks generate 60 days before expiration; commercial tasks generate 90 days before expiration. Carriers normally generate renewals 30–45 days before expiration.

### Processing

1. Open the existing Renewal Manual task and full discussion.
2. Confirm policy number, LOB, carrier, term, billing type, CSR, producer, due date, and prior attempts.
3. Read **Download or Manual** for feed context, then require the specific **Document Download** route.
4. Check for an equivalent prior request to avoid duplicates.
5. Obtain the renewal declaration, forms, endorsements, and invoice.
6. File documents in the corresponding renewal folder, rename clearly, apply **Renewals**, associate them with the policy, and reopen them to verify readability.
7. Renew from the existing policy using **Service > Renew > Renew & Edit Policy**.
8. Verify the future RWL term, carrier, billing, premiums, department, and service team.
9. Compare the expiring policy, carrier renewal, and EZLynx renewal across all applicable coverages, limits, deductibles, named insureds, additional interests, locations, buildings, class codes/exposures, vehicles, drivers, equipment, schedules, forms, endorsements, and exclusions.
10. Produce a Renewal QC Report with verified items, missing/incorrect entries, renewal changes, standards alerts, uncertainties, and pass/review/correction result.

### Communication and escalation

- Use **Manual Renewal Apps Reach Out** for an applicable nonautomatic client status/update; enter verified case-specific information.
- Agency bill: use **Policy Delivery** after QC.
- Direct bill: document carrier delivery responsibility and do not automatically duplicate it.
- If unavailable 30 days before expiration, reassign the same task to the CSR with attempts and blocker documented.
- `documents_not_generated`, `login_or_mfa_required`, `waiting_for_underwriter`, and `directory_incomplete` remain open states.
