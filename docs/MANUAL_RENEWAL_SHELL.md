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

On the VM, with Chrome already on CDP (`9222`) **and SSRobie already logged into an EZLynx dashboard tab**:

**SSRobie CDP session:** If the live tab on `:9222` is EZLynx Login / `auth/account/login` / forcedOff, the job **BLOCKED**s immediately (HITL + proof JSON). Cookie `storage_state` and Classic API “connected/active” are **not** proof. A human must re-login SSRobie on hermes CDP `:9222`. **Bots must not password-reset.** One live-page check, then stop — no attach-retry loop.

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
3. Optional **one-shot firmed-quote / Renewal Offer PDF fetch** (`--document-id` / `--fetch-firmed-quote`) — see below.
4. Optional upload — skip stubs **&lt; 10KB**. **Doc-type gate:** only a true renewal offer / declaration / firmed renewal quote may be filed as Renewal Offer. Reject Application / Bound Quote Application / Renewal Application prints (e.g. Benli `QHONJ2026080215`) even if someone named the file Renewal Offer. If the portal only has a Bound Quote / Application, stage it as an application artifact and HITL for the real offer — do **not** COMPLETE as Renewal Offer filed. When the PDF *is* a true offer: apply label **Renewal Offer** **and** place it in the Documents folder named **Renewal Offer** (create that folder if missing). Do not leave a renewal offer only under a bare policy-number folder.
5. Notes (LOB-agnostic). Prefer the existing titled discussion that matches the LOB renewal workflow for **that** LOB — `{LOB} Renewal` (e.g. `Homeowners Renewal`, `Commercial Auto Renewal`, `Workers Compensation Renewal`, `BOP Renewal`) and close variants such as `{LOB} Renewal Offer`. **Only** create/use `Manual {LOB} Renewal` and `Renewal Update {LOB}` when no matching existing LOB renewal discussion exists. Never untitled. Never Email Automation / Automation Center (Paulette HO note `1123385828`). Every note ends with `Robie was here`. Before COMPLETE, verify `discussionId` + exact title on the card(s) actually used.
6. **COMPLETE gate** (docs-only is **not** done). All required:
   - Firmed PDF is a **true** renewal offer / declaration / firmed quote (not Application / Bound Quote), uploaded with label **Renewal Offer** into the **Renewal Offer** folder
   - PDF/extract premium matches keyed shell premium (PR 25 extract or `--pdf-premium`; do not invent). **Premium = Policy Total / Total Annual / Grand Total from the PDF — never a Coverage A / Dwelling (or other single coverage-line) premium when a higher Policy Total exists.** If keyed RWL ≠ Policy Total (small tolerance), do not COMPLETE; surface the mismatch.
   - Exactly **one** pending RWL (`bound=false`)
   - Verified note on the existing `{LOB} Renewal` card **or**, if none exists, verified notes on both `Manual {LOB} Renewal` and `Renewal Update {LOB}`
   - `bound=false`
   Any miss → `PARTIAL` / `BLOCKED`, never `COMPLETE` / `SUCCESS`.
7. Fill required fields (Writing Company mandatory). Producer/CSR = **Carlo Ferrara**, never Robie.
8. Click **`#RenewPolicyBtn`** ("Renew Policy"). Never "Renew & Edit Policy" (that is FormEntry, not this job).
9. Write proof JSON. **No bind. No money. No client email. Never Add Policy from Quote ID.**

### Firmed-quote PDF (Paulette Fagone / 2026-09-07)

Once the EZLynx document id is known (e.g. `675732963` — `Fagone - J&J Home Quote Proposal (Firmed).pdf`):

- Prefer Classic `GET /document/{id}` or the known-good portal path **`/Download/675732963`**.
- **Strip the leading `A`** (and similar type prefixes). `/Download/A675732963` returns **0 bytes**.
- Accept only a real PDF (`%PDF` magic + **≥10KB** / 10240 bytes). On 0-byte / invalid: **one** retry with the corrected URL, then **HITL / Antigravity grab**.
- **Do not** burn 20 minutes on Preview / RadPdf page images + OCR.
- Premium extract runs from that PDF only. If the PDF has no premium, **stop** — do not invent one.
- **Policy Total rule:** key and COMPLETE on the labeled Policy Total / Total Annual Premium / Grand Total (largest clearly labeled total). Do not take the first `$` under Coverage Section I / Coverage A / Dwelling when a Policy Total is on the page (Benli HONJ2025100027-26 Hyundai: Coverage A $1,116 ≠ Policy Total $1,348).

```bash
PYTHONPATH=. python3 scripts/run_manual_renewal.py --env test --dry-run \
  --applicant-id 196126698 \
  --document-id A675732963 \
  --fetch-firmed-quote
```

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

## How to run tests

Paulette HO resolver + COMPLETE gate, Login fail-fast, shell keying, and one-shot firmed-quote download:

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_manual_renewal_gate.py tests/test_ezlynx_discussions.py tests/test_policy_renewer.py tests/test_document_uploader.py tests/test_cdp_session_preflight.py tests/test_document_downloader.py -v
```

Covers: existing `{LOB} Renewal` preferred over Manual/Update (HO + other LOBs); Renewal Offer folder selected/created (not a policy-number folder); Application / Bound Quote prints rejected as Renewal Offer; Email Automation / Automation Center rejected; missing `Manual {LOB} Renewal` is created only when no existing LOB renewal card exists (never untitled); COMPLETE fails when the required note is missing or the job is docs-only; Login wall still `blocked`; 0-byte / `A`-prefix download fail-fast.
