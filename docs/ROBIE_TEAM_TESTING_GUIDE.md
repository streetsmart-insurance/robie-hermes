# StreetSmart Insurance — Robie Voice AI Playbook
## Team Testing Guide: Labels, Free-Form Callouts & Live Transfer

**Target Audience:** Account Managers, CSRs, Producers (Carlo, Jake, Nicole, Sandy, Jackie, Eimy, Jimmy)  
**System Name:** ROBIE (Autonomous Voice AI Engine)  
**Caller ID:** `+1 (732) 298-6745` (Monmouth County, NJ)  
**Main Agency Callback:** `(732) 462-8343`  
**Date:** September 2026  
**Status:** **Live & Active for On-Demand Outbound Calls**

---

## 1. What Robie Does Today (The Core Engine)

Robie's job is simple, lean, and reliable:
1. **Dials the target** (carrier or client/insured).
2. **If Voicemail:** Leaves a concise, professional voicemail with our agency callback number.
3. **If Client Answers:** Delivers the message/reminder, and if they have questions or want to speak to someone, **warm-transfers the live call directly to the Account Manager / Assigned Producer's phone!**
4. **Logs Everything:** Automatically posts an audit note to the EZLynx Discussion card with the call summary, full transcript, audio recording link, and signature (`Robie was here`).

---

## 2. THE MAIN FEATURE: Free-Form "Robie Call"

You do not need to follow a rigid script. You can tell Robie to call anyone and give free-form instructions in plain English directly from EZLynx.

### How to Trigger:
1. Open the client in EZLynx (`https://app.ezlynx.com/web/account/{applicant_id}/activity`).
2. Open a Discussion card or click **Add Note**.
3. In the Note or Card Title, write **`Robie Call`** followed by your instructions.

### Free-Form Examples:

#### Example 1: Calling a Carrier Underwriter
```text
Robie Call
Carrier: The Hartford
Policy: 13 MS BL8665
What to say: Check with commercial underwriting if the upcoming renewal quote packet has been issued. If released, ask for the quoted premium and request they email it to robie@streetsmart.insurance.
```

#### Example 2: Calling a Client with Live Transfer
```text
Robie Call
Contact: Insured
What to say: Remind them that their renewal review is ready. Ask if they have 5 minutes right now to review with their Account Manager. If they say yes, transfer them to me.
```

#### Example 3: Explicit Custom Phone Number
```text
Robie Call
Phone: (800) 556-5376
Carrier: Utica First
Policy: HOP622388401
What to say: Inquire on the status of our endorsement request. If the underwriter is available, connect them to my extension.
```

---

## 3. The 4 Robie Labels & What Each One Does

All labels trigger an outbound phone call. **None of them alter files or perform complex backend operations**—they simply dial, leave a voicemail if unanswered, or talk to the client and route them to the Account Manager.

| Label | Who Robie Calls | If Voicemail | If Client Answers |
| :--- | :--- | :--- | :--- |
| **`Robie Call`** | Any contact or carrier you specify in your note | Leaves custom VM based on your note | Delivers your message; routes/transfers to you if requested. |
| **`Robie Audit`** | Insured / Client (using cell on file) | Leaves voicemail reminding them to complete their payroll audit | Reminds them their audit is due; offers to **route live call to Account Manager** for assistance. |
| **`Robie Cancellation`** | Insured / Client (using cell on file) | Leaves urgent payment reminder VM to prevent policy lapse | Alerts them to pending cancellation; offers to **route live call to Account Manager** to process payment. |
| **`Robie Quote Follow-up`** | Prospect / Quoted Client | Leaves polite check-in VM regarding their recent quote | Asks if they received the quote; offers to **route live call to Producer** to answer questions & bind. |

### Note on `Robie Audit`:
* `Robie Audit` **only** calls the insured to remind them that their payroll audit is due.
* It does **not** check audit portals, download audit statements, or touch carrier accounting systems.
* If the insured answers, Robie delivers the reminder and immediately offers to connect them directly to their Account Manager.

---

## 4. Current Status: Quote Follow-Up & Stop Controls (Are They Setup?)

### Is the multi-day cadence and stop setup right now?
**NO.** There is currently **no active cadence or automatic stop mechanism** running in production.

* **What works TODAY:**
  * When you write `Robie Quote Follow-up`, Robie places **one single outbound follow-up call** to the prospect.
  * If voicemail, it leaves a VM. If the prospect answers, it speaks to them and warm-transfers to the Producer.
* **What is NOT setup yet (Future Idea):**
  * The automated multi-day cadence (calling on Day 1, Day 3, Day 7).
  * The automatic `Robie Stop` keyword listener or automatic stop when moving an opportunity to `Won` / `ActiveClient`.
  * *Because there is no recurring cadence running, Robie will never make repeat calls unless you manually trigger another note.*

---

## 5. How to Test & Confirm It Went Right

When you test any of the labels (`Robie Call`, `Robie Audit`, `Robie Cancellation`, `Robie Quote Follow-up`), verify these items in EZLynx:

1. **Check the Discussion Card in EZLynx:**
   * Robie will post a note to the card within 1–2 minutes after the call finishes.
   * **Header:** `Policy: #{policy_number} ({lob} - {carrier})`
   * **Call Outcome:** Shows whether voicemail was left, call was answered, or client was transferred.
   * **Audio Link:** Clickable URL to listen to the exact call recording.
   * **Signature:** Always ends with `Robie was here`.
2. **Listen to the Recording:**
   * Click the audio link to verify how Robie introduced itself, delivered your message, and handled the handoff.
3. **Warm Transfer Check:**
   * If you answered and requested to speak with the agent, verify that Robie told you to hold and successfully connected the call to the Account Manager's phone.

---

## 6. What Robie CANNOT Do (Clear Guardrails)

* **No Binding:** Robie cannot bind coverage, accept policy cancellations, or alter coverages.
* **No Financial Info:** Robie will never collect credit card or bank routing numbers over the phone.
* **No Carrier System Navigation:** Labels like `Robie Audit` do not audit payroll or review carrier portals—they only place phone calls.
* **No Guessing Phone Numbers:** If a carrier or client phone number is missing, Robie will not guess; it will post a note asking the CSR for the number.

---

## 7. Quick Reference Cheat Sheet for the Team

```text
================================================================================
ROBIE CHEAT SHEET (WRITE IN EZLYNX DISCUSSION CARD)
================================================================================

1. Free-Form Call (Any Target):
   Robie Call
   Carrier: [Carrier Name]  (or Contact: Insured)
   Policy: [Policy Number]
   What to say: [Plain English instructions for Robie]

2. Audit Reminder Call:
   Robie Audit
   -> Robie calls client, reminds them to do payroll audit, transfers to AM if answered.

3. Cancellation / Payment Reminder Call:
   Robie Cancellation
   -> Robie calls client, reminds them of pending cancellation, transfers to AM to pay.

4. Single Quote Follow-Up Call:
   Robie Quote Follow-up
   -> Robie calls prospect, asks if they reviewed the quote, transfers to Producer.
================================================================================
```
