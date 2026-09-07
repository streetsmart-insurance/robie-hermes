# StreetSmart Productivity & Accountability System — Iteration 1 Blueprint
**Executive Specification, Audit Methodology, Data Feeds & Queue Mechanics**
*Status: Saved & Approved as Iteration 1*
*Prepared for: Carlo Ferrara & Jake Ferrara — StreetSmart Insurance*
*Author: ROBIE Engineering*
*Effective Date: August 29, 2026*

---

## 1. System Scope & Core Philosophy

The **StreetSmart Productivity & Accountability System (Iteration 1)** solves three critical agency failure modes:
1. **The Direct Avoidance Trap:** Reps letting inbound direct client calls roll to voicemail and failing to return them within SLA, creating severe client churn and lost renewals.
2. **The Queue Rescuer Burden:** A small subset of conscientious reps being forced to absorb unhandled overflow calls from general queues while avoiders stay idle.
3. **The Disconnected Work Myth:** Resolving the gap between phone talk time and back-office documentation by cross-referencing RingCentral VoIP logs with EZLynx Task Aging, Activity Looker Reports (#25979), and Magellan AI sentiment.

---

## 2. Exactly What We Are Checking & Data Sources

| Dimension | System / Source | Exact Data Elements Checked | Business Impact |
| :--- | :--- | :--- | :--- |
| **VoIP Call Graph** | RingCentral Detailed Call Log | `Date`, `Time`, `From` (Caller ID), `To` (Dialed DID), `Extension` (Rep / Queue), `Direction` (In/Out), `Action Result` (Accepted, Missed, Voicemail, Offline), `Duration` | Identifies who was called, whether they picked up, how long they spoke, and if a return call occurred. |
| **Callback SLA & Graph** | RingCentral Outbound Logs | Normalized 10-digit caller phone matching against all outbound dials within 30 min / 2 hr / 24 hr windows. | Determines if an orphaned call was returned by the assigned rep vs. assisted by a teammate vs. abandoned. |
| **Task Velocity & Backlog** | EZLynx Five / Reports 5.0 | Verified Reports 5.0 task/activity mapping only; total assigned, completed, overdue, aging, postponement, owner, account and source row. Legacy Saved Reports are not a fallback. | Identifies back-office operational bottlenecks and unworked service items. |
| **Documentation & Resolution** | EZLynx Five / Reports 5.0 plus account Activity tab | Manual calls/texts/emails, automated notices, tasks, policy changes, COIs and documented outcomes. Automated notices and verification codes do not prove a human response. | Credits meaningful service work and prevents automation from being mistaken for follow-through. |
| **Conversational Sentiment** | Magellan AI (`app.magellan.insure`) | AI Sentiment Score (54% Satisfied / 34% Neutral / 12% Sad), Intent Tags (`At-Risk`, `Cancellation`, `Claims`, `Billing`, `COI`). | Flags callers in emotional distress or active cancellation risk before churn occurs. |
| **Lead / Prospect SLA** | RingCentral + EZLynx Client Match | Inbound numbers checked against EZLynx Active Client database to isolate `PROSPECT_NON_CLIENT` and `VENDOR_BROKER`. | Measures response speed on new business leads vs. existing policyholders. |

---

## 3. How We Measure Missed Calls: Direct vs. Queue Calls

RingCentral handles calls through two distinct routing mechanisms, and the engine evaluates them differently:

### A. Direct Missed Calls (Individual Accountability)
* **What It Is:** An incoming call dialed directly to a specific rep’s extension (e.g. `Ext 9042 - Jackie Arriola`) or direct telephone number (e.g. `(848) 444-6345`).
* **How RingCentral Logs It:**
  * If the rep is on another call or ignores it: `Action Result: Missed` or `Voicemail`.
  * If the rep’s computer/app is closed: `Action Result: IP Phone Offline`.
* **Accountability Impact:**
  * Report the failed destination, assigned producer and assigned CSR separately.
  * A direct missed line proves where the call failed, not by itself who owned follow-up. If the called employee differs from the assigned CSR, report an ownership gap unless a documented handoff rule assigns responsibility.
  * Classify the final result as timely callback, non-call response, client had to redial, true no response, pending SLA or unverified caller.

### B. Queue Calls (Department / Agency Workload)
* **What It Is:** An incoming call dialed to the agency main number `(732) 462-8343` or a department routing rule (`Ext 9006 - Commercial Queue`, `Ext 105 - Trucking Queue`, `Ext 104 - Personal Lines Queue`).
* **How RingCentral Logs It:**
  * **Ring-All / Sequential Phase:** RingCentral rings all active members logged into that queue. For every member whose phone rings but doesn't answer, it logs an internal routing attempt (`Missed` or `IP Phone Offline`).
  * **Answer Leg:** When a specific member answers, it logs: `Ext: [Rep Extension]`, `Action Result: Accepted` (or `Call connected`), with the exact duration.
  * **Queue Abandonment:** If nobody in the queue answers before timeout, it routes to `Voicemail` on the department extension.
* **Accountability Impact:**
  * Group every routing leg under one parent customer call. A stopped, missed or offline member leg is not a separate customer failure when another human leg answered.
  * Do not label every rung endpoint an offender. Unless an explicit queue-owner or handoff rule exists, use the account's assigned CSR as the default follow-up owner and show the producer for oversight.
  * AI Receptionist or queue acceptance is not human contact; require a connected/accepted human leg.

---

## 4. Trucking Queue Deep-Dive (`Ext 105 & Ext 9005`)

Across the past 7 days, the **Trucking Department** handled the following volume:

* **Total Trucking Call Records:** 867 call routing legs.
* **Calls Successfully Connected:** **185 calls answered** (62 on Trucking Queue `Ext 9020`, 46 on Trucking Leads `Ext 1013`, and direct trucking dials).
* **The Trucking Phone Offline Bottleneck:** **530 routing attempts failed with `IP Phone Offline`** because reps assigned to the trucking ring-group were logged out of the RingCentral desktop app.
* **Trucking Queue Voicemails:** **30 voicemails left in the general Trucking queue**.
* **Key Finding:** Trucking call volume is heavy (185 connected calls), but callers experience long ring times because multiple assigned extensions are frequently offline in RingCentral.

---

## 5. Audit Windows & Timing Cycles

1. **Real-Time Watchdog (Every 30 Minutes during business hours):**
   * Window: Rolling 4-hour lookback.
   * Trigger: Any inbound voicemail or missed call sitting unreturned past **30 minutes** without an EZLynx note.
   * Action: Immediate alert card dispatched to Google Chat management space.
2. **Daily 5:00 PM Scorecard (`Cron Job 79d89f84bb90`):**
   * Window: Today (00:00 to 17:00).
   * Generates the day's callback reconciliation and employee rankings.
3. **Weekly Comprehensive Scorecard (`Cron Job a3c0fe92d10a`):**
   * Window: Rolling 7 days (Monday 00:00 through Friday 17:00).
   * Compares 7-day RingCentral calls + EZLynx Task Aging + Looker #25979 + Magellan AI Sentiment.
4. **Historical Forensic Audit (On-Demand / 30-Day Window):**
   * Deep-dive on specific distressed accounts (e.g. Costa 1 Cleaning, Piotr Gusciora, Jose Garcia) across 30–90 days.

---

## 6. Evidence Status Before Scoring

Iteration 1 reports verified facts and exceptions; it does not automatically impose disciplinary scores. Any future score requires a separately approved formula, minimum sample sizes and written ownership rules.

Every exception must preserve the parent call or source row, business-hours flag, failed destination, producer, CSR, response channel/time, final classification and evidence qualification. Missing identity, incomplete call coverage or an unverified Reports 5.0 mapping is shown as `UNVERIFIED`, never converted into a favorable or adverse employee result.
