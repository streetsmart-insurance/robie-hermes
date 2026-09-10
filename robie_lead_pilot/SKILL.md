---
name: "streetsmart-robie-lead-cadences"
description: "Operational runbook, state machine specifications, Bland AI telephony rules, and E&O escalation protocols for Robie autonomous lead and quote follow-up sequences (Inbound, Quoted, X-Date)."
---

# StreetSmart Insurance: Robie Autonomous Lead & Quote Outreach Cadences

This skill defines the agency operational runbook, multi-channel state machines, conversational Bland AI telephony rules, pre-flight eligibility checks, and E&O escalation protocols for Robie's outbound lead sequences.

---

## 1. Core Architecture & Use Cases

Robie operates across three high-value Sales Center funnels:

| Funnel | Target Audience | Cadence Schedule | Primary Objective |
| :--- | :--- | :--- | :--- |
| **A. New Inbound Leads** | Web form inquiries, unreached aggregator leads (`StreetSmart Website`, `EverQuote`, `QuoteWizard`) | **Immediate Ack** (SMS+Email), followed by **Day 1**, **Day 3**, and **Day 7** | Qualify interest and warm-transfer prospect to assigned producer. |
| **B. Quoted Prospects** | Prospects with completed carrier proposals in EZLynx | **Day 1**, **Day 3**, and **Day 7** post-quote | Review coverage, resolve questions, and transfer to bind. |
| **C. X-Date Opportunities** | Upcoming policy renewal expirations from Sales Center | **T-45**, **T-30**, and **T-14** days before expiration | Offer re-market comparison and capture renewal business. |

---

## 2. Telephony & Communication Invariants

- **Caller ID**: `+1 (732) 298-6745` (Monmouth County, NJ trust caller ID).
- **Callback Phone Number**: `(732) 462-8343` (Main office). Spoken form: *"seven three two, four six two, eight three four three"*. Never cite a producer's personal DID in voicemails or async callbacks.
- **Warm Transfer Number**: Assigned producer DID (Pilot: Jake Ferrara `+17324812520`). Only initiated when prospect explicitly agrees to speak with an agent.
- **Strict Anti-Urgency / Rate Grounding Rule**: Robie is strictly prohibited from claiming a rate will increase or a quote will expire unless explicitly backed by `verified_expiration_date` or `verified_rate_guarantee_date` in EZLynx.

---

## 3. The 6-Factor Atomic Eligibility Gate

Before ANY touch (voice call, SMS, or email) is dispatched, the candidate MUST satisfy all 6 rules:

1. **Lead Source & Assigned Producer**:
   - Lead source must match approved pilot list (`StreetSmart Website`, `Website Inbound`, `EverQuote`, `QuoteWizard`, `Sales Center X-Date`).
   - Assigned producer must be the approved pilot producer (`Jake Ferrara`) with active transfer DID.
2. **Client & Opportunity Status**:
   - Applicant must be `ProspectLead` (never `ActiveClient` or `InactiveClient`).
   - Opportunity stage must be open (`New`, `Unreached`, `Quoted`), never `Won`, `Lost`, `Dead`, or `Closed`.
3. **Contact Consent**:
   - Express consent flag must be `True` for the target channel.
4. **Global Suppression & DNC**:
   - Phone, email, or applicant ID must NOT match any record in the SHA-256 Global Suppression Registry.
5. **Cooldown & Duplicate Outreach**:
   - Strictly minimum **24 hours** between voice calls to the same phone number.
   - Minimum 4 hours between any channel touches.
6. **Appropriate Contact Hours**:
   - **9:00 AM to 6:00 PM** in recipient's local timezone (inferred from area code / state).
   - **Monday through Friday only**. Outreach is completely blocked on weekends and federal holidays.

---

## 4. Instant Stopping Logic & Global Suppression Record

Cadences must cease immediately upon any of the following triggers:
- **Account Bound**: Applicant becomes `ActiveClient` or Opportunity stage moves to `Won` -> Halts with `WON_BOUND`.
- **Closed / Lost**: Opportunity marked `Lost` or `Dead` -> Halts with `LOST_CLOSED` / `DEAD`.
- **Verbal Opt-Out**: Prospect states *"stop calling me"*, *"remove me"*, *"not interested"*, or *"wrong number"* in call transcript or disposition -> Halts with `OPT_OUT_CALL` and creates Global Suppression Record.
- **SMS Stop**: Inbound message contains `STOP`, `UNSUBSCRIBE`, `CANCEL`, `QUIT`, or `END` -> Halts with `OPT_OUT_SMS_STOP` and creates Global Suppression Record.
- **Email Unsubscribe**: Inbound email expressing opt-out intent -> Halts with `OPT_OUT_EMAIL` and creates Global Suppression Record.
- **EZLynx Note Keyword**: Discussion note entered by employee with trigger phrase `Robie Stop`, `Stop Robie`, `DNC`, or `Opt Out` -> Halts with `EZLYNX_NOTE_KEYWORD` and creates Global Suppression Record.
- **Employee Manual Action**: Employee clicks pause or cancel -> Halts with `EMPLOYEE_PAUSE`.

### The Global Suppression Registry:
Whenever an opt-out occurs, an immutable record is saved with SHA-256 hashes of the phone number and email address. This ledger is checked by **ALL** Robie workflows, ensuring a prospect opting out of a lead cadence is never contacted by a renewal or quote cadence.

---

## 5. Conversational Escalation & E&O Safeguards

| Scenario | Spoken Assistant Script | System Action | E&O Level |
| :--- | :--- | :--- | :--- |
| **Wants to Bind** | *"That's wonderful! Let me connect you directly with {Producer} right now so they can finalize your application and bind your coverage."* | Live warm-transfer to producer DID. | HIGH |
| **Coverage Advice** | *"As an automated assistant, I can't advise on specific coverage limits or legal options, but {Producer} is licensed and right here to advise you. Let me get them on the line."* | **E&O Guard**: Never recommend limits or explain policy exclusions. Transfer to producer. | HIGH |
| **Asks for Human** | *"Of course! Let me connect you with {Producer} right now."* | Warm-transfer to producer DID. | LOW |
| **Complaint / Anger** | *"I completely understand, and I apologize for any frustration. Let me connect you with {Producer} right away so we can take care of this."* | Immediate transfer + urgent management alert. | MEDIUM |
| **Confusion** | *"No problem at all! This is Robie from StreetSmart Insurance following up on your recent quote request..."* | Context clarification + offers callback or email. | LOW |

---

## 6. EZLynx Audit Logging Standards

Every interaction must be appended to the applicant's EZLynx file with the mandatory signature:
```text
ROBIE was here
```

Example call log note format:
```text
📞 ROBIE OUTBOUND CALL COMPLETED — Touch 1 (INBOUND_LEAD)
Target Phone: +17325550101
Call ID: c_bland_8841920
Outcome: ANSWERED
Transfer: SUCCESS (+17324812520 - Jake Ferrara)

Executive Summary:
Customer answered and confirmed interest in Personal Auto quote. Robie successfully warm-transferred to Jake Ferrara.

Audio Recording: https://api.bland.ai/recordings/c_bland_8841920.mp3

ROBIE was here
```

---

## 7. Execution CLI Commands

- **Run Full Test Suite**:
  ```bash
  cd /Users/carloferrara/.gemini/antigravity/scratch/robie-bland-lead-pilot
  PYTHONPATH=. python3 -m unittest discover -s tests -p "test_*.py"
  ```

- **Run Internal Dry Test (Carlo Ferrara & Jake Ferrara test accounts)**:
  ```bash
  cd /Users/carloferrara/.gemini/antigravity/scratch/robie-bland-lead-pilot
  PYTHONPATH=. python3 src/simulation/dry_test_runner.py
  ```

- **Run Cohort Shadow Mode (Zero dials, $0.00 spend audit)**:
  ```bash
  cd /Users/carloferrara/.gemini/antigravity/scratch/robie-bland-lead-pilot
  PYTHONPATH=. python3 src/simulation/run_shadow_cohort.py
  ```
