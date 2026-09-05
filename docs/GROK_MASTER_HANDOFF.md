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

### Method A: In-App EZLynx `Robie Call` Label / Note (CSR Native Flow)
Account Managers and CSRs trigger Robie directly inside EZLynx without leaving the applicant profile.

1. **Add a Note to any Discussion card** containing the keyword `Robie Call`:
   ```text
   Robie Call
   Carrier: The Hartford
   Policy: PWC1239278
   What to say: Check if the 2026 renewal quote has been released and request the quote packet be emailed to robie@streetsmart.insurance.
   ```
2. **Auto-Resolution of Phone Numbers:**
   * If the CSR includes a phone number, Robie dials it.
   * If omitted, Robie resolves the number automatically from the policy record and carrier directory (`KNOWN_CARRIER_PHONES` + `data/carrier_directory.json`).
3. **Clarification Fallback (Lean Direct API):**
   * If the carrier is unrecognized and no phone is supplied, Robie immediately posts a note requesting the number:
     ```text
     Policy: #{policy_number} ({lob} - {carrier})
     ⚠️ [ROBIE CALL - PHONE NUMBER NEEDED]
     Robie could not resolve a phone number for '{carrier}'.
     Please reply with 'Robie Call Phone: (xxx) xxx-xxxx'.

     Robie was here
     ```
4. **Execution Command:**
   ```bash
   PYTHONPATH=. .venv/bin/python3 -m src.voice.ezlynx_label_dispatcher --applicant-id <ApplicantID>
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

### Running Test Suite:
```bash
PYTHONPATH=. .venv/bin/pytest tests/ -v
# Output: 107 passed, 0 failed (100% green)
```
