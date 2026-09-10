# StreetSmart Insurance — Policy Change Confirmation & LOB Master Handoff
## Agent Handoff & Quickstart Guide (PASTE_INTO_CLAUDE.md)

**System Codename**: `StreetSmart Policy Changes`  
**Repository**: `streetsmart-policy-changes`  
**Environment**: Production / Hermes Automation Engine  
**Mandatory Audit Signature**: `ROBIE was here`  

---

## 1. Executive Summary & Architecture
This system packages the complete **Policy Change Confirmation & Verification Engine**, the upstream **Document Retrieval Handoff Protocol**, and the field-level operating rules extracted from all **18 Line of Business (LOB) Loom video walkthroughs**.

When carrier documents (revised declarations, endorsements, invoices) are retrieved from carrier portals, email queues, or autonomous phone calls, they are passed directly into the **Three-Way Verification Engine** for automated cross-checking, EZLynx transaction validation, and quality-controlled task resolution.

```
┌────────────────────────────────────────────────────────┐
│     Upstream: Carrier Policy Document Retrieval        │
│  (Carrier Portals, IVANS eDocs, Email, Voice AI Phone) │
└──────────────────────────┬─────────────────────────────┘
                           │ Documents Retrieved & Filed
                           ▼
┌────────────────────────────────────────────────────────┐
│     Handoff Interface (JSON Payload / CLI Command)     │
│ Account ID, Policy #, LOB, Document URI, Original Req  │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│   Downstream: Policy Change Confirmation & Audit QC    │
│  (scripts/policy_change_verification_pipeline.py)      │
│  - 3-Way Match: Request vs Carrier Dec vs EZLynx Rec   │
│  - Line of Business Specialized Underwriting Audits    │
│  - Posts QC Audit Note ending with 'ROBIE was here'    │
│  - Closes Task if 0 Exceptions / Holds if Discrepancy  │
└────────────────────────────────────────────────────────┘
```

---

## 2. Verification CLI Execution

```bash
# Execute verification pipeline on an endorsement
python3 scripts/policy_change_verification_pipeline.py \
  --account-id <EZLYNX_ACCOUNT_ID> \
  --policy-number <POLICY_NUMBER> \
  --lob "<LINE_OF_BUSINESS>" \
  --document-path "/path/to/carrier_endorsement.pdf" \
  --request-text "<ORIGINAL_CLIENT_REQUEST>" \
  --post-note
```

---

## 3. Codified Lines of Business (LOB) Directory
Reference guides and operational click paths are maintained in:
`.agents/skills/ezlynx-policy-change-confirmation/references/lobs/`:
- `commercial_auto.md`
- `commercial_general_liability.md`
- `commercial_package_bop.md`
- `commercial_umbrella_excess.md`
- `workers_compensation.md`
- `commercial_inland_marine.md`
- `commercial_errors_and_omissions.md`
- `commercial_garage_and_dealers.md`
- `bonds_and_surety.md`
- `crime_and_fidelity.md`
- `personal_lines.md`

---

## 4. Key Files & Structure
- `scripts/policy_change_verification_pipeline.py`: Main 3-way match audit engine.
- `scripts/carrier_policy_change_caller.py`: Autonomous carrier voice caller.
- `HANDOFF.md`: Full policy change verification specification.
