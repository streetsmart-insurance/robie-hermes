# Test VM EZLynx Account Identity Verification

**Session Date**: 2026-08-31  
**Verification Result**: `VERIFIED`  
**Host**: `hermes-test-01` (`hermes-test-01.c.streetsmart-hermes-poc.internal`)  
**Session Login Status**: `SIGNED_IN` (Restored via `robie_job_engine.ezlynx_session` using Secret Manager references)

---

## 1. Executive Summary & Session Restoration Audit

1. **Session Login Workflow**:
   Executed `robie_job_engine.ezlynx_session` against the persistent Chrome CDP endpoint (`http://127.0.0.1:9222`) using Google Secret Manager secret references:
   - `ROBIE_EZLYNX_USERNAME_SECRET=projects/streetsmart-hermes-poc/secrets/ezlynx-username/versions/3`
   - `ROBIE_EZLYNX_PASSWORD_SECRET=projects/streetsmart-hermes-poc/secrets/ezlynx-password/versions/1`
   - Outcome: `{"ezlynx_session": "SIGNED_IN"}`

2. **Read-Only Account Verification**:
   Navigated to both candidate account URLs on `hermes-test-01` in read-only mode using Playwright over CDP. Zero fill, click, save, or submit actions taken.

---

## 2. Empirical Account Identity Comparison Table

| Target Account ID | Candidate Target URL | Final Navigated URL | Status | Page Title | Visible Account Name / Headings | Account Status / Test Indicator |
| --- | --- | --- | --- | --- | --- | --- |
| **`220485040`** | `https://app.ezlynx.com/web/account/220485040/policies` | `https://app.ezlynx.com/web/account/220485040/policies` | `200 OK` | `Applicant Deleted` | `['Applicant Deleted', 'Applicant Deleted']` | **DELETED** in EZLynx |
| **`220250093`** | `https://app.ezlynx.com/web/account/220250093/policies` | `https://app.ezlynx.com/web/account/220250093/policies` | `200 OK` | `ROBIE Test LLC - Policies` | **`ROBIE Test LLC`** | **ACTIVE** Test Account (Compiled allowlist match) |

---

## 3. Playwright Execution Evidence Log

```json
=== Account 220485040 ===
Target URL: https://app.ezlynx.com/web/account/220485040/policies
Page Title: "Applicant Deleted"
Headings: ["Applicant Deleted", "Applicant Deleted"]
Status: DELETED in EZLynx

=== Account 220250093 ===
Target URL: https://app.ezlynx.com/web/account/220250093/policies
Page Title: "ROBIE Test LLC - Policies"
Headings: ["ROBIE Test LLC", "Policies"]
Status: ACTIVE Test Account
```

---

## 4. Key Findings & Recommendation for Carlo

- **Account `220485040`**: Inactive/Deleted in EZLynx (returns `Applicant Deleted`).
- **Account `220250093`**: Fully active Test account in EZLynx for **`ROBIE Test LLC`**. Matches Robie's compiled business-write allowlist in `robie_job_engine/ezlynx_write_scope.py`.
- **Recommendation**: Training pack specifications referencing `220485040` should be updated to target active account `220250093` (`ROBIE Test LLC`).

---

## 5. Security & Safety Boundary Certifications

- **Code & Allowlist**: Unmodified.
- **Production Isolation**: Zero access to Production (`hermes-poc-01`).
- **Data Protection**: Zero raw passwords, secret payloads, cookies, session tokens, or MFA codes displayed or logged.
