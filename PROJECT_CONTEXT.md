# StreetSmart Insurance - Manual Renewal Automation System
## Project Context & Conversation Continuity State

**Generated Date**: 2026-09-02  
**Active Project Path**: `/Users/carloferrara/.gemini/antigravity/scratch/renewal-automation-system`  
**Linked Conversation ID**: `22acf640-964e-475e-9970-0d7ebcd62ee0`

---

## 1. Executive Summary & Goal
Automate the daily manual renewal lifecycle for StreetSmart Insurance across **EZLynx**, **Carrier Portals**, and **Dual-Inbox Gmail Outreach**.

### Core Tenets:
1. **No Stubs / Real PDFs Only**: All downloaded quotes must be authentic carrier declaration/proposal PDFs uploaded directly to the client's **Documents** tab in EZLynx.
2. **50-Day Expiration Window**: Evaluates policies with `Source = Manual` or non-download carriers expiring within 50 days.
3. **Dual-Inbox Architecture**:
   - `robie@streetsmart.insurance`: Outbound outreach & carrier account logins.
   - `hello@streetsmart.insurance`: Polling for carrier underwriter replies.
4. **Autonomous 2FA**:
   - Carrier portals sending 2FA emails to `robie@streetsmart.insurance` are resolved automatically via Gmail API in ~2 seconds.

---

## 2. Active Operations & Progress

### A. Underwriter Email Outreach (Trinity Underwriters)
Dispatched 5 live outbound renewal requests from `robie@streetsmart.insurance` to `quotes@trinityunderwriters.net` with CC to assigned CSRs & Jake Ferrara:
1. `Marek PKS Transportation Inc` — Pol #`248394-001APD-94344-SSRM APD` (`[RENEWAL-REQ-25]`, Msg ID: `1a064a3841a53d23`)
2. `Marek PKS Transportation Inc` — Pol #`FINFR17078371-94344-SSRM NTL` (`[RENEWAL-REQ-26]`, Msg ID: `1a064a3865c1964a`)
3. `Edwin Lema` — Pol #`A23B8960-78760-SSRM NTL` (`[RENEWAL-REQ-31]`, Msg ID: `1a064a385a8654ed`)
4. `PEPPEP N SON'S TRUCKING LLC` — Pol #`25FIT10B01-MTC-94423-SSRM` (`[RENEWAL-REQ-47]`, Msg ID: `1a064a38661b3e68`)
5. `PEPPEP N SON'S TRUCKING LLC` — Pol #`CM92CT01-APD-94423-SSRM` (`[RENEWAL-REQ-48]`, Msg ID: `1a064a388915486a`)

*Next Action*: System automatically polls every morning and executes follow-up cadences on Days 7, 14, 21, and 28.

### B. Portal Account Provisioning (Nicole)
- Sent onboarding email to **`Nicole@streetsmart.insurance`** requesting Robie user accounts across Coterie, The Hartford, TAPCO, etc.
- Nicole will add credentials to **GCP Secret Manager** (`workspace-inbox-tracker`) and ping Carlo when complete.

### C. Live Portal Crawlers Built
- **Coterie**: `src/portals/carrier_agents.py` (`CoteriePortalCrawler`)
- **The Hartford**: `HartfordPortalCrawler`
- **TAPCO**: `TapcoPortalCrawler`

---

## 3. Key Commands & Running the System

```bash
# Enter project directory
cd /Users/carloferrara/.gemini/antigravity/scratch/renewal-automation-system

# Run today's intake & pipeline
PYTHONPATH=. .venv/bin/python3 -m src.main --run-today

# Run test suite (14 passing tests)
PYTHONPATH=. .venv/bin/pytest tests/
```
