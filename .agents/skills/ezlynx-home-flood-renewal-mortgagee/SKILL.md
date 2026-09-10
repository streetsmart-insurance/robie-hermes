---
name: ezlynx-home-flood-renewal-mortgagee
description: "Process StreetSmart Insurance homeowners and flood renewal/mortgagee workflows in EZLynx. Use for 30–45-day renewal queues, Homeowners Renewal or Flood Renewal discussions, payer and lender verification, MyCoverageInfo/MyInsuranceInfo uploads, producer handoffs, weekly payment monitoring, or 20-day CSR escalation."
---

# Homeowners and Flood Renewal/Mortgagee

Use one policy-linked renewal discussion for the entire lifecycle:

- Homeowners: **Homeowners Renewal** or the equivalent live renewal-review task.
- Flood: **Flood Renewal**, **Upcoming renewal**, or the equivalent live flood-renewal task.

Do not create a parallel Mortgagee Bill Policies task when the applicable renewal task exists.

## Ownership

- Bot: document retrieval/filing, billing and payer identification, lender verification, lender upload after producer clearance, and weekly payment monitoring.
- Producer: coverage review, rescoring, underwriting, retention, and client conversation.
- CSR: immediate exceptions and cases unpaid 20 days before expiration.

## Safeguards

- Use Carrier eDocs when the current renewal is already downloaded; do not retrieve a duplicate.
- Otherwise require the carrier's live **Document Download** route.
- Keep every action and handoff in the same renewal discussion.
- Never send an unreviewed renewal to a lender; require producer clearance when coverage, premium, underwriting, or retention review is pending.
- Stop normal delivery for cancelled/nonrenewed policies and route ownership to the producer unless an active task already owns the issue.
- End each note with the exact separate line `ROBIE was here`.
- Keep the task open until payment or a documented CSR handoff resolves the case.

## R&D boundary

During workflow development, perform read-only research and draft proposed notes and actions. Stop for confirmation before the first email send, portal submission, policy mutation, discussion-note write, reassignment, or task closure unless the user explicitly authorizes that class of action.

## Workflow

### Queue and task

1. Use the Renewal/Expiration queue filtered to homeowners and flood; include download and manual policies.
2. Work 30–45 days before expiration. Under 30 days is an overdue exception; over 45 days waits.
3. Open the existing Homeowners Renewal or Flood Renewal task and full discussion.
4. If no applicable renewal task exists, document all tasks found and ask Ana, Dani, and Gabriela which task should own the work before creating anything.

### Efficient queue triage (start here for a full sweep)

The Retention Center Overview list is slow to page through by hand for a full 30–45-day sweep, and its own **Status** and **Renewal Manager** columns are unreliable — most households showing a blank Status or no Renewal Manager are still being actively worked; the columns just aren't synced to reflect it. Do not treat a blank cell there as proof of neglect.

1. Instead, start from **Reports → Saved Reports → Retention Center → "Home Flood Renewal Queue - ROBIE"** (a Looker-backed report; internally titled "Renewal Detail"). It gives one row per renewal with Account Name, Policy Number, Master Company, Line of Business, Renewal Status, Retention Center Risk Level, premiums/change amount, Branch, Assigned Producer, and more — filterable and sortable, unlike the Overview list.
2. Filter `RenewalDetail Line of Business` to Homeowners, Flood. Set the `RenewalDetail Renewal Manager` filter operator to **is blank** to shortlist the households most likely to be genuinely untouched this cycle (this typically narrows ~50+ households down to a much smaller list, e.g. ~13). Check whether a renewal-date range filter is also needed to scope to the 30–45-day window specifically, since the report may not default to that window.
3. Treat the "Renewal Manager is blank" shortlist as candidates only, not confirmed gaps — most will still turn out to be in progress. Confirm each one via the account's **Activity tab** (see below) before flagging it as a real gap.
4. A custom filter set named **"Home Flood Renewal Verification"** may already be saved against this report from a prior session — check the "Custom Filter Set" dropdown before rebuilding filters from scratch. A related saved filter set, **"Retention Center Unassigned"** (under "Created by Me"), filters `RenewalDetail Renewal Manager` to the specific value "Unassigned" rather than using the "is blank" operator, and does not itself restrict Line of Business — add the Homeowners/Flood Line of Business filter on top of it if you start from that saved set.

### Confirming real prior activity

To see what's actually happened on an account this cycle, open the applicant record and go to its **Activity** tab (Overview → Activity, or `.../web/account/{id}/activity`). This is the reliable source of truth — it shows every task, note, automation-center email/SMS, and carrier message in one chronological feed, including whether the renewal-ready email was sent and opened.

Do **not** rely on the "Add Note" panel's discussion-title search (typing into "Search Discussions by Title") as your only check — many accounts have multiple identically-titled discussions (e.g. three separate "Homeowners Renewal" threads from different years), and the search can surface a stale prior-year thread with no current-cycle notes even though the account is actively being handled elsewhere. If you use that panel, confirm you've opened the discussion instance number that matches the current term (e.g. "Homeowners Renewal / Mortgage Verification (2)" vs "(3)"), or just check the Activity tab instead.

### Preparation

1. Confirm policy status, renewal term, carrier, billing type, feed type, payer, producer, CSR, and prior activity.
2. Obtain the declaration, invoice/billing evidence, forms, and endorsements.
3. File, rename, label, associate, and verify documents.
4. Compare material renewal changes and prepare a producer handoff.

### Producer gate

The producer reviews coverage, premium, rescoring opportunities, underwriting concerns, retention, and the client conversation. Record `producer_review_complete` before routine lender delivery when producer judgment is required.

### Payer paths

#### Mortgagee pays

Follow the **Lender Verification & Delivery Waterfall**:

1. **Digital Portals (Level 1)**:
   - Check third-party verification portals: **MyCoverageInfo**, **MyInsuranceInfo** (MyInsInfo), **Insurance Express**, or applicable lender portal.
   - Verify loan number and property ZIP. If found, upload the declaration and invoice after producer clearance, record confirmation evidence, and set `waiting_for_mortgagee_payment`.
2. **Direct Email / Fax (Level 1 Alternate)**:
   - If the mortgage company provides a dedicated insurance/escrow email or fax number, transmit the renewal packet directly and record transmission confirmation.
3. **Autonomous Phone Call to Mortgage Company (Level 2)**:
   - If portal lookup fails, loan/ZIP is rejected, servicing transferred, or no direct portal/email/fax is verified, trigger an outbound call to the mortgage company's **Insurance Department / Escrow Department** from StreetSmart Caller ID `+1 (732) 298-6745`.
   - **IVR & Hold Handling**: Barge through automated prompts, press `0` repeatedly, or state "Representative" / "Insurance Department" / "Escrow" to bypass IVR loops. Stay on the line through hold music until a live human answers.
   - **Authentication Dossier**:
     - Borrower Name, Loan Number, Property Address (street, city, state, ZIP).
     - **Date of Birth (DOB)**: Pulled from EZLynx applicant profile (e.g., `MM/DD/YYYY`).
     - **Social Security Number (SSN)**: Agency does not retain full SSN. When asked, instruct the voice agent to state: *"I don't have the full Social Security Number on file, but I have the borrower's Date of Birth: [DOB]."*
   - **Call Objectives**:
     - Verify current mortgagee clause and confirm loan number on file.
     - Inquire about escrow status and renewal premium disbursement schedule.
     - Obtain the exact transmission destination: portal URL, insurance department email, or fax number.
   - **Audit & Callback**: Automatically thread the call recording URL, full transcript, and structured summary back into the applicant's EZLynx renewal discussion thread.
4. **Payment Monitoring**:
   - Check payment weekly and add a dated note after each check ending with `ROBIE was here`.

#### Insured pays

- Producer owns the client renewal conversation and electronic delivery when appropriate.
- Direct bill + carrier download + physical mail: carrier is responsible; do not duplicate mailing. Monitor payment weekly.
- Agency bill + manual + physical mail: agency uses the approved mailing process and records evidence.

### Escalation

Escalate immediately for lender-servicing changes, invalid loan/ZIP, portal rejection, conflicting payer information, client clarification, cancellation/nonrenewal, or missing/contradictory documents.

If payment is not confirmed 20 days before expiration, loop the CSR into the same discussion with the full history. The bot may continue status checks, but the CSR owns direct lender/client follow-up.

### Completion

Close only after payer/lender verification, required delivery, producer work, payment confirmation, and all notes/evidence are complete, or after a formally documented CSR handoff under the escalation rule.
