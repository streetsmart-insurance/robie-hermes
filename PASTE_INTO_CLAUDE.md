# StreetSmart Insurance — Manual Renewal Automation System
## Agent Handoff & Quickstart Guide (PASTE_INTO_CLAUDE.md)

**System Codename**: `StreetSmart Manual Renewals`  
**Repository**: `streetsmart-manual-renewals`  
**Target Architecture**: Local macOS / GCP Hermes VM (`hermes-poc-01`)  
**Mandatory Audit Signature**: `ROBIE was here`  

---

## 1. Executive Summary & Goal
Automates the daily manual renewal lifecycle for StreetSmart Insurance across **EZLynx**, **Carrier Portals**, and **Dual-Inbox Gmail Outreach**.

### Core Tenets:
1. **Real Documents Only (No Stubs)**: All retrieved quotes must be authentic carrier declaration/proposal PDFs uploaded directly to the client's **Documents** tab in EZLynx.
2. **50-Day Expiration Window**: Evaluates policies with `Source = Manual` or non-download carriers expiring within 50 days.
3. **Dual-Inbox Architecture**:
   - `robie@streetsmart.insurance`: Outbound outreach & carrier account logins.
   - `hello@streetsmart.insurance`: Polling for carrier underwriter replies.
4. **Autonomous 2FA**:
   - Carrier portals sending 2FA emails to `robie@streetsmart.insurance` are resolved automatically via Gmail API in ~2 seconds.

---

## 2. Active Portal Crawlers & Cadences
- **Coterie**: `src/portals/carrier_agents.py` (`CoteriePortalCrawler`)
- **The Hartford**: `HartfordPortalCrawler`
- **TAPCO**: `TapcoPortalCrawler`
- **Underwriter Email Cadence**: Automatically polls every morning and executes follow-up cadences on Days 7, 14, 21, and 28.

---

## 3. How to Run on This Mac

```bash
# 1. Rebuild virtual environment (if needed on new machine)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Run daily intake & renewal pipeline
PYTHONPATH=. .venv/bin/python3 -m src.main --run-today

# 3. Run test suite
PYTHONPATH=. .venv/bin/pytest tests/
```

---

## 4. Key Files & Structure
- `src/main.py`: Main CLI runner and orchestrator.
- `src/portals/`: Carrier portal crawlers (Coterie, Hartford, TAPCO).
- `src/ezlynx/`: EZLynx SOAP/REST integration and document uploaders.
- `src/gmail/`: Gmail API client for dual-inbox handling and 2FA extraction.
- `PROJECT_CONTEXT.md`: Full architectural context and state tracking.
- `GROKBOT_HANDOFF_CHECKLIST.md`: Detailed transition checklist.
