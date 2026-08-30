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
| **Task Velocity & Backlog** | EZLynx `/web/tasks` | Total tasks assigned, tasks completed, overdue tasks (>1 day past due), task aging distribution per employee. | Identifies back-office operational bottlenecks and unworked service items. |
| **Documentation Density** | EZLynx Activity Log & Looker #25979 | Discussion notes logged, policy changes recorded, quotes generated, COIs issued, rescissions filed. | Credits reps who are active in policy servicing even when off the phone. |
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
  * **100% Attributable to That Individual Rep.** 
  * If a voicemail is left and the assigned rep does not dial that number back, it is logged as an **Orphaned Missed Call** and heavily penalizes the rep's Weekly Grade.

### B. Queue Calls (Department / Agency Workload)
* **What It Is:** An incoming call dialed to the agency main number `(732) 462-8343` or a department routing rule (`Ext 9006 - Commercial Queue`, `Ext 105 - Trucking Queue`, `Ext 104 - Personal Lines Queue`).
* **How RingCentral Logs It:**
  * **Ring-All / Sequential Phase:** RingCentral rings all active members logged into that queue. For every member whose phone rings but doesn't answer, it logs an internal routing attempt (`Missed` or `IP Phone Offline`).
  * **Answer Leg:** When a specific member answers, it logs: `Ext: [Rep Extension]`, `Action Result: Accepted` (or `Call connected`), with the exact duration.
  * **Queue Abandonment:** If nobody in the queue answers before timeout, it routes to `Voicemail` on the department extension.
* **Accountability Impact:**
  * Reps who answer queue calls receive **"Queue Rescuer / Assist" credit**, boosting their overall score.
  * Queue voicemails are assigned to the department queue lead for mandatory same-day callback.

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

## 6. Employee Scoring Formula (0–100 Scale)

$$\text{Final Score} = \text{Base (50)} + \text{Inbound Answer Bonus} + \text{Callback Bonus} + \text{Outbound Bonus} + \text{EZLynx Bonus} - \text{Penalties}$$

* **Inbound Answer Rate:** Up to $+20$ points ($>80\% = +20$, $<30\% = -15$).
* **Missed Call Resolution:** $+15$ points for $100\%$ returned; **$-10$ points for EACH unreturned voicemail**.
* **Outbound Dials & Talk Time:** $+10$ points for active phone outreach ($>150$ dials / $>5$ hrs talk time).
* **EZLynx Velocity:** $+10$ points for high activity notes / zero overdue tasks; **$-2$ points per overdue task**.
* **Queue Rescuer Assist:** $+5$ bonus points for answering general queue overflow calls.
