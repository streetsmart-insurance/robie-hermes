# StreetSmart Insurance - Autonomous Manual Renewal Project

This repository contains the autonomous engine for managing expiring non-download manual insurance policies in EZLynx.

## Project Scope
- **Intake**: Filters EZLynx expiration reports for non-download / manual policies within a 50-day window.
- **Portals**: Uses Playwright to crawl carrier portals (Coterie, The Hartford, TAPCO, etc.) and download PDF renewal packets.
- **Email Outreach**: Uses Gmail API with dual-inbox support (`robie@streetsmart.insurance` and `hello@streetsmart.insurance`) to request terms from underwriters.
- **EZLynx Sync**: Posts standardized audit notes to the appropriate Discussion Title (e.g., `Manual Commercial General Liability Renewal`), uploads authentic renewal PDFs to the client Documents tab, and generates review tasks for Account Managers.
- **Security**: GCP Secret Manager (`workspace-inbox-tracker`) and macOS Keychain.

## Key Commands
- Run daily pipeline: `PYTHONPATH=. .venv/bin/python3 -m src.main --run-today`
- List carrier matrix: `PYTHONPATH=. .venv/bin/python3 -m src.main --list-carriers`
- File underwriter replies (robie@ + hello@ only) onto titled EZLynx cards:
  `PYTHONPATH=. .venv/bin/python3 -m src.email_outreach.uw_reply_filer --dry-run`
  `PYTHONPATH=. .venv/bin/python3 -m src.email_outreach.uw_reply_filer`
  `PYTHONPATH=. .venv/bin/python3 -m src.main --file-uw-replies --dry-run`
- Run test suite: `PYTHONPATH=. .venv/bin/pytest tests/`
- UW-reply filing tests: `PYTHONPATH=. .venv/bin/pytest tests/test_uw_reply_filer.py tests/test_ezlynx_discussions.py -v`

Production cron on hermes-poc-01 is **not** changed: `0 9 * * * /opt/renewal-automation-system/scripts/run_daily_renewal_pipeline.sh` (already calls `process_incoming_inbox_replies`).

Policy numbers may change for the renewal term (e.g. Safe Man `R2WC681352` → `R2WC771037`). Matching, UW-reply filing, portal search, and document association accept **either** number via `policy_number_aliases` (`src/database/policy_aliases.py`). Do not fail filing solely because the number flipped.

## EZLynx Universal API & Session Operations (For All Agents & Roles)
The production EZLynx API is fully configured (`ssr_userPROD`) across both Classic REST Services (`services.ezlynx.com`) and Modern OAuth2 Gateway (`app.ezlynx.com`).
**All agent roles and automated tools should prioritize the direct API over slow browser navigation:**
- **Search applicant**: `scripts/ezlynx_cli.py search "<Name, DBA, or Policy#>"` (supports `--json`)
- **Get applicant profile**: `scripts/ezlynx_cli.py applicant <ApplicantID>` (supports `--json`)
- **Get applicant policies**: `scripts/ezlynx_cli.py policies <ApplicantID>` (supports `--json`)
- **List document library**: `scripts/ezlynx_cli.py documents <ApplicantID>` (supports `--json`)
- **List applicant discussions**: `scripts/ezlynx_cli.py discussions <ApplicantID>` (supports `--json`)
- **Post discussion audit note**: `scripts/ezlynx_cli.py note <ApplicantID> "<NoteText>" --policy-number <Policy#>` (dynamically threads into existing CSR renewal card!)
- **Inspect session health**: `scripts/ezlynx_cli.py sessions`
- **Inspect quote results**: `scripts/ezlynx_cli.py quote <QuoteID>`

In Python scripts or custom agent tools:
```python
from src.ezlynx.api_client import EZLynxApiClient
client = EZLynxApiClient()
applicant = client.get_applicant(applicant_id)
policies = client.get_applicant_policies(applicant_id)
# Automatically searches active discussion cards and threads directly into CSR's card:
client.add_note_to_discussion(
    applicant_id=applicant_id,
    discussion_title=discussion_title,  # Optional/fallback; auto-resolved if policy_number provided
    note_text=note_text,
    policy_number=policy_number,
    line_of_business=line_of_business,
    carrier_name=carrier_name
)
```

## Universal Policy Association Mandate (All Roles, Tools & Skills)
Every discussion note, document upload, or activity task in EZLynx **MUST be explicitly associated to the specific policy**.
- **Header formatting**: Every note posted MUST include the policy reference header at the very top:
  `Policy: #{policy_number} ({line_of_business} - {carrier_name})`
- **API Payload**: Always pass `policy_number=policy_number`, `line_of_business=line_of_business`, and `carrier_name=carrier_name` to `client.add_note_to_discussion(...)` so EZLynx associates the note directly to the policy record.
- **Discussion Title**: Must reflect the policy number (e.g., `Renewal Manual {LOB} | {PolicyNumber} {Carrier}`).
- **Mandatory Signature**: Every note MUST end with:
  ```text
  Robie was here
  ```

## Carrier Portal Sub-Account Mandate (Always Email Nicole)
Whenever a carrier portal crawler requires credentials or dedicated sub-account access:
- **Always email Nicole** (`nicole@streetsmart.insurance`, CC `carlo@streetsmart.insurance`, `jake@streetsmart.insurance`).
- **Request a dedicated sub-account** for `robie@streetsmart.insurance` (Name: `Robie Automation`) with permissions to view policies & download documents.
- **Mandate 2FA via Email**: 2FA/MFA delivery method MUST be set to EMAIL (`robie@streetsmart.insurance`) so Robie intercepts verification codes headlessly via Gmail API.
- **Automated Dispatch**: Use `PYTHONPATH=. python3 -m src.portals.portal_credential_requester --carrier "<Carrier>" --portal-url "<URL>" --send`
- **Secrets Storage**: Store provisioned credentials into GCP Secret Manager under `<carrier_key>_username` and `<carrier_key>_password`.

## Urgent Non-Renewal & Lapse Notice Mandate (Tasks, NOT Notes)
Whenever a portal crawl, carrier document retrieval, or underwriter reply reveals a **Notice of Non-Renewal**, **Conditional Renewal**, or **Lapse Warning**:
- **Mandatory High-Priority Task**: The autonomous engine **MUST create a High-Priority Task (`!`) in EZLynx assigned to the Account Manager / CSR**, NOT just a discussion note. Non-renewal notices jeopardize client coverage and require proactive re-marketing or urgent client outreach.
- **Task Specifications**:
  - Priority: High Priority (`!`, toggle `#btnPriority`).
  - Due Date: Next business day (1 day out).
  - Assignee: The assigned CSR / Producer on the policy.
  - Body: Include policy reference header (`Policy: #{policy_number} ({line_of_business} - {carrier_name})`), clear reason for non-renewal, required actions (e.g. questionnaires, re-marketing), and mandatory signature `Robie was here`.
  - Policy Association: Must be associated to the specific policy record.

## Mandatory Task Assignment Mandate (Never Just a Note for Handoffs)
Whenever communication, workflow logs, or agent actions state or imply that something was given, transferred, or handed off to a CSR, Producer, Account Manager, or any specific individual:
- **Mandatory Assigned Task**: The autonomous engine **MUST create and assign an actual Task in EZLynx to that specific individual**. Never post just a passive discussion note when an action or follow-up is expected from a team member.
- **Accountability**: If an agent or workflow says "gave to CSR" or "assigned to Producer", there MUST be a corresponding active task assigned to that person in EZLynx with an appropriate due date and clear action instructions.
- **Signature & Association**: The task must include the policy header (`Policy: #{policy_number} ({line_of_business} - {carrier_name})`), mandatory signature `Robie was here`, and be associated directly to the relevant policy record.

