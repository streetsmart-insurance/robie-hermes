# Stage 4 — Test JE-KILL disposables until boringly green

Owner: Pawel (Fiverr). Carlo/StreetSmart may spot-check box evidence.
**No Production JE-KILL** (that is Stage 5, Carlo written green light only).

## Goal

Run disposable JE-KILL-01 proofs on `hermes-test-01` until the lane is
stably green (multiple clean runs; flake fixes land as PRs with regression
tests).

Workflow: `.github/workflows/je-kill-test.yml`  
Confirmation: `RUN_JE_KILL_01_ON_HERMES_TEST_01`  
Fixture: `/opt/streetsmart-hermes-test/je-kill/fixture.json`  
Readiness (read-only): `scripts/je-kill-test-readiness.py`

## Prerequisites (fail closed)

1. Test release pointer SHA matches `main` commit used by the workflow
   (`deploy-test.yml` if drifted).
2. Fixture `approved_at` within 7 days (`approved_by=Carlo Ferrara`,
   `approval_scope=JE-KILL-01`). Re-stamp only with Carlo’s command:
   `bash scripts/refresh-je-kill-fixture-approval.sh REAPPROVE_JE_KILL_01_FIXTURE`
3. CDP AUTHENTICATED: exactly one EZLynx `app.ezlynx.com/web/` tab (not login,
   not about:blank) on `robie-ezlynx-browser-test`.
4. No lease-holding Jobs on Test (unleased RUNNING/VERIFYING are ignored by
   inventory gate / parked by Stage 3 sweeper).

## Current blocker (2026-09-17)

Checked live on `hermes-test-01`:

| Check | Result |
|-------|--------|
| Test pointer | `dcfda9850ae1` (matched `main` at check time) |
| Fixture age | ~3.2 days — OK |
| Browser service | `robie-ezlynx-browser-test` active |
| CDP | **only `about:blank`** — not AUTHENTICATED |
| Login bootstrap | `ROBIE_MAILBOX_AUTH_REQUIRED` — Test has no
  `/opt/streetsmart-hermes-test/.hermes/robie_google_token.json` and no DWD
  mailbox env for MFA |
| Leases | none blocking |

**Need from Carlo/Dusty:** authenticate SSRobie on Test Chrome (or provision
Test Gmail OAuth / keyless DWD for MFA). Leave one `app.ezlynx.com/web/` tab
open. Then Pawel dispatches JE-KILL and files flake PRs.

## Run loop

1. `PYTHONPATH=releases/current python scripts/je-kill-test-readiness.py --expected-sha <12char>`
2. If OK: `gh workflow run je-kill-test.yml --ref main -f confirmation=RUN_JE_KILL_01_ON_HERMES_TEST_01`
3. Record run ID + SHA + `JE-KILL REMOTE VERIFIED` / failure line
4. If dirty labels leave `action_attempts=0`: ensure_clean path (Stage 2) —
   regression tests required for any new flake

## Out of scope

- Production JE-KILL (Stage 5)
- FormEntry / Coverages / HITL fields (Muse / Stage 6+)
- Forging fixture `approved_at`

## Evidence log

| When (UTC) | Run / proof | SHA | Result |
|------------|-------------|-----|--------|
| 2026-09-17 | readiness SSH | `dcfda9850ae1` | **BLOCKED** — CDP about:blank / mailbox auth |
