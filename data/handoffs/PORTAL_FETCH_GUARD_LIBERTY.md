# Robie portal fetch — GUARD Safe Man + Liberty Pross
Carlo 2026-09-05: Robie on hermes-poc-01 must pull renewal docs from carrier **websites** (not only email).

## Auth
- GUARD: Secret Manager / secrets_mgr service `guard` (or `GUARD`) — username+password present.
- Liberty Mutual broker: secrets_mgr `liberty_mutual` — username+password present.
- Never paste secrets into chat or proof JSON.
- 2FA: use existing email interceptors on robie@ / configured inboxes. Do not log OTP values.

## Targets
### A) Safe Man LLC / InterGUARD (AmGUARD)
- Portal: https://www.guard.com/ (agency portal login)
- Applicant EZLynx: 163863318
- Expiring/prior: R2WC681352 → renewal R2WC771037 already keyed in EZLynx
- Download renewal proposal + billing/premium statement if available
- Save under `/opt/renewal-automation-system/data/downloads/carrier_renewals/` with names like `guard_safeman_portal_<ts>_Proposal.pdf` and `..._Billing.pdf`
- Write proof: `/opt/renewal-automation-system/data/handoffs/portal_fetch_proof.json` with paths, timestamps, success/fail per carrier

### B) Pross Construction LLC / NJCRIB Liberty Assigned Risk
- Portal: https://account.libertymutual.com/broker (DB portal_url)
- Also try agent.libertymutual.com if broker URL redirects
- Applicant: 151445306
- Policy: WC5-33S-B1X2Z3-025
- Expected: WC renewal quote packet (~$8,572, quote 02212061-01, term 10/08/2026–10/08/2027)
- Save as `liberty_pross_portal_<ts>_renewal_packet.pdf`
- Note: a packet already exists at `liberty_pross_renewal_packet.pdf` (Jul 29, 2026 letter). Re-pull from portal to prove Robie path; keep both if hashes differ.

## Rules
- No bind. No payment. No client email.
- Carrier portals only for this handoff (EZLynx filing is separate).
- If login/HITL blocked, stop and write failure reason into portal_fetch_proof.json — do not invent PDFs.
- Prefer CDP/Playwright on hermes-poc-01 under /opt/renewal-automation-system venv.

## Done when
portal_fetch_proof.json lists both carriers with local PDF paths that `pdftotext` shows as real renewal quotes (insured name + policy/quote).
