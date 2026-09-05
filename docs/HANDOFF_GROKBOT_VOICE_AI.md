# StreetSmart Insurance — Autonomous Carrier Voice AI Engine
## Grokbot Operational Handoff & Administration Guide

**Document Version:** 1.0.0  
**Effective Date:** September 05, 2026  
**System Maintainer:** Grokbot / Antigravity Operations  
**Agency:** StreetSmart Insurance  
**Primary Stakeholders:** Carlo Ferrara (`carlo@streetsmart.insurance`), Jake Ferrara (`jake@streetsmart.insurance`)

---

## 1. Executive Summary & Telephony Status

The Autonomous Carrier Voice AI Engine empowers Robie to place outbound phone calls to insurance carrier underwriting, audit, and billing desks, navigate complex automated Interactive Voice Response (IVR) phone trees, converse naturally with human underwriters, and record structured outcomes directly into EZLynx.

### Live Telephony & Account Configuration
* **Voice Engine Platform:** Bland AI (Enhanced Enterprise Audio Pipeline).
* **Organization API Key:** `org_020b04143009fc2cd8e95b2d711a20c890cd5513c05e11cf28eeb06700205d9187a970eb00a886d3301a69`
* **Account Balance:** `$11.787` (Active & funded; per-minute cost is ~\$0.09–\$0.14/min; 1m30s live call was \$0.21).
* **Dedicated Outbound Caller ID:** **`+1 (732) 298-6745`**
  * Purchased directly on Bland AI.
  * Local New Jersey `(732)` area code matches agency geographic footprint.
* **Inbound Call Forwarding (Auto-Transfer):**
  * Number `+1 (732) 298-6745` is configured with an automated greeting:
    > *"Thank you for calling StreetSmart Insurance. Let me transfer you directly to our office team."*
  * Inbound calls automatically transfer immediately to StreetSmart's RingCentral office PBX: **`+1 (732) 462-8343`**.
* **Carrier Whitelist Support Request (Solution #2):**
  * Official email sent from `robie@streetsmart.insurance` to `support@bland.ai` (CC: `carlo@`, `jake@`) on September 5, 2026 (`Msg ID: 1a0719ccf33b0178`).
  * Attached official RingCentral Statement Document #17050835001 verifying ownership of `+1 (732) 462-8343`.
  * Once Bland support approves the whitelist, `VOICE_CALLER_ID` can optionally be set to `+17324628343` directly.

---

## 2. How Carlo & Jake Invoke Voice Calls

There are two primary ways to trigger Robie to make a phone call:

### Method A: Via Email to Robie (Zero-CLI for Carlo & Jake)
Carlo or Jake can trigger a call from their phone or laptop simply by emailing `robie@streetsmart.insurance`:

* **To:** `robie@streetsmart.insurance`
* **Subject:** `Call Carrier: UB-6N448514-25-42-V` *(or simply paste the policy number or applicant name)*
* **Body (Optional Instructions & Overrides):**
  ```text
  Please call underwriting for Associated Specialty.
  Phone: (800) 555-0199
  Ask for Mike in Commercial Renewals. Find out if the renewal quote has been released and if payroll verification was approved.
  ```
* **Execution:**
  1. Robie polls the inbox on cadence.
  2. Robie matches the policy number against EZLynx via REST API.
  3. Robie resolves the carrier, line of business, and applicant details.
  4. Robie initiates the outbound phone call.
  5. Once finished, Robie posts the audit note to EZLynx and replies to Carlo/Jake with the call summary and audio link.

---

### Method B: Via EZLynx 'robie call' Note / Label (In-App CSR Trigger)
CSRs and Account Managers can trigger Robie without leaving EZLynx:
1. Open the **Applicant** in EZLynx.
2. In the Discussion card or Add Note, include `robie call`:
   ```text
   robie call
   Carrier: The Hartford
   Policy: PWC1239278
   What to say: Check if renewal terms are released and ask for quoted premium.
   ```
   *(Note: Phone number is optional! If omitted, Robie auto-resolves the carrier phone from the agency directory and policy file).*
3. **Execution & Intelligence:**
   - **Auto-Phone Resolution:** If no phone is provided, Robie pulls the carrier from the policy or directory (`Hartford`, `Travelers`, `Coterie`, `Progressive`, `AmTrust`, `Chubb`, etc.).
   - **Lean Direct API Clarification:** If the carrier phone cannot be found, Robie does NOT touch heavy Playwright tasks—it posts a clean clarification note directly into the card via REST API requesting the number:
     > `⚠️ [ROBIE CALL - PHONE NUMBER NEEDED]`
   - **Instant Dialing:** Robie dials via Bland AI from `+1 (732) 298-6745` and posts audio + transcript back to the card.
4. **Trigger / Scan Command:**
   ```bash
   PYTHONPATH=. .venv/bin/python3 -m src.voice.ezlynx_label_dispatcher --applicant-id <ApplicantID>
   ```

#### Client follow-up vs carrier call + producer warm transfer
Tag the note explicitly so Robie does not guess:

```text
robie call
Call type: client
Who to call: the insured
What to say: Review the quote Jake put together.
```

```text
robie call
Call type: carrier
Who to call: The Hartford
What to say: Confirm renewal terms, then connect them to the producer if they ask.
```

- **Client path (`call_type=client_followup`):** Robie greets the insured by EZLynx `FirstName` and mentions the **Sales Center opportunity `producerName`** ("quote {producerName} put together"). Carlo decision 2026-09-05 (verified on Buster Brown / applicant `26356199`): greeting is Sales Center `producerName`, not account Assigned Producer and not commission policy Producer. Busy / no / voicemail: do **not** warm-transfer; leave a short polite close asking them to call the agency main **732-462-8343** (RingCentral office PBX), not a producer cell/DID (Carlo voicemail-callback decision).
- **These are three different EZLynx fields:** Assigned Producer (`GetApplicantSidebar` → `Applicant.Assignment.AssignedTo` full name; Classic Applicant/v2 `AssignedTo` is only a username like `Carlo1`) ≠ Sales Producer (`GetOpportunitiesForApplicant` → `opportunities[].producerName`) ≠ commission Producer (`CommissionProducers[].Producer.ProducerName`, e.g. Brittni). CSR is `CsrUserModel.FullName` and is never the greeting source.
- **Fallback:** only if Sales Center has no `producerName`, optionally use portal `Assignment.AssignedTo` full name. Never invent a name. Never use commission Producer. Never treat Classic `AssignedTo` username as the greeting without resolution.
- **Warm transfer (unchanged):** Bland warm-transfers to the **requestor** — the Robie Call **label invoker** (note author / email sender), resolved from the voice directory DID. Greeting producer and transfer target are independent.
- **Carrier path (default):** Existing underwriting follow-up. After a live human is confirmed, Robie can warm-transfer to the same requestor.
- **Worked example:** Mike Sosa applies the Robie Call label on an account whose Sales Center `producerName` is Carlo Ferrara. Greeting says Carlo; transfer DID is Mike's RingCentral Direct Number `+17326540947`.
- Requestor is read from Portal `GetPagedDiscussions` note metadata (`discussionNote.createdByName` / `createdBy` / `userName` / email, then card `lastModifiedByName`). Lookup is by name, alias, or email in `producers[]`. Email or name alone is not enough — the row must have an E.164 `phone` or transfer is skipped. **Never fall back to the Sales Center producer or another staff DID.**
- Assigned CSR for EZLynx tasks remains Carlo Ferrara (never Robie).

---

### Method C: Via Command Line (Direct Terminal Execution)
To test or dispatch calls instantly from the terminal or scripts:

#### 1. Quick Dry-Run Simulation (No Real Phone Call Placed, $0 Cost):
```bash
PYTHONPATH=. .venv/bin/python3 -m src.voice.dispatcher \
  --policy-number "UB-6N448514-25-42-V" \
  --dry-run
```

#### 2. Live Outbound Call to a Policy Carrier:
```bash
PYTHONPATH=. .venv/bin/python3 -m src.voice.dispatcher \
  --policy-number "UB-6N448514-25-42-V"
```

#### 3. Live Outbound Test Call to a Specific Phone Number (e.g., Carlo or Jake's Cell):
```bash
PYTHONPATH=. .venv/bin/python3 -m src.voice.dispatcher \
  --policy-number "UB-6N448514-25-42-V" \
  --phone "+17329953409" \
  --instructions "Testing voice agent response. Ask for Buster Brown."
```

---

## 3. Hardcoded EZLynx Direct API Pushback Architecture

All call completions route through `src/voice/webhook_server.py::process_completed_call` and push directly into EZLynx via the Universal API.

### Automatic Account Resolution Flow:
1. **Metadata Lookup:** If `applicant_id` is passed in call metadata, use it directly.
2. **Database Lookup:** If `applicant_id` is missing, query local SQLite `PolicyRenewal` table by policy number.
3. **EZLynx REST Search API Fallback:** If still missing, query `services.ezlynx.com/ezlynxapi/api/applicant/v1/search` with `policy_number.strip()` to resolve the exact EZLynx `applicant_id` and `applicant_name`.

### Standardized Discussion Threading:
* Posts directly to the applicant's existing CSR renewal discussion card via `client.add_note_to_discussion(...)`.
* **Universal Mandate Compliance:**
  * **Header:** `Policy: #{policy_number} ({line_of_business} - {carrier_name})`
  * **Body:** Call status, call duration, summary of findings, and secure Bland recording URL.
  * **Signature:** Mandatory ending `Robie was here`.
  * **Auto-Association:** Policy Number, Line of Business, and Carrier are attached in the API payload so the note appears under the specific policy record.

---

## 4. Multi-Use Case Expansion Matrix

Robie's voice prompt generator in `src/voice/voice_client.py` and dispatcher support multiple specialized insurance operational use cases beyond basic renewal follow-ups:

| Use Case | Target Desk | Key Objective | Spoken Questions |
| :--- | :--- | :--- | :--- |
| **1. Renewal Terms Follow-Up** *(Active)* | Underwriting / Renewals | Obtain renewal quote status and quoted premium. | "Have renewal terms been released? What is the renewal premium? Was it sent to our portal or email?" |
| **2. Loss Runs Expediter** | Claims / Loss Runs Dept | Request 3–5 year currently valued loss runs. | "We need currently valued hard-copy loss runs for the past 5 policy terms sent to robie@streetsmart.insurance." |
| **3. Policy Endorsement Audit** | Commercial Servicing | Check if change request was issued. | "Following up on change request submitted on [Date] to add vehicle/driver. Has the endorsement been generated?" |
| **4. Premium Audit Follow-Up** | Premium Audit Dept | Inquire on payroll audit status or dispute. | "Checking on the final payroll audit for the expiring term. Is the audit closed or are additional 941s needed?" |
| **5. Cancellation / Reinstatement** | Billing / Accounting | Confirm payment receipt and prevent lapse. | "Checking policy status following client payment. Has the notice of cancellation been rescinded and policy reinstated?" |

To invoke any of these specific use cases, simply include the objective in the `--instructions` CLI flag or email body:
```bash
PYTHONPATH=. .venv/bin/python3 -m src.voice.dispatcher \
  --policy-number "UB-6N448514-25-42-V" \
  --instructions "USE CASE: Loss Runs. Inquire if 3-year loss runs have been generated and email them to robie@streetsmart.insurance."
```

---

## 5. File Inventory & Repository Map

* `src/voice/voice_client.py`: Bland AI API caller, prompt engineering, and guardrails.
* `src/voice/context_hydrator.py`: Resolves policy metadata, carrier phone, and CSR email from EZLynx.
* `src/voice/webhook_server.py`: Receives call completion payloads and writes notes to EZLynx API.
* `src/voice/email_dispatcher.py`: Parses incoming email triggers from Carlo and Jake.
* `src/voice/dispatcher.py`: CLI interface for testing and automated cron runs.
* `src/config.py`: Configuration settings (`voice_caller_id = "+17322986745"`).
* `.env`: Local environment variables.
* `tests/test_voice_*.py`: Automated pytest suite (100% passing).
