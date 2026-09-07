# Manual Renewal Shell Keying — Operator Notes

**Owner (GitHub / PR review):** Alvin  
**Approved:** Carlo Ferrara + Dusty (full Manual Renewals speed project)  
**Live path:** hermes-poc-01 `/opt/renewal-automation-system` → `src/ezlynx/policy_renewer.py`  
**Do not edit:** robie-hermes  

Production is **never** the first test. Prove the job on **hermes-test-01** (`--env test`) before any Production zip.

## Who does what

| Tool | Use |
| :--- | :--- |
| **Hardened server recipe** (`policy_renewer` / `run_manual_renewal.py`) | Routine shell keys |
| **Antigravity / Gemini** | Explore + HITL only — never a chain of SSH/SCP one-liners per step |

## One command (one CDP session)

On the VM, with Chrome already on CDP (`9222`):

```bash
PYTHONPATH=. .venv/bin/python3 scripts/run_manual_renewal.py \
  --env test \
  --dry-run \
  --applicant-id <ID> \
  --policy-id <POLICY_ID> \
  --policy-number <NUM> \
  --lob "Workers comp" \
  --carrier "Associated Specialty Insurance" \
  --premium 1200.00 \
  --effective-date 2026-10-15 \
  --expiration-date 2027-10-15 \
  --writing-company "Associated Specialty" \
  --discussion-title "Renewal Manual Workers comp | PWC1239278 Associated Specialty Insurance" \
  --upload /path/to/authentic.pdf \
  --proof-json data/renewal_proofs/example.json
```

Same module: `PYTHONPATH=. python3 -m src.ezlynx.policy_renewer …`

The job, in **one** connected Chrome session:

1. Verify pending RWL shells on policy-summary **History** / in-page PolicyAPI  
   (Classic `get_applicant_policies` is **not** sufficient — it omits pending RWL.)
2. If a pending RWL already exists for the **same term + premium** → `already_in`, **STOP**. No second shell. Skip docs and notes.
3. Optional upload — skip stubs **&lt; 10KB**.
4. Note only on the **exact original titled** discussion (caller `--discussion-title` or the policy-numbered card). Never untitled. Never `Workers Compensation Renewal` when `Renewal Manual Workers comp \| PWC…` exists.
5. Fill required fields (Writing Company mandatory). Producer/CSR = **Carlo Ferrara**, never Robie.
6. Click **`#RenewPolicyBtn`** ("Renew Policy"). Never "Renew & Edit Policy" (that is FormEntry, not this job).
7. Write proof JSON. **No bind. No money. No client email. Never Add Policy from Quote ID.**

### Production live gate

```bash
# Only after hermes-test-01 proof
--env prod --allow-live Carlo
```

Applicant must be on the production allow-list (`DEFAULT_PRODUCTION_LIVE_APPLICANTS` or `MANUAL_RENEWAL_LIVE_APPLICANTS=id1,id2`).

## 2026-09-06 symptoms this recipe stops

- **Maier Solar IM:** retries keyed ~5 pending RWL shells because Classic said “no policy.” UI History is the proof.
- **Yes We Do WC:** blank Writing Company bounced, retry duplicated the shell; notes landed on `Workers Compensation Renewal` instead of the original titled card.
- Stub PDF re-uploads, wrong button, SSH/SCP micro-scripts.

## Follow-up (design only): long-lived renewer / Directory

A small systemd unit on the VM is **not** in this PR (session ownership + 2FA + crash recovery need HITL). Sketch:

```
ezlynx-renewer.service
  - keeps one Chrome CDP (9222) warm
  - unix socket /run/ezlynx-renewer.sock (localhost only)
  - accepts one RenewalJobSpec JSON at a time (serialize; never two keys share a session)
  - writes proof JSON to data/renewal_proofs/
  - operators/callers: scripts/run_manual_renewal.py (same flags) or a one-shot curl to the socket
```

Until that exists, run the one-shot CLI against the already-open CDP Chrome. Do not invent per-step SCP wrappers.
