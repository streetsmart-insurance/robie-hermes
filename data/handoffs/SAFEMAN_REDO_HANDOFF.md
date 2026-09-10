# Safe Man LLC — REDO (Carlo authorized 2026-09-05)

Applicant: **163863318** (The Safe Man LLC)
SSRobie session only. Assigned Producer/CSR = **Carlo Ferrara** (never Robie).
**No bind. No money. No client email.**

## What failed (Dusty verified live on hermes-poc-01)

1. **Renewal NOT keyed** with the new policy number.
   - Portal/offer number: **R2WC771037** (term 10/16/2026–10/16/2027, AmGUARD / InterGUARD)
   - EZLynx still only has active WC **R2WC681352** (exp 10/16/2026)
   - Prior term R2WC558052 Inactive — do not touch

2. **Docs partially uploaded, incomplete vs agency standard**
   - Present: `WC GUARD Renewal Offer 26-27.pdf` + `WC GUARD Billing Statement 26-27.pdf`
   - Linked to old policy id for R2WC681352
   - Classic API cannot see folder/label; upload did NOT prove folder `Renewal Offers/Declarations` + label `Renewal Offer`
   - Source PDFs on VM:
     - `/opt/renewal-automation-system/data/downloads/carrier_renewals/guard_safeman_Proposal_-_08262026_-_08262026.pdf`
     - `/opt/renewal-automation-system/data/downloads/carrier_renewals/guard_safeman_Premium_Billing_Statements_-_08262026_-_08262026.pdf`

3. **Discussion notes NEVER synced to EZLynx**
   - Robie DB title intended: `Manual Workers comp Renewal`
   - All audit_note_logs rows: `synced_to_ezlynx = 0`
   - Portal discussions pull: empty

Kodomo screenshot was ONLY Carlo’s quality bar example (folder + label + policy # + name). Not a Kodomo/Safe Man mix-up.

## Redo — required end state (verify all)

A. **Key renewal policy** in EZLynx for applicant 163863318:
   - Policy #: **R2WC771037**
   - LOB: Workers Comp
   - Carrier: InterGUARD / AmGUARD (Berkshire Hathaway GUARD)
   - Eff 10/16/2026 Exp 10/16/2027
   - Premium ~$24,103 (from offer)
   - Transaction = Renewal of R2WC681352 (use alias — prior OR renewal # is same account)
   - Producer/CSR = Carlo Ferrara
   - **Do not bind**

B. **Re-file documents** via `EZLynxDocumentUploader` (or fix UI to match):
   - Folder: **Renewal Offers/Declarations**
   - Label: **Renewal Offer** (and billing appropriately)
   - Names per agency: e.g. `R2WC771037 Renewal Offer.pdf` and billing statement with policy #
   - Associate to policy **R2WC771037** (not only 681352)
   - Prefer `upload_safeman_doc.py` pattern / `src/ezlynx/document_uploader.py`
   - Safe to leave old wrongly-named files; better to re-upload correct named ones than delete

C. **Discussion note** on titled discussion (create if missing):
   - Title: **Manual Workers comp Renewal** (or existing matching Renewal discussion — never untitled)
   - Include: prior R2WC681352 → renewal R2WC771037, premium, term, docs uploaded, folder, **Robie was here**
   - Must actually land in EZLynx (`synced_to_ezlynx=1` or visible in Activity/Discussions)

D. **Update renewals.db** policy_number_aliases: R2WC681352 ↔ R2WC771037; status after offer filed.

E. **Proof for Dusty** (write to `/opt/renewal-automation-system/data/handoffs/safeman_redo_proof.json`):
   - policies list showing R2WC771037 Active
   - document rows: Description, PolicyId, folder/label evidence (screenshot OK)
   - discussion note id / screenshot
   - git SHA / script used

## Out of scope this pass
- Pross Construction (separate redo later)
- Bind / payment / client email
- PAWIVA / Ascend
