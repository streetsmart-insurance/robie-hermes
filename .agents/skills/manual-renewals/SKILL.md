---
name: manual-renewals
description: Operates, tests, and monitors the StreetSmart Autonomous Manual Renewal System across EZLynx, Carrier Portals, and Dual-Inbox Gmail.
---

# Autonomous Manual Renewal System - Skill Guide

## Overview
This skill governs the autonomous engine for managing expiring non-download manual insurance policies in EZLynx for StreetSmart Insurance. It operates within a 50-day expiration window, filtering out auto-downloading IVANS carriers, verifying policy active status in real time, crawling carrier portals, conducting underwriter outreach via dual-inbox Gmail, posting standardized audit notes to EZLynx discussions, and generating daily handoff reports.

---

## 1. Intake & Real-Time Policy Status Validation
EZLynx reports or Looker exports may list 1-year policy terms that were cancelled mid-term. **Never send outreach on unverified policies.**

### Verification Protocol:
- **Direct REST API (Recommended - 200ms)**:
  Run `scripts/ezlynx_cli.py policies {applicantId} --json` or in Python `client.get_applicant_policies(applicant_id)`.
  Instantly checks all applicant policies across all lines of business without browser overhead.
- **Browser Internal API (Fallback)**:
  Query EZLynx Internal Policy API via Chromium:
  `GET /PolicyAPI/v1/PolicyCard/GetPolicies?applicantId={applicantId}`
- Inspect policy record:
  - Active policy with upcoming expiration: Eligible for renewal processing.
  - Inactive / mid-term cancelled policy: Mark `EXCLUDED_INACTIVE_ACCOUNT` and bypass.

---

## 2. Carrier Routing (Portal vs. Email)
Refer to `data/carrier_directory.json` for routing channels:
- **`PORTAL`**: Crawl carrier portal via Playwright (e.g., Coterie, The Hartford, TAPCO, Swyfft, Pathpoint, Utica, Hyundai). Fetch 2FA OTP codes from `workspace-inbox-tracker` if prompted.
- **`EMAIL`**: Conduct outreach to carrier underwriters (e.g., Trinity Underwriters, AmWINS MGA, Risk Placement Services MGA, Insurtec Inc MGA).
- **`EMAIL_ASK_PORTAL`**: Inquire about portal access on initial email outreach.

---

## 3. Outreach Cadence, CSR CC Guarantee & 25-Day Auto-Escalation
Outbound email outreach is conducted using Google Workspace Gmail APIs.

### Outreach Cadence & Timing Rules:
1. **Outreach Window (Day 45 to Day 25)**: Initial outreach begins at Day 45 prior to policy expiration. Policies expiring in > 45 days remain in the upcoming queue.
2. **Follow-Up Frequency**: Check back **no more than twice** (email and/or portal). After **two** unsuccessful attempts with no renewal in hand, place **exactly one** outbound carrier **Robie Call** (`call_type=carrier`) via `src/voice/renewal_cadence.py` / existing `CarrierVoiceClient`. Same style as today’s email `Call Carrier:` path. Never invent a carrier phone; if no E.164 underwriter/carrier number, post an EZLynx note ending with `Robie was here` and skip the dial.
3. **Stop when the renewal lands**: If a renewal PDF is filed, the UW reply filer matched, or pipeline status says we have the dec/offer → **STOP**. No more carrier calls. Do **not** auto-dial the client on a successful or in-progress manual renewal.
4. **25-Day Auto-Escalation to CSR**: If no renewal quote is received by **Day 25** prior to expiration (or if email follow-ups are exhausted), the engine automatically:
   - Sets status to `RenewalStatus.ESCALATED_MANUAL`.
   - Posts a high-priority audit note to the EZLynx discussion card ending with `Robie was here`.
   - Creates a high-priority EZLynx task assigned to the CSR for direct underwriter/phone escalation.
5. **Instant CSR Reply Alerts**: When an underwriter replies or sends terms, the engine:
   - Immediately dispatches a high-priority email alert to the assigned CSR (CC'ing `carlo@streetsmart.insurance` and `jake@streetsmart.insurance`).
   - Logs the underwriter response note to the EZLynx discussion card.
   - Uploads attached quote PDFs to the EZLynx Documents tab and creates a review task.
6. **Sender Identity**: All outbound outreach and cadence follow-ups **MUST be sent from `robie@streetsmart.insurance` and signed by Robie**, NEVER the assigned CSR.
   ```text
   Should you have any questions please feel free to email me back.

   Thank you,

   Robie
   StreetSmart Insurance
   ```
7. **Mandatory CSR CC Guarantee**: Outbound emails MUST ALWAYS CC the assigned CSR (resolved via `CSR_EMAIL_DIRECTORY` with fallback to `sandy@streetsmart.insurance` if unmapped) and `jake@streetsmart.insurance`.
8. **Subject Line Tracking Tag**: Every thread must include a unique tracking reference tag formatted as `[RENEWAL-REQ-###]`.

---

## 4. Scheduled Report Ingestion (Automated Intake)
EZLynx scheduled reports (configured under `Reports` → `Saved Reports` → `Scheduled Reports`) email the daily renewal queue CSV to `robie@streetsmart.insurance` each morning.
- **Intake Engine**: `src/intake/email_ingest.py` polls Robie's inbox for emails with attachments matching `(from:(appliedsystems.com OR ezlynx.com) OR subject:(EZLynx OR Renewal OR Scheduled)) has:attachment`.
- Downloads CSV/XLSX attachments into `data/input_reports/` and syncs records into SQLite `renewals.db`.
- Can be invoked directly: `PYTHONPATH=. .venv/bin/python3 -m src.intake.email_ingest`.

---

## 5. Carrier Portals & Nicole Login Coordination
For carriers requiring portal crawling (e.g., Coterie, The Hartford, TAPCO, Swyfft, Pathpoint, Utica, Hyundai, Selective, Johnson & Johnson, Orchid, MGA Resource, Universal Property):
- **Coordination SOP**: Nicole (`nicole@streetsmart.insurance`) coordinates credentials and MFA verification with Carlo (`carlo@streetsmart.insurance`).
- Daily Handoff Report prominently highlights the portal login queue for Nicole and Carlo.
- Credentials stored securely in macOS Keychain / GCP Secret Manager (`workspace-inbox-tracker`).

---

## 6. EZLynx Discussion Note Automation & Mandatory Policy Association
Audit notes are posted directly to the relevant line-of-business Discussion in EZLynx via the direct REST API (`EZLynxApiClient.add_note_to_discussion`) or Playwright over Chrome CDP.

### MANDATORY Policy Association Rules:
1. **Header Formatting**: Every note posted MUST begin with the policy header linking the record:
   ```text
   Policy: #{policy_number} ({line_of_business} - {carrier_name})
   ```
2. **Discussion Title**: Must reflect the policy number (e.g., `Renewal Manual {LOB} | {PolicyNumber} {Carrier}`).
3. **API Association**: Always pass `policy_number`, `line_of_business`, and `carrier_name` to ensure EZLynx associates the note directly with the policy object, ensuring it displays in the policy's individual history.
4. **Mandatory Signature**: Every note must end with:
   ```text
   Robie was here
   ```

### Discussion Matching Hierarchy:
1. Target discussion title corresponding to the policy line of business:
   - `Commercial Auto Renewal [Year]`
   - `Commercial Package Renewal`
   - `Excess Manual Renewal`
   - `General Liability Renewal`
   - `Workers Compensation Renewal`
2. **DOM Action** (when using UI fallback):
   - Locate `.activity-container` containing the discussion title.
   - Click `button[title="Add to Discussion"]` (or dispatch click event via JavaScript evaluation).
   - Wait for `#txtNote` textarea to be visible.
   - Enter standardized note body with policy header.
   - Ensure `Robie was here` signature is present.
   - Click `button:has-text("Save")` (excluding Reset).
   - Capture a screenshot of the updated card to verify successful posting.

---

## 7. EZLynx Document Library & Upload Protocol (Playwright)
When uploading renewal policies, packets, or carrier documents to EZLynx:
- **Always Enter the Target Folder First**:
  EZLynx default tables pin 24+ agency/system folders to the top of the Document Library. Do NOT upload to the root and attempt to move the document. Instead, drill into the destination folder first (e.g., `Renewal Offers/Declarations`).
- **Target Cell Clicking for Folder Navigation**:
  Do NOT use `row.dblclick()` on the `<tr>` element (Playwright clicks the center, hitting empty whitespace). Instead, click directly on the text cell:
  ```python
  await page.locator('td:has-text("Renewal Offers/Declarations")').first.click()
  await page.wait_for_timeout(3000)
  ```
- **Nested Iframe Scoping for the Upload Modal**:
  The upload dialog is hosted inside an external iframe (`iframe[src*="documentactions/upload"]`). Standard page-level locators will time out. Always bind to the frame:
  ```python
  # Click Add -> Upload
  await page.locator('button.mat-mdc-menu-trigger:has-text("Add")').first.click()
  await page.locator('[role="menuitem"]:has-text("Upload")').first.click()
  frame = page.frame_locator('iframe[src*="documentactions/upload"]')
  
  # Stage File
  await frame.locator('input[type="file"]').set_input_files(pdf_abs_path)
  
  # Document Name
  doc_name = f"{effective_year}-{expiring_year[-2:]} Renewal Offer - {carrier} {policy_num}.pdf"
  await frame.locator('#file-desc-0, input[placeholder*="Document Name"]').first.fill(doc_name)
  
  # Policy Association (Angular Material mat-select)
  policy_dropdown = frame.locator('#selected-policyor-application-0, mat-select[id*="selected-policy"]').first
  if await policy_dropdown.count() > 0:
      await policy_dropdown.click()
      await frame.locator(f'mat-option:has-text("{policy_num}")').first.click()
      
  # Submit Upload
  await frame.locator('button:has-text("Upload")').first.click()
  ```
- **Document Naming Convention**:
  Follow standard agency conventions: `YYYY-YY Renewal Offer - <Carrier> <Policy#>.pdf` (e.g., `2026-27 Renewal Offer - Coterie CBB-00113127-02.pdf`).

---

## 8. EZLynx Policy Renewal & FormEntry Data Entry Protocol
Autonomous manual renewal requires a two-step renewal and data entry pipeline in EZLynx:

### Step 1: Renewal Transaction Shell
1. Navigate directly to the policy renewal action URL:
   `https://app.ezlynx.com/applicantportal/Policy/Actions/Renew/{applicant_id}/{policy_id}`
2. Populate the core financial and term fields:
   - `#Premium`: Written premium (raw numeric, e.g. `5182.00`).
   - `#FullTermPremium`: Full term premium (e.g. `5182.00`).
   - `#AnnualPremium`: Annualized premium (e.g. `5182.00`).
   - `#Description`: Standard format `Renewal of <expiring_policy_number>`.
3. **Commit Action**: Click **`Renew & Edit Policy`** (`button:has-text("Renew & Edit Policy")`).
   *CRITICAL*: Do NOT click just "Renew Policy"; "Renew & Edit Policy" commits the shell, generates the new transaction ID, and transitions directly into the FormEntry editor (`/applicantportal/Policy/{policy_id}/FormEntry/Index/{transaction_id}`).

### Step 2: FormEntry Deep Coverage Entry
FormEntry contains multi-tab ACORD-level policy data (`Insured Information`, `Policy Level Coverages`, `Premises Information`, `GL Class Codes`, `Underwriting`, `Additional Interest / Policy Contacts`).
1. **Navigating to Policy Level Coverages**:
   Click `text="Policy Level Coverages"`.
2. **Expanding Nested Accordions**:
   Coverages live inside collapsible accordions. You must click the accordion heading to expand the inputs:
   ```python
   await page.locator('text="Liability - Policy Level Coverages"').first.click()
   ```
3. **Field ID Mapping & Formatting**:
   Pass raw, unformatted numeric strings (EZLynx formats currency on blur):
   - Occurrence Limit: `#GeneralLiability_BodilyInjury_EachOccurrenceLimitAmount_A`
   - General Aggregate Limit: `#GeneralLiability_BodilyInjury_AggregateLimitAmount_A`
   - Products & Completed Ops Aggregate: `#GeneralLiability_ProductsAndCompletedOperations_AggregateLimitAmount_A`
   - Personal & Advertising Injury: `#GeneralLiability_PersonalAndAdvertisingInjury_LimitAmount_A`
   - Damage to Rented Premises: `#GeneralLiability_OtherCoverageLimitAmount_A`
   - Medical Expense (Per Person): `#GeneralLiability_MedicalExpense_EachPersonLimitAmount_A`
   - Bodily Injury / Property Deductible: `#GeneralLiability_BodilyInjury_DeductibleAmount_A`
4. **GL Class Codes & Exposure**:
   Under `GL Class Codes` tab, verify class code (e.g. `18021` for Landscape Gardening), rating basis (`Payroll`), and exposure.
5. **Committing Changes**:
   Click `button:has-text("Save & Close")`. This commits all tabs across the policy and redirects to `/summary/index`.

### Step 3: Verifying Renewal Lifecycle State
- **"Pending" Renewal Term in History**:
  EZLynx policy overviews are filtered "As of: Today". A future renewal term (e.g. effective next month) will NOT overwrite the active policy overview card today.
- **Verification Location**: Check the policy's **`History`** tab (`/Policy/{policy_id}/summary/index` -> `History`). The renewal will appear as a `Pending` transaction row with the updated term (`RENEWAL (MM/DD/YYYY - MM/DD/YYYY)`), change amount, and updated full-term premium.

---

## 9. EZLynx Playwright Automation Guardrails
- **Selector Collision Avoidance (Global Search vs. Local Filter)**:
  `page.locator("input[placeholder*='Search']")` routinely collides with the global applicant search bar `#searchPhrase` in the top navigation. Always scope table searches to the container:
  `page.locator(".document-management input, ez-document-management input")`.
- **Nested Scrollable Containers**:
  Standard `window.scrollTo(0, 0)` fails because EZLynx wraps main content in internal scroll divs (`.body-content`, `mat-drawer-content`). Use `element.scroll_into_view_if_needed()` or scroll the container element directly.
- **Angular Material Dropdowns**:
  When interacting with Angular Material dropdowns (`mat-select`), always wait for the `.cdk-overlay-container` or `mat-option` elements to render before attempting to click options.

---

## 10. Daily Handoff Reporting Workflow
At the conclusion of each renewal run, generate and email a Daily Handoff Report to team leadership (`carlo@`, `jake@`, `gabriela@`, `sandy@`, `ashley@`).

### Report Structure:
1. **Executive Summary**: Total active processed, upcoming queue count, mid-term cancelled excluded, and total premium.
2. **Processed Accounts Table with Direct Hyperlinks**: Every account name links directly to `https://app.ezlynx.com/web/account/{applicant_id}/activity`.
3. **Audit Proof / Embedded Screenshots**: Verified screenshots showing discussion cards with "Robie was here".
4. **Upcoming Accounts Queue (Next 7–14 Days)**: Manual accounts expiring in 35–45 days queued for future outreach.
5. **Excluded Accounts Table**: Real-time cancelled/inactive accounts filtered out with explicit reasons.
6. **Portal Credentials Queue**: Prominent callout for Nicole and Carlo to coordinate carrier portal logins.
7. **Next Scheduled Actions**: Outline of upcoming cadence follow-ups and auto-escalations.

---

## 11. Magellan AI Conversational Intelligence & Churn Risk
Integrates with Magellan (`https://app.magellan.insure`) to cross-reference renewing accounts against customer phone call transcripts, sentiments, and intent tags.
- **Sentiment Categories**: `SATISFIED`, `NEUTRAL`, `SAD_FRUSTRATED`.
- **At-Risk & Cancellation Tags**: If a customer has a recent Magellan call tagged with "Sad", "Frustrated", "Cancellation", or "At-Risk Customer", the system flags this directly in the EZLynx discussion card so Account Managers are alerted before presenting renewal terms.
- **Phone Systems Note**: RingCentral is explicitly excluded from the manual renewal workflow. Intake relies strictly on EZLynx expiration reports and customer sentiment from Magellan.

---

## 12. Key Commands & Execution Scripts
- **Ingest Scheduled Reports via Email**:
  `PYTHONPATH=. .venv/bin/python3 -m src.intake.email_ingest`
- **Run Pipeline with 45-25 Day Cadence**:
  `PYTHONPATH=. .venv/bin/python3 -m src.main --run-today`
- **Generate & Send Daily Handoff Email**:
  `PYTHONPATH=. .venv/bin/python3 scripts/generate_and_send_today_handoff.py`
- **Execute Pytest Suite**:
  `PYTHONPATH=. .venv/bin/pytest tests/`
- **File underwriter replies (robie@ + hello@ → titled EZLynx cards)**:
  `PYTHONPATH=. .venv/bin/python3 -m src.email_outreach.uw_reply_filer --dry-run`
  `PYTHONPATH=. .venv/bin/python3 -m src.email_outreach.uw_reply_filer`
  `PYTHONPATH=. .venv/bin/pytest tests/test_uw_reply_filer.py tests/test_ezlynx_discussions.py`

