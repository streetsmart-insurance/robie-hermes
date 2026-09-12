# Homeowners APE Static Readiness Review

**Session Date**: 2026-08-31  
**Classification**: Static APE Readiness Review *(Not a live browser observation session)*  
**Target Account**: `220485040`  
**Skill Status**: `DRAFT` (`production_ready: false`, `consequential_writes_enabled: false`)

---

## 1. Executive Summary & Correction of Record

This session is classified strictly as a **Static Homeowners APE Readiness Review**. 

- **Correction of Record**: No active Playwright browser execution, live browser session, screenshot capture, trace generation, or destination readback occurred during this turn.
- **Repository Locators**: Selectors referenced for EZLynx Homeowners field entry originate from existing static definitions in `robie_job_engine/locators/ezlynx_policy.json`.
- **Allowlist Integrity**: No changes were made to Robie's compiled write scope allowlist.

---

## 2. Evaluation Lanes Scorecard

| Evaluation Lane | Status | Rationale |
| --- | --- | --- |
| **`policy_router_understanding`** | `PASS` | Profile routing verified statically against `training-pack.json` (Personal Lines Policy tab, Row 6) and mapped to the `homeowners` profile. |
| **`ambiguity_permission_boundaries`** | `PASS` | Fail-closed rules enforced: Homeowners Full Value treatment remains unresolved and mortgagee billing requires explicit fixture authority. |
| **`browser_behavior`** | `NOT-RUN` | Requires a live Playwright session in Robie Test with real DOM interaction, screenshot evidence, and visual readback. |
| **`assignment_handling`** | `NOT-RUN` | Requires execution against a sanitized APE policy fixture. |
| **`verification_outcome`** | `NOT-RUN` | No browser write, save, or post-save reopen verification was performed. |

---

## 3. Workflow & Boundary Analysis

### Workflow Target
- **Line of Business**: `Homeowners`
- **Source Tab & Row**: `Personal Lines Policy`, Row 6 (`gid=1527186573`)
- **Approved Loom Demonstration**: `https://www.loom.com/share/f05774171d434032811b2e3eb9fd5734`
- **Readiness State**: `READY-FOR-GUIDED-TEST` (Static pack classification)

### Account Allowlist Conflict Documentation
- **Training Pack Target Account**: `220485040` (`Robie TEST-Onboarding`)
- **Compiled Write Allowlist Target**: `220250093` (`robie_job_engine/ezlynx_write_scope.py`)
- **Conflict Details**: Account `220485040` is specified as the training account for `robie-ape-test`, but is intentionally absent from Robie's compiled business-write allowlist (`applicant_is_write_allowed`). Any attempt to run preflight policy setup against account `220485040` returns:
  `applicant 220485040 is not on the compiled EZLynx business-write allowlist`
- **Resolution Strategy**: Per safety rules, the compiled allowlist was **not modified**. Account `220485040` is reserved for read-only training/observation, ensuring zero risk of accidental writes.

---

## 4. Static Field & Locator Mapping Reference

The following locators were verified statically from `robie_job_engine/locators/ezlynx_policy.json`:

- **Add Policy Control**: `#add-policy` (fallback `button:Add policy`)
- **LOB Dropdown**: `#mergeSplitLOB` (fallback label `Line of Business`)
- **Master Carrier**: `#MasterCompany` (fallback label `Master Company`)
- **Writing Company**: `#WritingCompany` (fallback label `Writing Company`)
- **Policy Number**: `#PolicyNumber` (fallback label `Policy Number`)
- **Effective Date**: `#EffectiveDate` (fallback label `Effective Date`)
- **Expiration Date**: `#ExpirationDate` (fallback label `Expiration Date`)
- **Rating State**: `#RatingState` (fallback label `Rating State`)
- **Coverages Tab**: `a[data-target='#coverages-tab']` (fallback tab `Coverages`)

---

## 5. Checkpoints Formulated for Future Dry Runs

1. **`duplicate_check`**: Generate SHA-256 key over `(applicant_id, carrier, policy_number, effective_date, expiration_date, lob)`.
2. **`pre_save_snapshot`**: Verify pre-save DOM state.
3. **`post_save_readback`**: Confirm post-save UI readback.
4. **`reopen_verification`**: Verify policy details upon reopening account.

---

## 6. Skill State & Certification

- **Current Status**: `DRAFT`
- **Production Ready**: `false`
- **Consequential Writes**: `false`
- **Promotion Status**: Not marked as `GUIDED_TEST`, `RUNTIME_CANDIDATE`, or Production ready.
