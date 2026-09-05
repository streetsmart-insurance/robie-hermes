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
- Run test suite: `PYTHONPATH=. .venv/bin/pytest tests/`

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
