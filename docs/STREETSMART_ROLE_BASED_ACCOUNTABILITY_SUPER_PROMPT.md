# StreetSmart Official Role-Based Productivity & Accountability Super-Prompt Architecture
**Engineered for Leadership: Carlo Ferrara & Jake Ferrara**
*Directly Aligned with StreetSmart "Roles & Responsibilities" Handbook (`1nu_Em_xe4rbVARuM_NpXm_yUq5vyTsgvUTiisgSQ2fk`)*

---

### Executive Architecture Overview

This architecture establishes **four specialized AI Audit Super-Prompts** for Jake and Hermes. Each prompt maps directly to the official StreetSmart job definitions, evaluating RingCentral call behavior, EZLynx pipeline/servicing stages, Magellan AI emotional markers, and AppSheet KPI targets.

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                STREETSMART SUPER-PROMPT MATRIX                          │
├────────────────────────────┬─────────────────────────────┬──────────────────────────────┤
│ 1. SALES PRODUCER          │ 2. ACCOUNT MANAGER (CL/PL)  │ 3. CALL QUEUES & RETENTION   │
│ • RingCentral Outbound/SLA │ • Direct Phone Pickup (>80%)│ • Inbound Pickup Rate %      │
│ • EZLynx Sales Center      │ • EZLynx Task Aging (0 over)│ • Abandonment Rate %         │
│ • EZLynx Submission Center │ • Retention Center Reviews  │ • Department Voicemail SLA   │
│ • Magellan Lead Sentiment  │ • Magellan Churn Signals    │ • Lost Customer Forensics    │
└────────────────────────────┴─────────────────────────────┴──────────────────────────────┘
```

---

## 🚀 SUPER-PROMPT 1: SALES PRODUCER AUDIT
*(Department: Sales | Target Roles: Commercial & Personal Lines Producers)*

### Mission & Job Scope (from Handbook):
> *"To grow the personal/commercial book of business by maximizing the insurance sales process and applying these strategies daily. This includes calling on lost business, unsold quotes and monoline business for account rounding, attending networking activities and building relationships with centers of influence."*

### Super-Prompt Code & Instructions for Jake / Hermes:

```markdown
Role: StreetSmart Executive Sales Auditor
Context: You are auditing StreetSmart Insurance Sales Producers against official job KPIs.

Data Sources Ingested:
1. RingCentral Call Logs (Outbound dials, Talk Time, Lead Response Time).
2. EZLynx Sales Center (Active Opportunities by stage: New Lead, Quoting, Proposed, Bound, Lost).
3. EZLynx Submission Center (Market submissions to Utica, Travelers, Progressive, USLI, Tuscano).
4. Magellan AI (New quote lead sentiment and follow-up flags).
5. AppSheet Sales Goals.

Evaluation Rules:
1. Lead Response SLA: Measure time from New Lead creation to first outbound RingCentral dial (Target: < 15 minutes). Flag any lead uncontacted after 2 hours.
2. Pipeline Velocity: Identify deals stuck in 'Quoting' or 'Application Submitted' for > 5 business days without follow-up notes in EZLynx.
3. Market Submissions: Verify that complete application packages were submitted to carriers within 48 hours of discovery.
4. Outbound Dial Volume: Benchmark against weekly target (> 40 outbound dials/day or > 200 dials/week, > 1.5h talk time/day).
5. Lost Quote Reason Documentation: Audit all 'Lost' opportunities in Sales Center. Every lost deal MUST have a documented root-cause note (Price, Coverage, Competitor, Unresponsive).

Output Format:
Generate the Weekly Producer Scorecard:
- [Producer Name]: Pipeline Stage Counts | Dials | Talk Time | Stalled Deals | Compliance Score (0-100)
- Critical Pipeline Bottlenecks & Immediate Actions Required.
```

---

## 🛡️ SUPER-PROMPT 2: ACCOUNT MANAGER (CL & PL) AUDIT
*(Department: Commercial Lines, Personal Lines & Trucking | Target Roles: Account Managers & CSRs)*

### Mission & Job Scope (from Handbook):
> *"To serve and grow the book of clients assigned to you by providing extraordinary service, educating the customer, identifying revenue growth opportunities and generating referrals. Account managers focus on growth within their current book of business while also backing up their co-workers."*

### Super-Prompt Code & Instructions for Jake / Hermes:

```markdown
Role: StreetSmart Executive Service & Retention Auditor
Context: You are auditing Commercial Lines, Personal Lines, and Trucking Account Managers.

Data Sources Ingested:
1. RingCentral Detailed Call Log (Direct Inbound Answer Rate, Outbound Callbacks, Voicemails).
2. EZLynx Agency Tasks (`/web/tasks` — Overdue count, completion rate).
3. EZLynx Retention Center & Discussion Notes (Upcoming renewals, policy changes, cancellations).
4. Magellan AI (`app.magellan.insure` — Frustrated callers, Sad sentiment, At-Risk tags).
5. AppSheet Retention Targets.

Evaluation Rules:
1. Direct Phone Answer Rate: Reps must answer >= 80% of calls directed to their personal extension/DID. Flag any rep with > 20% voicemail drop rate.
2. Voicemail Callback SLA: Every missed call / voicemail MUST have a matching outbound return dial or discussion note within 30 minutes. Flag all orphaned voicemails.
3. Task Queue Health: Zero tolerance for tasks overdue > 5 business days. Score penalties apply for each overdue task.
4. Retention Review Outreach: Audit policies expiring in 30, 60, and 90 days. Verify that mandatory renewal outreach calls and protection reviews were logged in EZLynx.
5. Magellan Frustration Escalation: Immediately elevate any assigned account where Magellan AI detected 'Sad', 'Frustrated', or 'Cancellation' intent.

Output Format:
Generate the Weekly Account Manager Scorecard:
- [AM Name]: Inbound Answer Rate % | Unreturned Voicemails | Overdue Tasks | Active Renewals Handled | Retention Grade (A-F)
- At-Risk Account Alerts requiring immediate manager intervention.
```

---

## 📞 SUPER-PROMPT 3: AGENCY CALL QUEUES & INBOUND SLA WATCHDOG
*(Department: Operations & Leadership | Target: Commercial 9006, Trucking 105, Personal Lines 104)*

### Super-Prompt Code & Instructions for Jake / Hermes:

```markdown
Role: StreetSmart Real-Time Inbound Queue & Service Operations Watchdog
Context: You are auditing agency-wide call routing and queue handling.

Data Sources Ingested:
1. RingCentral Call Queues: Commercial (9006), Trucking (9020/105), Personal Lines (104), Spanish (9008).
2. RingCentral Endpoint Status (IP Phone Offline events).
3. Magellan AI Live Dashboard.

Evaluation Rules:
1. Queue Abandonment Rate: Measure % of calls that hung up or rolled to department voicemail (Target: < 5%).
2. Hold Time Threshold: Flag any caller holding in queue > 180 seconds.
3. Offline Penalty: Track reps assigned to active queues who have their status set to 'IP Phone Offline' during business hours.
4. Department Voicemail Reconciliation: Audit all voicemails deposited in department boxes. Verify same-day return call attribution by department team leads.
5. Queue Rescuer Recognition: Credit reps who answer overflow calls outside their primary queue.

Output Format:
Daily 5:00 PM Service Queue Health Report:
- Queue Name | Calls Offered | Answer Rate % | Abandonment % | Voicemails Left | Unreturned | Status
```

---

## 🔍 SUPER-PROMPT 4: LOST CUSTOMER & CHURN ROOT-CAUSE INVESTIGATOR
*(Target: Churned Accounts & Cancellation Requests)*

### Super-Prompt Code & Instructions for Jake / Hermes:

```markdown
Role: StreetSmart Forensic Churn Investigator
Context: You are conducting a root-cause autopsy on a lost client or cancellation request (e.g. Costa 1 Cleaning).

Investigation Procedure:
1. Reconstruct RingCentral History (Past 60 Days):
   - Extract every inbound call, duration, transfer leg, hold time, and missed attempt.
   - Extract every outbound return dial and note response time lag.
2. Audit EZLynx Activity & Notes:
   - Read all discussion notes, billing rescissions, COI requests, and cancellation documents.
   - Identify how many different reps touched the account in the final 30 days.
3. Analyze Magellan AI Markers:
   - Extract caller emotional state, frustration markers, and topic flags.
4. Synthesize Forensic Root Cause:
   - Determine whether churn was caused by: (A) Phone avoidance / unanswered voicemails, (B) Multi-rep handoff confusion, (C) Price / Carrier increase, or (D) Out-of-business / sale.

Output Format:
Executive Churn Autopsy Report:
- Client Name & Account # | Lifetime Value | Assigned Producer & CSR
- Forensic Timeline of Events
- Primary Root Cause of Loss
- Responsible Parties & Process Breakdown
- Recommended Agency SOP Preventive Fix
```

---

## Evidence-Backed Daily, Weekly, and Monthly Reporting Contract

The report engine must fail closed: if an export is missing, empty, stale, or
cannot be parsed, the affected metric is **UNVERIFIED**. It must never substitute
sample counts, a default score, or an assumed customer root cause.

### Department tracker register

| Tracker | Department | Cadence | Exception question | Verified source/schema |
|---|---|---|---|---|
| BOR Tracker | Commercial / Trucking | Weekly | Which BORs remain pending or blocked? | Sheet `17mlkNkDlP51TjNPUVIe0f8-hhDJ6-mF6cRQFWyaAbzs`; client, carrier, submitted/effective dates, status, notes, department |
| Pending Payouts | Accounting | Weekly | Which payments are blocked by missing policy number, invoice, or release data? | Accounting Payable Worklist; applicant, policy number, invoice number, due date, amount, received/paid/net due |
| Policy Change Request Tracker | Service | Daily | Which changes exceeded turnaround or lack an owner/next step? | Export required; column aliases are normalized and source row preserved |
| Referrals Report - Last Week | Sales | Weekly | Which referrals need follow-up and which became wins? | Sheet `1dbhkyWw5krL_ujbOBfrh1_s3LlTcWBPutCZw4y9m3Hc`; account, referrer, assigned agent, source/status, producer, estimated value |
| Missed Calls Report 2026 | All departments | Daily | Was each missed call returned within SLA, and by whom? | Reconciled with RingCentral inbound/outbound evidence; tracker export supplements manual disposition |
| 2026 Pending COIs/Endorsements | Service | Daily | Which requests are late, blocked, or missing a next step? | Export required; account, owner, request/due dates, status and notes expected |
| Expiration Report | Retention | Weekly | Which upcoming renewals lack a documented renewal plan? | Renewal tracker `1AnnKl85a8QzFUGabpy_UduhZBdjlCFfcOl35cqcykLs`; account, renewal date, status, remarks, premium, agent |
| Voicemail/Email Tracker | All departments | Weekly | Who has unresolved workload, and is the weekly tracker complete? | Sheet `1wB1ZBJDDkR6WjWLFw19DWTt1eowEb8nTG_aKl-s`; employee/email weekly count matrix |
| Case Studies | All departments | Monthly | Which saved renewal or new-business win has evidence suitable for team sharing? | Evidence bundle must identify the account, result, owner, and supporting source IDs |
| Submission Center Status | Sales | Weekly | Which open submission is older than 30 days or has no actionable note? | EZLynx Submission Center export; submission/account/owner/carrier/created/last activity/status |
| Policy Transaction - Audit & Cancellation | Accounting / Service | Weekly | Which audits/cancellations remain incomplete or need escalation? | Export required; transaction/account/owner/due date/status/notes expected |

Each exception carries the original tracker name and source row. A vague note is
a coaching signal, not proof that work did not occur. Employee discipline or a
churn verdict requires corroborating RingCentral, EZLynx, Magellan, email, and/or
document evidence.

### Gmail accountability boundary

For a backend report, use a Google Workspace service account with administrator-
approved domain-wide delegation and the narrowest Gmail read-only metadata
scope. Impersonate each approved employee only for the reporting job. Report
thread-level workflow signals such as unread age, last external message, last
employee response, and whether the thread appears to await the employee. Do not
publish message bodies in leadership reports by default.

Unread is not the same as unreplied. A reliable email metric must reconstruct the
thread and determine whose message was last. Auto-replies, internal-only threads,
spam, bulk mail, and delegated/shared inbox behavior must be excluded by written
rules. Until Workspace authorization and those rules are approved, Gmail metrics
remain **UNVERIFIED** and the browser-export/manual tracker path is used.

### Reporting cadence

- Daily: direct and queue calls, voicemail/callback reconciliation, overdue tasks,
  policy changes, missed calls, and pending COIs/endorsements.
- Weekly: per-employee rollup, Sales/Retention/Submission Centers, Gmail workflow
  signals, BORs, payouts, referrals, expirations, audits/cancellations, and tracker
  completion.
- Monthly: trends and recurring exceptions plus evidence-backed case studies and
  churn autopsies. An agency grade is shown only when the approved weighting
  formula version and all required evidence are supplied.
