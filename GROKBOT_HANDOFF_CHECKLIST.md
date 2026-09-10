# StreetSmart Insurance - Autonomous Manual Renewal System
## Grokbot Quality Assurance & Handoff Checklist

**Document Version**: 1.0.0  
**Effective Date**: September 4, 2026  
**Host Environment**: GCP Compute Engine (`hermes-poc-01.c.streetsmart-hermes-poc.internal` | Zone: `us-east1-b`)  
**Production Repository**: `/opt/renewal-automation-system`

---

## 1. Executive Summary & Purpose
This checklist serves as the authoritative Quality Assurance (QA) and handoff protocol for **Grokbot** (or external audit agents/human reviewers). It defines the operational criteria and validation gates required for managing expiring non-download manual commercial and personal insurance policies in EZLynx across carrier portals, dual-inbox Gmail outreach, and AMS synchronization.

---

## 2. Grokbot Verification & Audit Checklist

### Gate 1: Intake, Scope & Real-Time Eligibility
- [ ] **50-Day Renewal Window**: Only policies expiring within $T+50$ days are evaluated (`src/intake/email_ingest.py` / `data/renewals.db`).
- [ ] **IVANS Auto-Download Exclusion**: Policy carrier must be checked against `src/ivans/ivans_filter.py`. Auto-downloading lines are strictly excluded from manual intake.
- [ ] **Real-Time Policy Status Validation**: Checks EZLynx API (`GET /PolicyAPI/v1/PolicyCard/GetPolicies?applicantId=...` or REST API) to verify policy is currently in-force. Cancelled mid-term or inactive accounts are flagged `EXCLUDED_INACTIVE_ACCOUNT` and skipped.
- [ ] **Carrier Routing Accuracy**: Confirms `data/carrier_directory.json` accurately routes accounts to `PORTAL` (direct crawler) vs. `EMAIL` (underwriter outreach).

### Gate 2: Carrier Portal Automation & Document Retrieval
- [ ] **Authentic Packets Only (No Stubs)**: Every downloaded file must be a verified, authentic carrier declaration or renewal proposal PDF (Coterie, The Hartford, TAPCO, Berkshire Hathaway Homestate, etc.). Zero dummy or stub files.
- [ ] **Autonomous Multi-Inbox 2FA/OTP**: Portals prompting for 2FA must have verification codes intercepted via Google Workspace Domain-Wide Delegation (`robie@`, `hello@`, `carlo@`) in $< 3$ seconds without human intervention.
- [ ] **Accurate Premium Extraction**: Extracted expiring and renewal premiums must match the carrier document exactly (zero truncation, no missing decimal precision, no `NaN`).

### Gate 3: Dual-Inbox Email Outreach Cadence
- [ ] **Dual-Inbox Role Separation**:
  - `robie@streetsmart.insurance`: Outbound outreach requests and carrier logins.
  - `hello@streetsmart.insurance`: Inbound underwriter reply ingestion.
- [ ] **Outreach Cadence Window**: Outreach initiates at Day 45 prior to expiration; maximum 3 follow-ups spaced 5–7 business days apart.
- [ ] **Mandatory Tracking Tag**: All email subjects must contain the reference tag formatted as `[RENEWAL-REQ-###]`.
- [ ] **Mandatory CSR CC Guarantee**: Outbound emails must CC the assigned Account Manager / CSR, `jake@streetsmart.insurance`, and `carlo@streetsmart.insurance`.
- [ ] **Sender Identity**: Outbound emails are sent from `robie@streetsmart.insurance` signed by **Robie**, never spoofing an individual CSR.
- [ ] **Day 25 Auto-Escalation**: If no terms are received by Day 25, policy transitions to `ESCALATED_MANUAL`, creates a high-priority EZLynx task, and alerts the CSR.

### Gate 4: EZLynx Document Library Standards
- [ ] **Standardized Naming Convention**: Follows `YYYY-YY Renewal Offer - <Carrier> <Policy#>.pdf` (e.g., `2026-27 Renewal Offer - BHHC 02TRM066190-01.pdf`).
- [ ] **Target Folder Placement**: Must be uploaded directly into `Documents > Renewal Offers/Declarations`, never left in the root applicant document directory.
- [ ] **Explicit Policy Association**: Document record must be explicitly associated to the specific policy number via Angular `mat-select` or Document Properties modal.
- [ ] **System Labeling**: The system label **`Renewal Offer`** (`#label-139009`) must be applied to the document record.

### Gate 5: EZLynx Discussion Cards & Note Threading
- [ ] **Authentic Discussion Card Threading**:
  - Matches the established CSR renewal card via exact string matching on `DiscussionTitle` (e.g., `Commercial Auto Renewal  2026-2027`, `Business Owners Manual Renewal`, or `Manual Inland marine (comm) Renewal`).
  - Never creates orphan duplicate discussion cards.
- [ ] **Auxiliary Thread Disqualification**:
  - Auto-matching logic must explicitly exclude auxiliary threads such as `Loss Runs request ...`, `Certificate of Insurance`, `COI`, `Text Sent`, and `Email sent by Automation Center`.
- [ ] **Policy Reference Header**: Every note must begin with the header:
  ```text
  Policy: #{policy_number} ({line_of_business} - {carrier_name})
  ```
- [ ] **Financial & Coverage Breakdown**: Every note must detail Expiring Premium, Quoted Renewal Premium, delta (\$ and %), limits, deductibles, and document file references.
- [ ] **Mandatory Sign-off**: Every note must end with:
  ```text
  Robie was here
  ```
- [ ] **Direct REST API Utilization**: Notes must be posted via Classic REST API (`POST https://services.ezlynx.com/ezlynxapi/api/note/v1`) using the authenticated `SSRobie` account for sub-second execution.

### Gate 6: Production Infrastructure & GCP VM Deployment
- [ ] **Host Environment**: Pipeline executes on GCP VM instance **`hermes-poc-01`** (`streetsmart-hermes-poc` in `us-east1-b`).
- [ ] **Secrets & Permissions**: GCP Secret Manager (`workspace-inbox-tracker`) supplies all API keys, service account credentials, and portal passwords.
- [ ] **Unit & Integration Tests**: All test suites must pass on the server:
  ```bash
  cd /opt/renewal-automation-system && PYTHONPATH=. venv/bin/pytest tests/
  ```

---

## 3. Active Accounts Verification Matrix

| Client Name | Policy Number | LOB | Carrier | Target Discussion Card | Document Staged | System Label | Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **[Edwin Lema](https://app.ezlynx.com/web/account/145217363/overview)** | `A23B8960-78760-SSRM NTL` | Comm Auto (NTL) | Trinity / Progressive | `Commercial Auto Renewal  2026-2027` (17 notes) | `Renewal Offers/Declarations` | `Renewal Offer` | Verified |
| **[Maier Solar LLC DBA Solar Me](https://app.ezlynx.com/web/account/25156187/overview)** | `13 MS BL8665` | Inland Marine | The Hartford | `Manual Inland marine (comm) Renewal` | `Renewal Offers/Declarations` | `Renewal Offer` | Verified (Sandeep alerted) |
| **[Green Lion Lawn Care LLC](https://app.ezlynx.com/web/account/21587333/overview)** | `CBB-00113127-02` | BOP | Coterie | `Business Owners Manual Renewal` (12 notes) | `Renewal Offers/Declarations` | `Renewal Offer` | Verified |
| **[ABC TRANSPIRATION LLC](https://app.ezlynx.com/web/account/193438339/overview)** | `02TRM066190-01` | Comm Auto | Berkshire Hathaway (BHHC) | `Commercial Auto Renewal (2026-2027)` (33 notes) | `Renewal Offers/Declarations` | `Renewal Offer` | Verified (Loss Runs clarified) |

---

## 4. Key Server Commands (GCP VM: `hermes-poc-01`)

```bash
# Connect to GCP VM
gcloud compute ssh hermes-poc-01 --zone=us-east1-b --project=streetsmart-hermes-poc

# Navigate to project directory
cd /opt/renewal-automation-system

# Run automated test suite
PYTHONPATH=. venv/bin/pytest tests/

# Run daily intake & outreach pipeline
PYTHONPATH=. venv/bin/python3 -m src.main --run-today

# Query applicant status via EZLynx CLI
PYTHONPATH=. venv/bin/python3 scripts/ezlynx_cli.py applicant <ApplicantID>

# Post discussion audit note via CLI
PYTHONPATH=. venv/bin/python3 scripts/ezlynx_cli.py note <ApplicantID> "<NoteText>" --policy-number <PolicyNumber>
```
