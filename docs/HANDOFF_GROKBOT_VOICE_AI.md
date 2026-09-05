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

#### Three voice `call_type`s (do not mix the prompts)
There are three dispatch `call_type`s. Pathway WF labels all use `client_outreach`:

| Label | `call_type` | Who is dialed | Greeting |
| :--- | :--- | :--- | :--- |
| **`Robie Call`** | `carrier` (default) | Carrier desk from the note / directory | Underwriting IVR script |
| **`Robie lead follow-up`** | `client_followup` | Insured Cell → Home → Business | "quote {Sales Center producerName} put together" |
| **`Robie client outreach`** (and pathway WF labels below) | `client_outreach` | Primary applicant, then co-applicant | First name + CSR reason only — **no** producer quote |

#### EZLynx Admin org labels (Carlo creates these names)
Create these **exact names** in EZLynx org Admin so CSRs can click them like Splice WFs. This repo matches `noteLabels[].labelName` only — **do not invent label IDs**. Close variants also match (spaces / hyphens / underscores, optional `robie ` prefix, brackets).

| Admin label name | `call_type` | Pathway |
| :--- | :--- | :--- |
| **`Robie client outreach`** | `client_outreach` | `generic` (CSR What to say) unless the note body matches a pathway |
| **`Robie cancellation`** | `client_outreach` | `cancellation` |
| **`Robie audit`** | `client_outreach` | `audit` |
| **`Robie returned mail`** | `client_outreach` | `returned_mail` |
| **`Robie e-sign`** | `client_outreach` | `esign` |
| **`Robie esign`** | `client_outreach` | `esign` |
| **`Robie additional info`** | `client_outreach` | `additional_info` |
| **`Robie recommendations`** | `client_outreach` | `recommendations` |
| **`Robie unresponsive`** | `client_outreach` | `unresponsive` |
| **`Robie renewal reach-out`** | `client_outreach` | `renewal_reachout` |
| **`Robie renewal reachout`** | `client_outreach` | `renewal_reachout` |

Do **not** create Birthday, winback, Sales Center, new customer, Additional Policy, Applicant Created, Welcome, Reinstatement, Policy Renewed, or Upcoming Renewal/Expiration as Robie dispatch labels.

**Winner when more than one dispatch label is on the same note** (existing rules — do not invert):
1. Any client-outreach trigger (including a pathway WF label) → `client_outreach`
2. Else `Robie lead follow-up` → `client_followup` (requestor transfer unchanged)
3. Else `Robie Call` → `carrier` (unless `Call type: client` / who-to-call insured)

#### Client follow-up vs carrier call + producer warm transfer
**Preferred CSR path for a client/lead call:** apply the org label **`Robie lead follow-up`** (case-insensitive; `Robie Lead Follow-up`, `robie lead follow up`, and `Robie lead followup` also match). That label alone dispatches the same way `Robie Call` does and **forces `call_type=client_followup`**. You do **not** need `Call type: client` in the note body.

```text
(apply label: Robie lead follow-up)
Who to call: the insured
What to say: Review the quote Carlo put together.
```

Writing the phrase in the discussion title or note body works the same as applying the label.

**`Robie Call` is unchanged** — carrier by default; client only when the note says `Call type: client` or who-to-call is the insured:

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

- **Both labels on the same note:** `Robie lead follow-up` wins → `client_followup`.
- **Client path (`call_type=client_followup`):** Robie greets the insured by **first name only** (EZLynx `FirstName` / preferred / nickname; commercial accounts use CommercialDetail / PrimaryContact / Contacts[] — never the first token of an LLC). Mentions the **Sales Center opportunity `producerName`** ("quote {producerName} put together"). Carlo decision 2026-09-05 (verified on Buster Brown / applicant `26356199`): greeting is Sales Center `producerName`, not account Assigned Producer and not commission policy Producer. Staff transfer briefing may cite the business as account context (`I have Buster on the line about Green Lion Lawn Care`) but never greets the client with a full personal name. Busy / no / voicemail: do **not** warm-transfer; leave a short polite close asking them to call the agency main **732-462-8343** (RingCentral office PBX), not a producer cell/DID (Carlo voicemail-callback decision).
- **These are three different EZLynx fields:** Assigned Producer (`GetApplicantSidebar` → `Applicant.Assignment.AssignedTo` full name; Classic Applicant/v2 `AssignedTo` is only a username like `Carlo1`) ≠ Sales Producer (`GetOpportunitiesForApplicant` → `opportunities[].producerName`) ≠ commission Producer (`CommissionProducers[].Producer.ProducerName`, e.g. Brittni). CSR is `CsrUserModel.FullName` and is never the greeting source.
- **Fallback:** only if Sales Center has no `producerName`, optionally use portal `Assignment.AssignedTo` full name. Never invent a name. Never use commission Producer. Never treat Classic `AssignedTo` username as the greeting without resolution.
- **Warm transfer (unchanged):** Bland warm-transfers to the **requestor** — the **label invoker** (note author / email sender), resolved from the voice directory DID. Greeting producer and transfer target are independent.
- **Carrier path (default for `Robie Call`):** Existing underwriting follow-up. After a live human is confirmed, Robie can warm-transfer to the same requestor.
- **Worked example:** Mike Sosa applies `Robie lead follow-up` (or `Robie Call` + `Call type: client`) on an account whose Sales Center `producerName` is Carlo Ferrara. Greeting says Carlo; transfer DID is Mike's RingCentral Direct Number `+17326540947`.
- Requestor is read from Portal `GetPagedDiscussions` note metadata (`discussionNote.createdByName` / `createdBy` / `userName` / email, then card `lastModifiedByName`). Lookup is by name, alias, or email in `producers[]`. Email or name alone is not enough — the row must have an E.164 `phone` or transfer is skipped. **Never fall back to the Sales Center producer or another staff DID.**
- Assigned CSR for EZLynx tasks remains Carlo Ferrara (never Robie).
- Creating the `Robie lead follow-up` org label in the live EZLynx UI is orchestration's job; this repo only recognizes the name (and close variants) on `noteLabels[].labelName` or in title/note text.

#### Client outreach (cancellations / action-needed) — Carlo 2026-09-05
**Preferred CSR path when the insured must do something** (cancellation, documents, payment, “please call us back”): apply **`Robie client outreach`** or the matching pathway WF label from the Admin list (`Robie audit`, `Robie cancellation`, …). Close variants match case-insensitively: spaces / hyphens / underscores, optional `robie ` prefix on the label, brackets. A standalone **`Robie audit`** (label only) dispatches — the CSR does not also need `Robie client outreach`. That label alone dispatches like `Robie Call` and **forces `call_type=client_outreach`**. You do **not** write `Call type: client` and you do **not** use the lead-follow-up greeting. Pathway is inferred from the **same** label; `Robie client outreach` stays generic unless the note body matches a pathway.

```text
(apply label: Robie client outreach)
What to say: Policy is pending cancellation — please call to keep coverage or confirm they want to cancel.
```

Writing the phrase in the discussion title or note body works the same as applying the label.

- **Dial order (locked):** always primary applicant first, then secondary / co-applicant when both have phones. Phone fields: `CellPhone` → `HomePhone` → `WorkPhone` (Classic Applicant/v2 `BusinessPhone` is the work-line alias; portal sidebar `ContactInfo` is also read). Skip anyone with no valid US E.164 — **never invent numbers**. If primary and secondary share the same number, call once.
- **Buster Brown (applicant `26356199`):** primary cell `7329953409`; co-applicant currently has no cell — secondary dial is skipped until a phone exists.
- **Each call:** greet that person by **first name only** (same commercial-contact sources as lead follow-up; generic Hi/Hello if none — never “Hi Green Lion…”). On a clear yes, warm-transfer to the **account Assigned Producer** (`GetApplicantSidebar` → `Applicant.Assignment.AssignedTo` full name, resolved via `lookup_producer` / RingCentral DID). **Not** Sales Center `producerName`. **Not** the label invoker. If Assigned Producer has no E.164 DID, skip transfer (voicemail still asks them to call **732-462-8343**). Never fall back to Sales Center producer or another staff DID. Do **not** say “the quote {Sales Center producer} put together”.
- **Splice-replacement conversational pathways** — source of truth is Carlo's Google Doc Manual WFs ([doc](https://docs.google.com/document/d/1cZCe_9cz_fuNWYZLjdNvP1Z3jhkUIqGrYZq2pwJdaPM/edit)). Conversational Robie only (no press-1 / 2 / 4 / 6, no Splice toll-free). `<<Agent>>` = Assigned Producer (first name in client copy). Inferred from CSR `What to say` / note body / alias:
  - `audit` → “It appears that an audit for your account is currently incomplete. Please take the necessary steps to finalize this audit as soon as possible.”
  - `recommendations` → “We are following up on some recommendations that were made for your account. Please take the necessary steps to address these recommendations as soon as possible.”
  - `returned_mail` → “We have received some returned mail for your account. Please contact our office to update your information as soon as possible.”
  - `esign` → “We are following up on an e-signature request for your account. Please complete the e-signature process as soon as possible.”
  - `additional_info` → “We are following up on a request for additional information for your account. Please provide the requested information as soon as possible.”
  - `unresponsive` → “We are reaching out regarding your policies.”
  - `renewal_reachout` → “Your insurance policy will be up for renewal soon. We want to ensure you have the proper coverage and would like to discuss your options.” Fires **only** if the CSR labels/notes this phrase — **not** from the manual renewal pipeline.
  - `cancellation` / `robie cancellation` → Cancellation Notice PDF (not in that doc body): overdue payment, policy set to be cancelled, pay by {date} to avoid lapse. Default when the note looks like cancel/non-pay.
  - fallback → existing generic outreach (CSR What to say).
- **Not ported:** Birthday, Additional Policy, Applicant Created, New Customer/Welcome, Policy Reinstatement, Policy Renewed, Upcoming Renewal/Expiration EZLynx automations, Winback, Sales Center New/Contacted/Quoted/Won. Sales Center Reviewed Status (“quote we released a few days ago”) is already **Robie lead follow-up** — leave that path alone.
- **Both `Robie Call` and `Robie client outreach` (or any pathway WF label) on the same note:** client outreach wins → `client_outreach`.
- **Both `Robie lead follow-up` and an outreach pathway label on the same note:** client outreach wins → `client_outreach` (existing `infer_call_type` order: outreach, then lead, then Robie Call/carrier). Do not invert this.
- **`Robie lead follow-up` is unchanged** when no outreach label is present — still `client_followup` with the Sales Center `producerName` greeting and transfer to the **label invoker**.
- **`Robie Call` is unchanged** — stays `carrier` unless `Call type: client` / who-to-call insured. A carrier note that merely mentions “audit” does **not** dispatch client outreach.
- Assigned CSR for EZLynx notes/tasks remains Carlo Ferrara. Caller ID `+17322986745`. No Bland `max_duration`.
- Creating the Admin org label **names** in the live EZLynx UI is Carlo / Admin’s job; this repo only recognizes the names (and close variants). Do not invent `organizationLabelId` values.

#### Manual renewal WF — one carrier Robie Call after N=2 (Carlo 2026-09-05)
Live insertion is the give-up branch of `OutreachCadenceManager.process_due_followups` in `src/email_outreach/thread_tracker.py` (the path that today sets `ThreadStatus.EXHAUSTED` + `ESCALATED_MANUAL` + CSR task “URGENT: Review / Call Carrier…”). Helper: `src/voice/renewal_cadence.py`. **Not** a parallel daily-runner scan, cron, or Bland stack.
1. Follow up no more than **twice** after the initial UW email (`settings.carrier_voice_after_followups = 2`). Then exactly one `VoiceCallDispatcher().dispatch(policy_number=...)` (`src/voice/dispatcher.py` → `CarrierVoiceClient.dispatch_call`, `call_type=carrier`). Do **not** post a Robie Call EZLynx label (avoids watcher loops).
2. Stop / skip if renewal is already in hand: `QUOTE_RECEIVED` or `READY_FOR_AGENT_REVIEW`, `ThreadStatus.RESOLVED`, or a renewal `DocumentRecord`. Do not treat unused `FOLLOWUP_SENT` as a signal.
3. After the one voice attempt, set `carrier_voice_attempted` so it never re-fires. CSR escalate (`max_followups=3` / 25d / Step 6) may still happen later. No client autodial from this cadence.
- Portal is only tried while `PENDING_EVALUATION` (no multi-day portal retry). PORTAL-only carriers with no UW email: do not invent email attempts; skip voice unless a carrier phone exists in the directory.
- Step 5b stays CSR **Email Robie to Call** only. **No default client-call Step 5c.**
- Production cron `0 9 * * *` on hermes-poc-01 is unchanged.

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
* `src/voice/outreach_pathways.py`: Splice-replacement conversational pathways for client outreach.
* `src/voice/renewal_cadence.py`: After 2 email/portal misses, one carrier Robie Call; stop when renewal lands.
* `src/voice/webhook_server.py`: Receives call completion payloads and writes notes to EZLynx API.
* `src/voice/email_dispatcher.py`: Parses incoming email triggers from Carlo and Jake.
* `src/voice/dispatcher.py`: CLI interface for testing and automated cron runs.
* `src/config.py`: Configuration settings (`voice_caller_id = "+17322986745"`).
* `.env`: Local environment variables.
* `tests/test_voice_*.py`: Automated pytest suite (100% passing).
