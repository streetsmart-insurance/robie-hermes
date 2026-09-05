---
name: carrier-portal-engine
description: Autonomous Carrier Portal Crawling, Multi-Inbox 2FA/OTP Interception, Sub-Account Provisioning, and Document Retrieval for StreetSmart Insurance.
---

# Carrier Portal Engine - Skill Guide

## Overview
This skill governs autonomous navigation, authentication, 2FA/OTP interception, and renewal packet document retrieval across StreetSmart Insurance carrier portals. It operates alongside `manual-renewals` and `ezlynx-home-flood-renewal-mortgagee` to retrieve authentic carrier renewal PDFs before falling back to email outreach.

---

## 1. Multi-Inbox 2FA & OTP Interception Architecture

Carriers require multi-factor authentication (MFA) via one-time passcodes (OTP), verification links, or magic links. The system intercepts these in real time across agency inboxes using **Google Workspace Domain-Wide Delegation (DWD)**.

### Monitored Inboxes
1. `carlo@streetsmart.insurance` (Primary Agency Principal / Admin / Fallback OTP Receiver)
2. `robie@streetsmart.insurance` (Dedicated Autonomous Agent / Outreach / Sub-Logins)
3. `hello@streetsmart.insurance` (Central Agency Intake / Inbound Poller)

### Interceptor Engine: `src/email_outreach/otp_interceptor.py`
The `MultiInboxOTPInterceptor` polls all 3 inboxes simultaneously via Service Account Domain-Wide Delegation (`data/credentials/service_key.json`).

```python
from src.email_outreach.otp_interceptor import otp_interceptor

# Wait up to 45 seconds for an OTP code matching a carrier query
result = otp_interceptor.wait_for_otp(
    query="Next Insurance OR ERGO OR verification OR code",
    max_wait_seconds=45,
    poll_interval=2,
    since_offset_seconds=20
)

if result and result.code:
    print(f"Intercepted code {result.code} from {result.inbox}")
```

### Supported Token Formats
- **Standard 6-digit OTPs**: `\b([0-9]{6})\b` (Next Insurance, The Hartford, EZLynx)
- **4-to-8 digit codes with context**: `(?:verification|security|confirmation|passcode|code|otp)\s*(?:is|:)?\s*([0-9]{4,8})\b`
- **Clerk / Magellan Reset Codes**: 6-digit numeric codes in email subjects and bodies
- **PolicyLink Activation Links**: `https://policylink.neptuneflood.com/...`
- **Exponea Redirect Links**: `https://nxi-cdn.exponea.com/...` (Next Insurance auto-redirects)

---

## 2. Carrier Portal Authentication Matrix

| Carrier | Portal URL | Auth Type | Username / Identifier | Password / Credential Source | 2FA / MFA Interception |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Next Insurance (ERGO NEXT)** | `https://agents.nextinsurance.com` | Passwordless Email OTP | `robie@streetsmart.insurance` | `OTP_EMAIL_BASED` | Auto-intercepted from `robie@` via Gmail DWD |
| **Magellan** | `https://app.magellan.insure/login` | Direct Email & Password | `carlo@streetsmart.insurance` | macOS Keychain & Secret Manager (`StreetSmartMagellan2026!#`) | None required on direct login |
| **TAPCO** | `https://www.gotapco.com` | Direct Username & Password | `robieai` | macOS Keychain & Secret Manager (`StreetSmart!Tapco2026`) | Intercepted from `robie@` if prompted |
| **The Hartford** | `https://ebusiness.thehartford.com` | Username & Password + 2FA | `Sstreetsmart` | macOS Keychain & Secret Manager (`the_hartford`) | Auto-intercepted from `carlo@` or `robie@` |
| **Neptune Flood** | `https://neptuneflood.com/agent-hub/#/home` | Agent ID & Password | `FL12652` | macOS Keychain & Secret Manager (`Neptune Insurance`) | Single-use invitation link / Turnstile |
| **CNA Surety** | `https://www.cnasurety.com` | User ID & Password | `FERRS8` | macOS Keychain & Secret Manager (`CNA Surety`) | Auto-intercepted if prompted |
| **Coterie Insurance** | `https://dashboard-v2.coterieinsurance.com` | Email & Password | `carlo@streetsmart.insurance` | macOS Keychain & Secret Manager (`Coterie`) | Auto-intercepted from `carlo@` |
| **Cover Whale** | `https://app.coverwhale.com` | Email & Password | `Jake@streetsmart.insurance` | macOS Keychain & Secret Manager (`Cover Whale`) | Intercepted from `hello@` or `carlo@` |
| **Markel** | `https://www.markelinsurance.com` | Username & Password | `cferrara123` | macOS Keychain & Secret Manager (`Markel`) | Auto-intercepted if prompted |
| **Selective Insurance** | `https://www.selective.com` | Username & Password | `a89110sf` | macOS Keychain & Secret Manager (`Selective Insurance`) | Auto-intercepted if prompted |

---

## 3. Bot Detection & Cloudflare Turnstile Protocol

### Execution Environment Rules:
1. **Never run plain headless Chromium (`headless=True`) on bot-protected portals**:
   - Cloudflare Turnstile (Neptune Flood, Clerk SSO) blocks headless user agents.
   - Google OAuth / SSO rejects headless Chromium instances (`Error 400: invalid_request` / bot detection).
2. **Always use Persistent Browser Contexts on Mac**:
   ```python
   browser = await p.chromium.launch_persistent_context(
       user_data_dir="/tmp/carrier_chrome_profile",
       headless=False,
       args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
   )
   ```
3. **Session Preservation**:
   - Store session cookies in `data/<carrier>_auth_state.json`.
   - Before prompting login, check if existing session cookies remain valid by loading dashboard endpoints directly.

---

## 4. Carrier-Specific Crawling Procedures

### Next Insurance (ERGO NEXT)
1. **Navigation**: Go to `https://agents.nextinsurance.com/authentication`.
2. **Email Entry**: Enter `robie@streetsmart.insurance` into `input[name="agentEmail"]`.
3. **Trigger**: Click `button:has-text("Continue")`.
4. **OTP Catch**: Poll `robie@streetsmart.insurance` using `otp_interceptor.wait_for_otp()`.
5. **OTP Entry**: Focus the verification code input and type `res.code` sequentially with delay (`press_sequentially(res.code, delay=150)`) .
6. **Submit**: Press `Enter` or click `Verify`.
7. **Destination**: Confirms landing on `https://agents.nextinsurance.com/agent-home` or `/dashboard/clients`.
8. **Renewal Document Retrieval**: Search client by policy number or business name -> Open policy details -> Download Renewal Offer / Certificate PDF.

### Magellan Agency Intelligence Portal
1. **Navigation**: Go to `https://app.magellan.insure/login`.
2. **Credential Entry**:
   - Email: `carlo@streetsmart.insurance`
   - Password: `StreetSmartMagellan2026!#`
3. **Submit**: Click `button:has-text("Login")`.
4. **Destination**: `https://app.magellan.insure/dashboard`.
5. **Key Subsystems Available**:
   - `/dashboard/renewals`: Full renewal pipeline and priority renewal queue.
   - `/dashboard/saved-clients`: Active client portfolio.
   - `/search`: Search call audio recordings, transcripts, and customer sentiment across agency phone numbers.
   - `/dashboard/team-performance`: AI Coach insights and CSR responsiveness metrics.

### TAPCO Underwriters
1. **Navigation**: Go to `https://www.gotapco.com`.
2. **Credential Entry**:
   - Username: `robieai`
   - Password: `StreetSmart!Tapco2026`
3. **Document Retrieval**: Policy Inquiry -> Enter policy number -> Retrieve Binder / Policy Decs.

### The Hartford
1. **Navigation**: Go to `https://ebusiness.thehartford.com`.
2. **Credential Entry**: `Sstreetsmart` / password from SecretsManager.
3. **2FA Resolution**: If SMS/Email 2FA prompt appears, call `otp_interceptor.wait_for_otp(query="The Hartford")` and fill code.
4. **Document Retrieval**: Electronic Policy Delivery -> Documents -> Download Declarations PDF.

---

## 5. Standardized File Naming & EZLynx Sync

All renewal documents retrieved from carrier portals must be saved locally and pushed to EZLynx:
1. **Local Download Directory**: `data/downloads/carrier_renewals/`
2. **File Naming Convention**:
   `STREETSMART_{CARRIER_NAME}_{POLICY_NUMBER}_{EXPIRATION_DATE}_RENEWAL.pdf`
   *Example*: `STREETSMART_NEXT_INSURANCE_NX1029384_20261015_RENEWAL.pdf`
3. **EZLynx Client Sync & Mandatory Policy Association**:
   - Upload PDF to EZLynx client Documents tab under folder `Policy Documents` or `Renewals`.
   - Post audit note using `scripts/ezlynx_cli.py note {applicantId} "{NoteText}" --policy-number {Policy#} --lob "{LineOfBusiness}" --carrier "{CarrierName}"`.
   - **MANDATORY Header**: Every note posted MUST begin with:
     `Policy: #{policy_number} ({line_of_business} - {carrier_name})`
   - **MANDATORY Signature**: Every note MUST end with:
     `Robie was here`
   - Verify uploaded document presence via `scripts/ezlynx_cli.py documents {applicantId}`.
   - Update policy renewal status in pipeline tracker to `RENEWAL_READY`.

