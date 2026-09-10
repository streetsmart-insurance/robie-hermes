# StreetSmart Insurance: Robie Autonomous Lead Outreach Pilot Proposal

**Target Reviewer**: Jake Ferrara, Agency Principal  
**Author**: Carlo Ferrara / Robie Engineering  
**Version**: 1.0.0 (Controlled Pilot)  
**Status**: Ready for Executive Sign-off  

---

## 1. Executive Summary
This proposal establishes a controlled, highly guarded pilot for Robie's outbound multi-channel outreach engine using Bland AI conversational voice, SMS, and email. The system targets three high-value Sales Center opportunity funnels:
1. **New Inbound & Unreached Leads** (Immediate acknowledgment + Days 1, 3, 7).
2. **Quoted Prospects** (Days 1, 3, 7 post-quote proposal review).
3. **X-Date Renewal Opportunities** (T-45, T-30, T-14 days prior to policy expiration).

Every touch is gated by an atomic **6-Factor Eligibility Gate**, instant **Stopping Logic**, and a cross-workflow **Global Suppression Registry** ensuring zero unwanted contact, strict TCPA/DNC adherence, and complete E&O protection.

---

## 2. Pilot Parameters & Scope
To ensure zero operational disruption and flawless customer experience, the pilot is strictly bounded:

| Parameter | Pilot Configuration | Rationale / Safety Guard |
| :--- | :--- | :--- |
| **Assigned Producer** | **Jake Ferrara** | Single producer champion with configured warm-transfer DID `(732) 481-2520`. |
| **Lines of Business (LOB)** | **Personal Auto** (Primary) & **Homeowners** (Secondary) | Standardized quoting guidelines and rapid re-rating workflows. |
| **Approved Lead Sources** | `StreetSmart Website`, `Website Inbound`, `EverQuote`, `QuoteWizard`, `Sales Center X-Date` | Verified inbound consent with TCPA checkboxes on file. |
| **Cohort Size** | **25 to 50 recent leads** | Manageable batch for daily producer follow-up and transcript review. |
| **Daily Dial Ceiling** | Max **20 voice dials / day** | Prevents overwhelming producer transfer availability. |
| **Telephony Caller ID** | `+1 (732) 298-6745` | Monmouth County NJ local trust caller ID. |
| **Agency Callback** | `(732) 462-8343` | Main office routing, spoken: `"seven three two, four six two, eight three four three"`. |
| **Warm Transfer Number** | `+1 (732) 481-2520` | Jake Ferrara's direct transfer line (only when prospect consents). |

---

## 3. Cadence Timing Schedules

### Funnel A: New Inbound & Unreached Leads
- **Touch 0 (Immediate Ack - within 2 minutes)**: Automated SMS + Email confirming receipt of inquiry.
- **Touch 1 (Day 1 - ~24h post-inquiry)**: Bland Voice Call offering 2-min chat with Jake + Email.
- **Touch 2 (Day 3 - 48h post-Touch 1)**: Email check-in + SMS.
- **Touch 3 (Day 7 - 96h post-Touch 2)**: Final polite Voice Call + Email closing file if unreached.

### Funnel B: Quoted Prospects
- **Touch 1 (Day 1 - ~24h post-quote)**: Bland Voice Call reviewing coverage options + Email proposal link.
- **Touch 2 (Day 3 - 48h post-Touch 1)**: Email coverage comparison + SMS check-in.
- **Touch 3 (Day 7 - 96h post-Touch 2)**: Final proposal review call + Email.

### Funnel C: X-Date Renewal Opportunities
- **Touch 1 (T-45 Days)**: Bland Voice Call offering complimentary re-market quote + Email.
- **Touch 2 (T-30 Days)**: Bland Voice Call checking for vehicle/driver changes + Email.
- **Touch 3 (T-14 Days)**: Urgent rate lock Bland Voice Call + SMS prior to auto-renewal.

---

## 4. Safety Controls & Compliance Invariants

### 1. Grounded Conversational Claims (Anti-Hallucination Invariant)
Robie is explicitly prohibited from stating that a quote is expiring or that a rate is increasing unless verified by `verified_expiration_date` or `verified_rate_guarantee_date` in EZLynx.

### 2. Immediate Stopping Logic & Global Suppression Record
Robie instantly aborts all active and scheduled outreach when:
- Account becomes `ActiveClient` or Opportunity moves to `Won`.
- Opportunity marked `Lost`, `Dead`, or closed.
- Verbal opt-out in call transcript (`"stop calling me"`, `"take me off your list"`, `"not interested"`).
- Inbound SMS reply contains `STOP`, `UNSUBSCRIBE`, `CANCEL`, `QUIT`, or `END`.
- Inbound Email containing unsubscribe intent.
- EZLynx Discussion note containing keyword trigger (`Robie Stop`, `DNC`, `Opt Out`).
- Manual employee pause/cancel action.

*Every opt-out automatically creates an immutable SHA-256 hashed entry in the Global Suppression Registry, permanently blocking future dials across all Robie workflows.*

### 3. Contact Hours & Timezone Rules
- Outbound voice dials only permitted between **9:00 AM and 6:00 PM local recipient time** (inferred from area code / state).
- **Monday through Friday only**. Outreach blocked on weekends and national holidays.
- Minimum **24-hour voice cooldown** between call attempts to the same phone number.

---

## 5. Escalation Matrix & E&O Safeguards

| Scenario | Spoken Assistant Response | System Action |
| :--- | :--- | :--- |
| **Prospect Wants to Bind** | *"That's wonderful! Let me connect you directly with Jake right now so he can finalize your application and bind your coverage."* | Live warm transfer to `+17324812520`. If unanswered, high-priority EZLynx task + SMS alert to Jake. |
| **Coverage Advice Requested** | *"As an automated assistant, I can't advise on specific coverage limits or legal options, but Jake is licensed and right here to advise you. Let me get him on the line."* | **E&O Guard**: Transfers to Jake immediately. Never recommends limits. |
| **Human Requested** | *"Of course! Let me connect you with Jake right now."* | Warm transfer to `+17324812520`. |
| **Complaint / Anger** | *"I completely understand, and I apologize for any frustration. Let me connect you with Jake right away so we can take care of this."* | Immediate transfer + high-priority management alert. |
| **Confusion / Identity Query** | *"No problem at all! This is Robie from StreetSmart Insurance following up on your recent quote request..."* | Context clarification + offers async email or callback. |

---

## 6. Pilot Phasing & Sign-Off Milestones
1. **Phase 1: Internal Dry Testing (COMPLETED)**:
   - Executed full state machine passes using internal test accounts (Carlo Ferrara & Jake Ferrara).
   - Verified payload structures, caller ID `+1 (732) 298-6745`, callback `(732) 462-8343`, and stopping triggers.
2. **Phase 2: Cohort Shadow Mode (COMPLETED)**:
   - Evaluated 30 candidate leads in Shadow Mode.
   - 25 approved, 5 correctly blocked by safety rules. Cost: **$0.00**.
3. **Phase 3: Controlled Live Pilot (Awaiting Jake's Approval)**:
   - Load initial cohort of 25–50 Sales Center leads assigned to Jake Ferrara.
   - Monitor daily transcripts and live warm-transfers.
