# Autonomous Daily Manual Renewal Engine for EZLynx

An end-to-end autonomous insurance renewal system designed for **EZLynx** that runs on a daily schedule to:
1. **Intake Expiring Policies**: Ingests daily reports and filters policies expiring within the **30 to 45 day** window.
2. **Carrier Portal Automation**: Uses headless **Playwright** to log into carrier portals and download renewal proposals.
3. **Underwriter Gmail Outreach**: Connects via **Gmail API** to request quotes from underwriters using tagged reference codes (`[RENEWAL-REQ-XXXX]`).
4. **Follow-Up Cadence Tracking**: Enforces a strict **5 to 7 business day** follow-up cadence until terms are received or escalated.
5. **EZLynx Discussion Notes**: Automatically logs detailed audit notes under the designated Discussion Title (e.g. `Manual Homeowners Renewal`, `Manual Commercial Auto Renewal`) for each applicant.
6. **Quote Ingestion & Task Creation**: Parses downloaded PDF quotes for renewal premiums, uploads the documents to EZLynx, and generates review tasks for Account Managers.
7. **Cloud & Headless Ready**: Pre-configured Docker environment (`Dockerfile` and `docker-compose.yml`) for seamless cloud hosting (GCP Cloud Run, AWS ECS, or DigitalOcean).

---

## 🏗 System Architecture

```
                               ┌───────────────────────────┐
                               │   Daily Report / Intake   │
                               │  (30-45 Days Expiration)  │
                               └─────────────┬─────────────┘
                                             │
                                             ▼
                             ┌───────────────────────────────┐
                             │  Carrier Portal Check         │
                             │  (Playwright Headless Chrome) │
                             └───────┬───────────────┬───────┘
                                     │               │
                     Quote Retrieved │               │ Quote Not Ready / Unsupported
                                     ▼               ▼
                        ┌──────────────────┐   ┌───────────────────────────┐
                        │ Parse PDF Quote  │   │ Underwriter Email Outreach│
                        │ & Extract Limits │   │ (Gmail API with Tracking) │
                        └────────┬─────────┘   └─────────────┬─────────────┘
                                 │                           │
                                 │               5-7 Day Cadence Follow-ups
                                 │               & Inbox Reply Classification
                                 │                           │
                                 ▼                           ▼
                        ┌──────────────────────────────────────────────────┐
                        │               EZLynx Integration                 │
                        │  • Post Audit Note to Discussion Title           │
                        │  • Upload Proposal PDF to Applicant Documents    │
                        │  • Create AM Renewal Presentation Task           │
                        └──────────────────────────────────────────────────┘
```

---

## 📁 Project Structure

```
renewal-automation-system/
├── Dockerfile                   # Production container with Playwright & Cron
├── docker-compose.yml           # Multi-container service configuration
├── pyproject.toml               # Python package configuration & dependencies
├── requirements.txt             # Locked dependencies
├── .env.example                 # Template for environment variables & API keys
├── data/
│   ├── input_reports/           # Daily renewal reports (CSV/Excel)
│   ├── downloads/               # Carrier quote PDFs & inbox attachments
│   └── renewals.db              # SQLite persistence database
├── src/
│   ├── config.py                # Pydantic settings & application paths
│   ├── main.py                  # CLI entrypoint for manual & daemon execution
│   ├── database/
│   │   ├── models.py            # SQLAlchemy ORM models (Policy, Thread, Audit, Doc)
│   │   └── session.py           # DB engine and session manager
│   ├── intake/
│   │   ├── base_source.py       # Data source interface and RawRenewalItem schema
│   │   └── report_ingestor.py   # Ingestion, 30-45d window filter, and note builder
│   ├── ezlynx/
│   │   ├── note_builder.py      # Standardized discussion note formatter
│   │   ├── api_client.py        # EZLynx REST API client (Notes, Docs, Tasks)
│   │   └── browser_adapter.py   # EZLynx Playwright UI automation fallback
│   ├── email_outreach/
│   │   ├── auth_setup.py        # Gmail OAuth2 & headless token management
│   │   ├── gmail_client.py      # Gmail API sender, thread tracker, & downloader
│   │   ├── templates.py         # Jinja2 outreach and 5-7d cadence templates
│   │   ├── intent_classifier.py # Reply classifier (Quote, Info Needed, Decline)
│   │   ├── thread_tracker.py    # Outreach cadence & follow-up scheduler
│   │   └── uw_reply_filer.py    # UW reply → titled EZLynx discussion filing
│   ├── portals/
│   │   ├── base_portal.py       # Base carrier crawler interface
│   │   └── carrier_agents.py    # Playwright crawlers (Travelers, Liberty, etc.)
│   ├── extractor/
│   │   └── quote_parser.py      # PDF text extractor for premiums and terms
│   └── scheduler/
│       └── daily_runner.py      # Master orchestrator & Rich terminal dashboard
└── tests/
    ├── test_intake_window.py
    ├── test_ezlynx_note_builder.py
    ├── test_gmail_cadence.py
    ├── test_intent_classifier.py
    └── test_quote_parser.py
```

---

## ⚙️ Configuration & Setup

### 1. Environment Variables
Copy `.env.example` to `.env` and fill in your agency credentials:

```bash
cp .env.example .env
```

Key configuration options:
- `RENEWAL_WINDOW_MIN_DAYS`: Default `30` (Start checking 30 days before expiration).
- `RENEWAL_WINDOW_MAX_DAYS`: Default `45` (Start checking up to 45 days before expiration).
- `FOLLOWUP_CADENCE_MIN_DAYS`: Default `5` (Days between underwriter follow-ups).
- `FOLLOWUP_CADENCE_MAX_DAYS`: Default `7` (Max days before sending reminder).
- `EZLYNX_CLIENT_ID` / `EZLYNX_CLIENT_SECRET`: EZLynx API credentials.
- `GMAIL_BOT_EMAIL`: The designated mailbox for sending and tracking outreach.

### 2. Gmail API Authorization
To authorize the Gmail API locally:
```bash
python3 src/main.py --auth-gmail
```
This generates `token.json`. For cloud deployments without interactive browser logins, you can base64 encode `token.json` into the `GMAIL_TOKEN_BASE64` environment variable.

---

## 🚀 Running the Automation

### Daily Manual Run
Execute the daily pipeline for today:
```bash
PYTHONPATH=. python3 src/main.py --run-today
```

### Date-Specific Simulation (Test Follow-ups)
Run for a simulated date (e.g. 7 days in the future to trigger follow-up emails):
```bash
PYTHONPATH=. python3 src/main.py --date 2026-09-08
```

### Daily Daemon Mode
Run continuously as a 24-hour background daemon:
```bash
PYTHONPATH=. python3 src/main.py --daemon
```

### Underwriter Reply → EZLynx Discussion Filing
When an underwriter replies to outreach in **`robie@streetsmart.insurance`** or **`hello@streetsmart.insurance`** (never CSR / org-wide inboxes), the filer:

1. Detects `[RENEWAL-REQ-###]` and/or policy number.
2. Matches the `PolicyRenewal` row (tracking tag, then existing `_matches_any_policy` helper).
3. Locates the existing titled renewal card via `EZLynxApiClient.find_matching_discussion` (skips Loss Runs, COI, Text Sent, Automation Center, cancellation, etc.).
4. Posts `EZLynxNoteBuilder.format_reply_received_note` — top line `Policy: #{policy_number} ({LOB} - {carrier})`, ends with `Robie was here`.
5. Alerts the assigned CSR and Carlo via `notify_csr_of_underwriter_reply` (existing handoff path).

It **will not** create an orphan / untitled discussion if no titled renewal card exists. The inbox cleaner still does not trash legitimate UW replies; filing is additive.

```bash
# Dry-run (detect + match only; no EZLynx post, no CSR email, no mark-read)
PYTHONPATH=. python3 -m src.email_outreach.uw_reply_filer --dry-run

# Live filing from robie@ + hello@
PYTHONPATH=. python3 -m src.email_outreach.uw_reply_filer

# Same path via main CLI
PYTHONPATH=. python3 -m src.main --file-uw-replies --dry-run
```

### Manual renewal shell (one CDP job)
Routine EZLynx shell keys use the hardened server recipe — **not** Antigravity/Gemini SSH/SCP micro-scripts. Verify on **hermes-test-01** before any Production zip.

```bash
# Dry-run / plan (no submit)
PYTHONPATH=. python3 scripts/run_manual_renewal.py --env test --dry-run \
  --applicant-id <ID> --policy-id <PID> --policy-number <NUM> \
  --lob "Workers comp" --writing-company "Associated Specialty" \
  --discussion-title "Renewal Manual Workers comp | PWC1239278 Associated Specialty Insurance" \
  --proof-json data/renewal_proofs/example.json

# Same module
PYTHONPATH=. python3 -m src.ezlynx.policy_renewer --help
```

Guards: **live CDP page preflight** (Login / `auth/account/login` / forcedOff → HITL `blocked` immediately; `storage_state` and Classic API are not proof — human re-logins SSRobie on hermes CDP `:9222`; bots must not password-reset), **one-shot firmed-quote PDF fetch** (Classic or `/Download/{numericId}`; strip leading `A-`; reject 0-byte / non-PDF after one corrected retry → HITL, no RadPdf/OCR), UI History pending-RWL proof (Classic API is insufficient), same term+premium dedupe, `#RenewPolicyBtn` only, Writing Company required, exact titled discussion, skip stubs &lt;10KB and skip docs/notes when `already_in`, Carlo Ferrara never Robie, no bind. Operator notes: `docs/MANUAL_RENEWAL_SHELL.md`. Tests: `pytest tests/test_cdp_session_preflight.py tests/test_policy_renewer.py tests/test_document_downloader.py -v`.

The daily pipeline (`--run-today`) and Robie inbox cleaner also invoke this path so cadence and cleanup stay in sync. Production cron on hermes-poc-01 is unchanged:

`0 9 * * * /opt/renewal-automation-system/scripts/run_daily_renewal_pipeline.sh`

That script already runs `process_incoming_inbox_replies`, which now delegates to this filer.

### Policy number term aliases
Renewal offers sometimes use a **new** policy number for the next term while `renewals.db` still stores the expiring number. `policy_number_aliases` maps prior ↔ current so UW-reply filing, discussion match, portal search, and document association treat both as the same account. Tests: `pytest tests/test_policy_number_aliases.py tests/test_ezlynx_discussions.py -v`.

### Running Automated Test Suite
```bash
PYTHONPATH=. pytest tests/ -v
PYTHONPATH=. pytest tests/test_uw_reply_filer.py tests/test_ezlynx_discussions.py -v
```

---

## ☁️ Cloud & Docker Deployment

Build and run in headless container mode:
```bash
docker-compose up -d --build
```
This runs the daily cron scheduler inside a container equipped with headless Chromium, Playwright drivers, and persistent volume mounts for data storage.
