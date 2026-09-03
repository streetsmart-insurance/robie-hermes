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
