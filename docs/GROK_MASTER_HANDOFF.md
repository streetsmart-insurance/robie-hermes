# StreetSmart Insurance - Grok Master Handoff
## Autonomous Voice AI Engine & EZLynx 'Robie Call' Integration

**Repository:** `streetsmart-insurance/renewal-automation-system`  
**Target Operator:** Grok / Grokbot / Engineering & Operations  
**Date:** September 2026  
**Status:** **Production Ready (107/107 Tests Passing)**

---

## 1. Executive Summary & Capabilities

The **StreetSmart Autonomous Voice AI Engine** ("Robie Voice") is an automated, carrier-facing outbound and inbound telephonic agent designed to eliminate manual phone outreach for insurance agency staff.

### Core Architecture
* **Primary AI Engine:** Bland AI (`enhanced` model, `nat` conversational insurance voice persona) with IVR navigation, background hold listening, machine detection, and voicemail drop capabilities.
* **Secondary Fallback:** Retell AI adapter.
* **Caller ID & Phone Numbers:**
  * **Dedicated Robie Line:** `+1 (732) 298-6745` (Monmouth County, NJ local caller ID).
  * **Agency Main Line:** `+1 (732) 462-8343`.
* **Universal EZLynx Integration:** Posts standardized audit notes directly into customer Discussion cards via the Classic REST API (`services.ezlynx.com`) within <150ms of call completion.
* **Zero Browser Overhead for CSR Feedback:** Avoids fragile browser tasks; delivers questions or missing phone alerts back to the agent directly via EZLynx discussion cards.

---

## 2. Trigger Methods (How to Invoke Robie)

### Method A: In-App EZLynx `Robie Call` / `Robie lead follow-up` / `Robie client outreach` Label / Note (CSR Native Flow)
Account Managers and CSRs trigger Robie directly inside EZLynx without leaving the applicant profile.

#### Can the API pull the label?
* **Official EZLynx Swagger 2.0 Discovery:** In this session, we pulled the full official EZLynx Swagger specification directly from `https://services.ezlynx.com/ezlynxapi/swagger/docs/v1` (saved in `data/ezlynx_swagger.json`).
* **Classic REST API (`services.ezlynx.com`):** The Classic REST API has **no public query endpoint for labels** (only `POST /api/note/v1`, `GET /api/Applicant/v2/{id}`, and `ZapierNoteViewModel.Labels`).
* **Portal / Web UI (`app.ezlynx.com`):** The Activity / Discussion web UI (`/web/account/{applicant_id}/activity`) uses `/EZLynxPortalAPI/Discussions/GetPagedDiscussions`.
* **Lean Strategy (Zero-Playwright Priority):**
  To avoid slow, fragile browser automation as Carlo mandated, Robie monitors **`noteLabels[].labelName`**, the **Discussion Card Title**, and the **Note Body** for `Robie Call`, `Robie lead follow-up`, or `Robie client outreach` (close variants accepted; `robie cancellation` aliases to client outreach). Any of those phrases/labels is immediately detected and executed.

#### Triggering in EZLynx:
1. **Carrier call (`Robie Call`, default):** Add a note containing `Robie Call` (or apply that org label):
   ```text
   Robie Call
   Carrier: Utica First (or leave blank to auto-detect from policy)
   Policy: HOP622388401
   What to say: Check if the renewal quote has been released and request the quote packet be emailed to robie@streetsmart.insurance.
   ```
   `Robie Call` stays **carrier** unless the note also says `Call type: client` or who-to-call is the insured.
2. **Client / lead follow-up (`Robie lead follow-up`):** Apply the org label **`Robie lead follow-up`** (or write that phrase in the title/note). Close variants match case-insensitively: `Robie Lead Follow-up`, `robie lead follow up`, `Robie lead followup`. This **forces `call_type=client_followup`** — do **not** write `Call type: client`. Greeting uses **first name only** (commercial accounts: CommercialDetail / PrimaryContact / Contacts[] — never “Hi Green” from an LLC); Sales Center `producerName` is quote attribution only. Warm transfer is the label invoker; voicemail asks the insured to call **732-462-8343**. If both `Robie Call` and `Robie lead follow-up` appear, lead follow-up wins (client path).
   ```text
   Robie lead follow-up
   Who to call: the insured
   What to say: Review the quote Carlo put together.
   ```
3. **Client outreach / cancellations (`Robie client outreach`):** Apply the org label **`Robie client outreach`** (or write that phrase / `robie cancellation` in the title/note). Forces **`call_type=client_outreach`** — a different prompt from lead follow-up (no “quote {Sales Center producer} put together”). Dial order is locked: **primary applicant first, then secondary/co-applicant** when both have E.164 phones (`CellPhone` → `HomePhone` → `WorkPhone`). Skip anyone with no phone; never invent numbers. Same number on both people → one call. Each call greets that person by **first name only**. On a clear yes, warm-transfer to the **account Assigned Producer** (`GetApplicantSidebar` → `Applicant.Assignment.AssignedTo`, `lookup_producer` DID) — **not** Sales Center `producerName`, **not** the label invoker. Missing Assigned Producer DID skips transfer; voicemail still asks them to call **732-462-8343**. Splice scripts come **only** from Carlo's Google Doc Manual WFs (conversational, no press-1/2/4/6): `audit`, `recommendations`, `returned_mail`, `esign`, `additional_info`, `unresponsive`, `renewal_reachout` (CSR label/note only — never the daily renewal pipeline), plus `cancellation` / `robie cancellation` from the Cancellation Notice PDF. Not ported: Birthday, Additional Policy, Applicant Created, Welcome, Reinstatement, Policy Renewed, Upcoming Renewal/Expiration, Winback, Sales Center New/Contacted/Quoted/Won. Sales Center Reviewed Status is already **Robie lead follow-up**. Buster Brown (`26356199`): primary `7329953409`, co-applicant has no cell — secondary is skipped.
   ```text
   Robie client outreach
   What to say: Policy is pending cancellation — please call to keep coverage or confirm they want to cancel.
   ```
4. **Auto-Resolution of Phone Numbers (Multi-Tier):**
   * **Tier 1 (Explicit Note):** If the CSR includes a phone number (e.g. `Phone: 800-556-5376`), Robie dials it directly.
   * **Tier 2 (Carrier Directory):** If omitted, Robie resolves the carrier from the applicant's policies and looks up the number in `KNOWN_CARRIER_PHONES` (e.g., Utica First `800-556-5376`, TIP National `800-688-8408`, Travelers `800-238-6225`, Hartford `800-555-1234`).
   * **Tier 3 (Applicant / Insured Phone):** If the target is the client/insured or Carlo, Robie pulls the `CellPhone` directly from the applicant profile (`+1 (732) 995-3409`).
   * **Tier 4 (Clarification Fallback - 100% Lean Direct API):** If phone is unknown, Robie posts a clarification note directly into the discussion card without launching any browser sessions:
     ```text
     Policy: #{policy_number} ({lob} - {carrier})
     ⚠️ [ROBIE CALL - PHONE NUMBER NEEDED]
     Robie could not resolve a phone number for '{carrier}'.
     Please reply with 'Robie Call Phone: (xxx) xxx-xxxx'.

     Robie was here
     ```
5. **Execution Commands:**
   * **Process Applicant Notes (Live or Dry-Run):**
     ```bash
     PYTHONPATH=. .venv/bin/python3 -m src.voice.ezlynx_label_dispatcher --applicant-id 26356199 --dry-run
     ```
   * **Direct Test Note with Buster Brown (`26356199`):**
     ```bash
     PYTHONPATH=. .venv/bin/python3 -m src.voice.ezlynx_label_dispatcher \
       --test-note "Robie Call: Carrier: Utica First. Policy: HOP622388401. Tell Carlo the test call succeeded." \
       --applicant-id 26356199 \
       --dry-run
     ```
   * **Live Test Call to Carlo (`+1 732-995-3409`) on Buster Brown Account:**
     ```bash
     PYTHONPATH=. .venv/bin/python3 -m src.voice.ezlynx_label_dispatcher \
       --test-note "Robie Call: Contact: Carlo. What to say: Carlo, Robie here testing the Buster Brown dispatcher." \
       --applicant-id 26356199
     ```

---

### Method B: Email Command Trigger (`robie@streetsmart.insurance`)
Authorized agency staff (`carlo@streetsmart.insurance`, `jake@streetsmart.insurance`) can email Robie:

* **To:** `robie@streetsmart.insurance`
* **Subject:** `Robie Call UB-6N448514-25-42-V`
* **Body:**
  ```text
  Carrier: Travelers
  Phone: +1-800-252-2268
  Instructions: Ask underwriting if the commercial auto renewal terms are available and ask for the quote packet to be emailed to robie@streetsmart.insurance.
  ```

---

### Method C: Direct CLI / Terminal Execution (Grokbot Control)

#### 1. Dry-Run Simulation (Safe Verification):
```bash
PYTHONPATH=. .venv/bin/python3 -m src.voice.ezlynx_label_dispatcher \
  --test-note "Robie Call
Carrier: The Hartford
Policy: PWC1239278
What to say: Inquire on renewal terms." \
  --dry-run
```

#### 2. Live Outbound Call by Policy Number:
```bash
PYTHONPATH=. .venv/bin/python3 -m src.voice.dispatcher \
  --policy-number "UB-6N448514-25-42-V"
```

#### 3. Live Outbound Call to Specific Target (e.g. Underwriter or Cell):
```bash
PYTHONPATH=. .venv/bin/python3 -m src.voice.dispatcher \
  --policy-number "TEST-ROBIE-CALL" \
  --phone "+17329953409" \
  --instructions "Testing live Robie Voice dispatcher. Confirm terms received."
```

---

## 3. EZLynx API Direct Pushback Architecture

All completed calls trigger `src/voice/webhook_server.py::handle_completed_call` which automatically formats and posts the note directly into the client's discussion card.

### Mandate Rules (Strictly Enforced):
1. **Policy Reference Header (Top of Note):**
   `Policy: #{policy_number} ({line_of_business} - {carrier_name})`
2. **Mandatory Signature (End of Note):**
   `Robie was here`
3. **Payload Association:**
   `policy_number`, `line_of_business`, and `carrier_name` are passed directly into `client.add_note_to_discussion(...)` so EZLynx binds the note to the specific policy record.

### Example Note Posted to EZLynx:
```text
Policy: #TEST-ROBIE-CALL (Commercial Lines - StreetSmart Executive Office)
Autonomous Carrier Phone Outreach Completed:
- Result: Call finished successfully
- Summary: Robie contacted Carlo Ferrara regarding the upcoming renewal. Carlo confirmed renewal terms are available and agreed to email the quote packet and quoted premium to robie@streetsmart.insurance.
- Audio Recording: https://api.bland.ai/v1/recordings/dbcabba5-308f-49cc-9b0f-1414e58d9652

Robie was here
```

---

## 4. Operational Multi-Use Case Matrix

| Use Case | Target Desk | Key Dialogue & Goal |
| :--- | :--- | :--- |
| **1. Renewal Follow-Up** | Commercial Underwriting | "Have renewal terms been released? What is the quoted premium? Was the packet emailed or in portal?" |
| **2. Loss Runs Request** | Claims / Loss Runs Dept | "Please send currently valued 3-to-5 year loss runs for [Insured] to robie@streetsmart.insurance." |
| **3. Endorsement Audit** | Commercial Servicing | "Following up on policy change submitted on [Date] to add vehicle/driver. Has the endorsement issued?" |
| **4. Final Premium Audit** | Premium Audit Dept | "Checking status of expiring term final payroll audit. Is the audit closed or are additional 941s needed?" |
| **5. Notice of Cancellation** | Billing / Collections | "Confirming payment receipt to rescind Notice of Cancellation and ensure policy remains active." |

---

## 5. Live Verification Proof (Carlo Test Call)

* **Call ID:** `dbcabba5-308f-49cc-9b0f-1414e58d9652`
* **Destination:** `+1 (732) 995-3409` (Carlo Ferrara)
* **Caller ID:** `+1 (732) 298-6745` (Robie NJ Direct Line)
* **Status:** `completed` (Duration: 52 seconds)
* **Audio Recording:** [Bland AI Recording](https://api.bland.ai/v1/recordings/dbcabba5-308f-49cc-9b0f-1414e58d9652)
* **EZLynx Sync:** Audit note `1123314713` posted directly to EZLynx applicant `220250093`.
* **CSR Notification:** Email confirmation dispatched to `carlo@streetsmart.insurance`.

---

## 6. Deployment & System Maintenance

### File Locations:
* **Label Dispatcher:** `src/voice/ezlynx_label_dispatcher.py`
* **Voice Client Adapter:** `src/voice/voice_client.py`
* **Context Hydrator:** `src/voice/voice_context_hydrator.py`
* **Post-Call Webhook Server:** `src/voice/webhook_server.py`
* **Email Command Dispatcher:** `src/voice/email_dispatcher.py`
* **EZLynx Direct REST Client:** `src/ezlynx/api_client.py`
* **Carrier Phone Directory:** `data/carrier_directory.json`

### Production Daemon:
To start the post-call webhook listener on Hermes:
```bash
PYTHONPATH=. .venv/bin/python3 -m src.voice.webhook_server --port 8088
```

### Manual renewal carrier voice cadence (Carlo 2026-09-05)
Insertion is `src/scheduler/daily_runner.py` after Step 4 (`process_due_followups`) — not a parallel stack. Steps 2–4 stay non-autodial. After the existing 5–7d follow-up budget (**2 quiet checks**) with no renewal in hand, place **exactly one** carrier Robie Call via existing `CarrierVoiceClient`. Stops when a renewal PDF / UW reply / in-hand status is present. Never auto-dials the client. No default client-call Step 5c. Step 5b stays CSR “Email Robie to Call”. Step 6 (20–25d, “contact underwriter directly”) stays non-autodial. Never invents a carrier phone. Sacred cron `0 9 * * *` on hermes-poc-01 is unchanged.

### Running Test Suite:
```bash
PYTHONPATH=. .venv/bin/pytest tests/ -v
```
