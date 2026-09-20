# Stage 4 — Test JE-KILL disposables until boringly green

Owner: Pawel (Fiverr). Verifier: Dusty (box evidence). Approver: Carlo.
**No Production JE-KILL** (Stage 5 — Carlo written green light only).

## Goal

Make Test JE-KILL boringly honest on `hermes-test-01`: predictable
kill/park/fail/resume, every run ends with a real `job_debug_dump`, and Test
SSRobie stays AUTHENTICATED on CDP `:9222` without Dusty re-authing every few
hours.

## What “Stage 4 done” looks like (one paragraph)

Test JE-KILL on hermes-test-01 is green: disposables leave real
`job_debug_dump` receipts (phase DB auto-resolved; `ensure_clean_destination`
STATUS=ok when clean succeeds), kill/park/fail/resume matches jobs.db, and
SSRobie stays on `app.ezlynx.com/web/` for a documented window (≥4h or a full
5-cycle) without repeated Dusty login rescue. Prod JE-KILL remains HOLD.

## Prerequisites (fail closed)

1. Test release pointer SHA matches the workflow commit (or run with
   `EXPECTED_SHA=<live>` for overlay proofs — do not use release button for
   Stage 4 acceptance).
2. Fixture `approved_at` within 7 days (`approved_by=Carlo Ferrara`,
   `approval_scope=JE-KILL-01`).
3. CDP AUTHENTICATED: exactly one EZLynx `app.ezlynx.com/web/` tab (not login,
   not about:blank) on `robie-ezlynx-browser-test`.
4. No lease-holding Jobs on Test.

## How to check CDP auth

```bash
curl -fsS http://127.0.0.1:9222/json/list | python3 -c "
import sys,json
pages=[t for t in json.load(sys.stdin) if t.get('type')=='page']
ez=[t for t in pages if 'ezlynx.com' in str(t.get('url') or '').lower()]
print('count', len(ez))
print('title', ez[0].get('title') if ez else None)
print('url', ez[0].get('url') if ez else None)
u=(ez[0].get('url') or '').lower() if len(ez)==1 else ''
ok=len(ez)==1 and '/web/' in u and 'login' not in u
print('CDP_AUTHENTICATED', ok)
"
# or:
PYTHONPATH=/opt/streetsmart-hermes-test/ops:/opt/streetsmart-hermes-test/current \
  /opt/streetsmart-hermes-test/venv/bin/python -m robie_job_engine.test_ssrobie_keepalive --no-warm --json
```

If login / blank → **ping Dusty**. Do not start a disposable. Do **not**
`systemctl restart robie-ezlynx-browser-test` (restart → `about:blank`).

## How to run one disposable + dump

1. Confirm CDP AUTHENTICATED (above).
2. Run JE-KILL (GHA `je-kill-test.yml` when SHA matches, or box runner with
   `EXPECTED_SHA=<live 12-char>`). Overlay with ensure_clean + dump fixes if
   not yet in the live release:
   `PYTHONPATH=/var/tmp/stage4-dump-fix/pkg:$current`.
3. Dump (auto-finds phase DB if pointed at main jobs.db):

```bash
PYTHONPATH=/var/tmp/stage4-dump-fix/pkg:/opt/streetsmart-hermes-test/current \
  /opt/streetsmart-hermes-test/venv/bin/python -m robie_job_engine.job_debug_dump \
  <job-id> --db /opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db
```

Expect `DB_SOURCE=je-kill-phase` and
`TOOL=ensure_clean_destination STATUS=ok` on a clean run.

## Root cause — CDP login / about:blank drops

| Cause | What happens | Mitigation |
|-------|----------------|------------|
| Chrome starts / restarts on `about:blank` | Authenticated tab gone; cookies may remain | Keep-alive navigates blank → dashboard; **never restart** Chrome to “fix” login |
| No Test session timer (Prod has `robie-ezlynx-session.timer`) | Idle EZLynx cookies expire → next nav is login | `robie-ezlynx-keepalive-test.timer` every 20m |
| Test MFA bootstrap blocked (`ROBIE_MAILBOX_AUTH_REQUIRED`) | Cannot auto re-login | Dusty manual re-auth; provision Test Gmail token/DWD separately |
| Daily Chrome refresh (Prod) | Intentional recycle | Test: prefer keep-alive over restart |

## Durability change (Test only)

- Code: `robie_job_engine/test_ssrobie_keepalive.py`
- Units: `deploy/systemd/robie-ezlynx-keepalive-test.{service,timer}`
- Installed under `/opt/streetsmart-hermes-test/ops/` + enabled timer on
  hermes-test-01 (no Prod, no release button).

## ensure_clean_destination

Already-labeled fixture docs must not 0-match on “Add label”. Settle via
`_wait_row_label_state` (applied text **or** row `edit` with Add gone → clear;
Add present → clean). Successful clean stamps `playwright_exec`
`ensure_clean_destination` / `ok`.

## Evidence log

| When (UTC) | Proof | Result |
|------------|-------|--------|
| 2026-09-18 | smoke | FAIL — Add-label 0-match; dump empty PENDING (pre-fix) |
| 2026-09-18 | dump fix | phase DB resolve + LAST_ERROR/playwright_exec |
| 2026-09-18 | ensure_clean fix | unit tests; CDP then login — no disposable |
| 2026-09-20T09:06Z | `je-kill-01-20260920T090648Z-5af7c640` | **PASS** — TEST VERIFIED; dump job `bd68f886-…` shows `ensure_clean_destination STATUS=ok` |
| 2026-09-20 | keepalive timer | installed on Test (soak / 5-cycle pending Carlo OK) |

## Out of scope

- Production JE-KILL (Stage 5)
- Bland / voice / FormEntry / HITL product
- Merges / release button / Prod deploy

## Stop for Carlo

**Before 5-cycle streak:** need written OK. Acceptance B (ensure_clean ok
dump) is met. Acceptance A (durable ≥4h / full streak) needs soak or the
5-cycle itself after OK. Prod remains HOLD.
