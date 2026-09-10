# Antigravity handoff — Policy Change Verification on hermes-poc-01

Carlo wants **you (Antigravity)** to drive this work to keep Grok Bot usage down.
Grok Bot (Policy Change Verification) orchestrates and verifies evidence only.

## Server
- Host: `hermes-poc-01` (zone `us-east1-b`, project `streetsmart-hermes-poc`)
- Code: `/opt/renewal-automation-system`
- Voice key already in VM `.env` (`VOICE_AI_API_KEY`, `VOICE_CALLER_ID=+17322986745`)

## Deployed scripts (already on VM)
- `scripts/run_policy_change_verification.py`
- `scripts/carrier_policy_change_caller.py` (**must use `requests`**, not urllib — urllib POST hits Cloudflare 1010)
- Skills: `.agents/skills/carrier-policy-document-retrieval/`, `.agents/skills/ezlynx-policy-change-confirmation/`

## Proven dry-run
```bash
cd /opt/renewal-automation-system
PYTHONPATH=. ./venv/bin/python3 scripts/run_policy_change_verification.py \
  --applicant 149367863 --policy 2021047341 --voice --json
```

## Live call already placed (do not re-dial unless Carlo asks)
- Omega General Construction LLC / `2021047341` / National General `+18883251190`
- Agency code `9010158` (alt `9039647`)
- Bland call_id `a1b50a1a-9e5c-4329-847b-0587dc40a9f5` (~2026-09-06 9:12am ET)
- State: `carrier_followup_dispatched`
- EZLynx note write still OFF

## Your next jobs
1. Poll Bland for call_id `a1b50a1a-9e5c-4329-847b-0587dc40a9f5` status/transcript/recording (use VM key; never paste secrets into chat).
2. Summarize outcome for Carlo (issued? pending? docs emailed to robie@?).
3. If endorsement arrived: draft 3-way QC note (do **not** write EZLynx unless Carlo authorizes `--write-notes`).
4. Own future OPEN Confirmation Queue runs on hermes (batch `--limit N --voice` dry-run by default; `--live-voice` only when Carlo says).
5. Keep GitHub dirty-tree discipline — prefer additive scripts; don't wipe unrelated hermes dirty files.

## National General directory facts
- Phone: +18883251190
- Agency: 9010158 / 9039647
- Portal note in matrices under `.agents/skills/carrier-policy-document-retrieval/references/`

## Hard stops
No bind. No client email for carrier docs. No secrets in chat. No EZLynx mutation without Carlo OK.
