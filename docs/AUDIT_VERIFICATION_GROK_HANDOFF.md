# EZLynx Audit Verification — Grok / Hermes Handoff & Architecture Guide

**Version**: 1.1.0  
**Target Environment**: `hermes-poc-01` (`/opt/renewal-automation-system`)  
**Target Agent**: Grok / Hermes Autonomous Agent  
**Author**: Antigravity Pair-Programming Session with Carlo Ferrara  
**Date**: September 6, 2026  

---

## 1. Executive Summary

The **EZLynx Audit Verification** system (codename **ROBIE**) automates the audit lifecycle for Workers' Compensation (and commercial) policies across StreetSmart Insurance. In Workers' Comp, every renewal and cancellation requires a final payroll audit.

The system performs:
1. **Queue Ingestion**: Ingests daily scheduled EZLynx audit reports or active EZLynx audit tasks.
2. **Discussion-First Intelligence**: Queries existing applicant discussions to detect active tasks already created by teammates (e.g. Sandy Santana, Jackie Arriola, Eimy Ramos). It threads into active cards and respects CSR ownership and due dates.
3. **Download History & Carrier eDocs Verification**: Checks whether the carrier has already completed the audit (IVANS `Premium Audit` / `PRMAU` downloads showing return premium or additional premium credits/charges).
4. **Multi-Channel Carrier Retrieval**:
   - **Carrier Portals**: Generates exact portal URLs (e.g. The Hartford, Pie Insurance, Travelers).
   - **Underwriter Outreach**: Drafts carrier audit requests for email-serviced carriers (e.g. NJCRIB Assigned Risk carriers, Associated Specialty MGA).
   - **Autonomous Phone AI**: Staged voice prompts for outbound phone calls to carrier support (e.g. Pie Partner Support at `855-965-1840`).
5. **Client Communication**:
   - **Completed Audits**: Sends final audit results/adjustment statements to the client (`Audit Results (Completed Statement)` template).
   - **Pending Audits**: Sends audit questionnaires (`Audit` template) and stages voice auto-dial reminders.
6. **EZLynx Auditable Discussion Notes**: Posts structured, standardized notes ending with the mandatory signature line `ROBIE was here`.

---

## 2. EZLynx Scheduled Report Specification

### Does StreetSmart have an EZLynx report for this?
**YES.** StreetSmart has an active, scheduled EZLynx report generating every single morning.

| Parameter | Value |
| :--- | :--- |
| **Report Name** | `Workers_Comp_Renewal_Audit_Queue_-_ROBIE` |
| **Schedule Frequency** | **Daily at 06:06 AM** |
| **EZLynx Base Report** | **Policy Transaction Detail** Report |
| **Filter 1 (Transaction Type)** | `Renewal` (and `Cancellation`) |
| **Filter 2 (Line of Business)** | `Workers comp` |
| **Filter 3 (Effective Date)** | Rolling window: `"matches (advanced)" = 45 days ago for 15 days` (or renewal effective window) |
| **How to Access Filters** | In EZLynx, click the gear icon &rarr; **"Explore from Here"** to expose the full field picker, as default view clips filters. |
| **Local File Location** | `/opt/renewal-automation-system/data/input_reports/` (e.g. `EZLynx_Scheduled_1a06bf0a_Workers_Comp_Renewal_Audit_Queue_-_ROBIE_2026-09-04T0606.csv`) |

### Report CSV Column Schema:
```csv
Applicant ID,Branch,Account Name,Account Type,Assigned Producer,CSR,Policy Number,Policy ID,Policy Transaction ID,Transaction Type,Transaction Date,Line of Business,Master Company,Download Date,Effective Date,Expiration Date,Current Policy Status,Policy Term,Policy Type,Transaction Deleted,Service Team,Total Written Premium,Total Customers,Total Transactions
```

---

## 3. Core Operational Rules (StreetSmart SOP)

### Rule 1: Always Check Existing Discussions & Tasks First
- Never classify an account or create a duplicate task without checking `client.get_applicant_discussions(app_id)` first.
- Search for titles matching `audit`, `wc audit`, or `policy audit verification`.
- If an active task exists (e.g., Sandy Santana created a task assigned to Eimy Ramos: *"Pls check with carrier if audit was completed"*):
  - **Thread into that exact discussion ID**.
  - Respect the assigned CSR, creator, and due date.
  - Do NOT create a new duplicate discussion card.

### Rule 2: Check Download History / Carrier eDocs for Completed Audits
- In EZLynx, when an audit is finished by the carrier, it downloads as an IVANS transaction of type `Premium Audit` or `PRMAU` (e.g. `PRMAU: Premium Audit 13WBCAT1G7C WORK Effective 2025-07-21`).
- **If this transaction is present, THE AUDIT IS ALREADY COMPLETE.**
- **Action**: Do NOT send the client a blank audit form. Send them the completed audit statement with their premium adjustment (credit or bill), notify them via email/phone, and mark the EZLynx `Policy Audit Verification` checklist item complete.

### Rule 3: Guard Against Stale Documents from Prior Inactive Policies
- Filter out documents from old inactive policies or prior years (e.g., a 2024 cancellation notice on an expired policy).
- Never allow a 2024/2025 historical document to trigger a current urgent non-compliance exception on a 2026 renewal.

### Rule 4: Carrier Channel Matrix & Autonomous Voice
- **Carrier Portals**: The Hartford, Travelers, Selective, Pie Insurance.
- **Email Servicing**: NJCRIB Assigned Risk carriers (NJM, Hartford AR, PA Lumbermans, Continental), Associated Specialty MGA.
- **Autonomous Carrier Calls**: For carriers like Pie Insurance, provide autonomous outbound phone dispatch (`CarrierVoiceClient`) to carrier support (`(855) 965-1840`) to verify audit status.

### Rule 5: Mandatory Discussion Note Signature
- Every note written to EZLynx must end with the exact standalone line:
```text
ROBIE was here
```

---

## 4. Benchmark Accounts Verified Live

| Account Name | Applicant ID | Policy Number | Carrier | Live Finding & State |
| :--- | :--- | :--- | :--- | :--- |
| **Advanced Hair Designs LLC** | `112237823` | `13WBCAT1G7C` | The Hartford | **COMPLETED**: Download history verified audit completed on 08/11/2026 with **($105.00)** return premium credit. Delivered completed results statement; completed task checklist. |
| **Sun Volt Energy LLC** | `164706131` | `WC PI 2695561-001` | Pie Insurance | **READY FOR RESEARCH**: Read Sandy Santana's task assigned to Eimy Ramos (*"Pls check with carrier if audit was completed"* due 09/14/2026). Staged Pie portal check or support call at `855-965-1840`. |
| **Middlesex Gutter Supply Inc.** | `81168616` | `WC PI 1238424-002` | Pie Insurance | **READY FOR RESEARCH**: Read Jackie Arriola's task. Checked EZLynx Document Library (not in EZLynx yet). Staged Pie portal check or support call at `855-965-1840`. |

---

## 5. Server Deployment & Execution Commands

### Server Environment
- **GCP Instance**: `hermes-poc-01` (Zone `us-east1-b`, Project `streetsmart-hermes-poc`)
- **Code Directory**: `/opt/renewal-automation-system`
- **Virtualenv**: `/opt/renewal-automation-system/venv` (Python 3.12.3)
- **EZLynx Connectivity**:
  - Classic API: Connected (`ssr_userPROD`)
  - OAuth2 Gateway: Connected (`DiscussionApi`, `EzLynxApi`, `PolicyApi`)
  - Chrome CDP: Active on `http://localhost:9222`
  - Browser Storage: Active (273 cookies)

### How Grok Can Run the System

#### 1. Run on a Specific Applicant / Policy
```bash
cd /opt/renewal-automation-system
PYTHONPATH=. ./venv/bin/python3 scripts/run_audit_verification.py \
  --applicant 112237823 \
  --policy 13WBCAT1G7C
```

#### 2. Run the Entire Daily Scheduled Report
```bash
cd /opt/renewal-automation-system
PYTHONPATH=. ./venv/bin/python3 scripts/run_audit_verification.py \
  --input data/input_reports/EZLynx_Scheduled_1a06bf0a_Workers_Comp_Renewal_Audit_Queue_-_ROBIE_2026-09-04T0606.csv
```

#### 3. Output Machine-Readable JSON for Downstream Pipelines
```bash
cd /opt/renewal-automation-system
PYTHONPATH=. ./venv/bin/python3 scripts/run_audit_verification.py \
  --input data/input_reports/EZLynx_Scheduled_1a06bf0a_Workers_Comp_Renewal_Audit_Queue_-_ROBIE_2026-09-04T0606.csv \
  --json
```

#### 4. Run Automated Unit Test Suite
```bash
cd /opt/renewal-automation-system
PYTHONPATH=. ./venv/bin/pytest tests/test_audit_verification.py tests/test_ezlynx_discussions.py tests/test_ezlynx_api_client.py tests/test_voice_context_hydrator.py tests/test_voice_dispatcher.py
```

---

## 6. Code Architecture Map

```text
/opt/renewal-automation-system/
├── src/
│   ├── ezlynx/
│   │   ├── audit_verification.py     <-- Core engine: classifier, note builder, email builder, voice dispatcher, verifier
│   │   ├── api_client.py             <-- EZLynx API (Applicant, Policy, Documents, Discussions)
│   │   └── discussion_writer.py      <-- Discussion card creator and updater
│   └── voice/
│       ├── context_hydrator.py       <-- Hydrates applicant and carrier data for voice agents
│       └── dispatcher.py             <-- Autonomous voice call dispatcher (LiveKit / SIP / Twilio)
├── scripts/
│   ├── run_audit_verification.py     <-- Production CLI runner (single account or batch CSV)
│   ├── ezlynx_cli.py                 <-- EZLynx session manager and debug CLI
│   └── sync_to_hermes_vm.sh          <-- Local to GCP VM sync script
├── tests/
│   ├── test_audit_verification.py    <-- Unit tests for classifier, notes, emails, and dispatcher
│   └── ...
└── data/
    ├── input_reports/                <-- Daily scheduled EZLynx CSV reports
    └── ezlynx_parsed_carrier_summary.json <-- Carrier directory rules & underwriter emails
```

---

## 7. Next Steps for Grok / Production Integration

1. **Auto-Trigger via Cron**: Set up systemd timer or cron on `hermes-poc-01` to execute `scripts/run_audit_verification.py` at 06:15 AM daily, right after EZLynx delivers the 06:06 AM report.
2. **Autonomous Call Execution**: Wire `carrier_voice_call_draft` and `client_autodial_draft` directly into the live voice telephony bridge for automatic dialing with human-in-the-loop review.
3. **EZLynx Write Switch**: Once Carlo gives final approval on live note creation, flip the verification runner to post notes directly to EZLynx discussion cards via `client.create_discussion_note`.
