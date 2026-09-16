# Stage 2 — JE-KILL-01 disposable Test proof blockers

## Goal

Carlo: `#334` / Stage 2 browser verification closes only after a green
disposable Test run (`TEST VERIFIED` / `JE-KILL REMOTE VERIFIED`).

Workflow: `.github/workflows/je-kill-test.yml`  
Confirmation: `RUN_JE_KILL_01_ON_HERMES_TEST_01`  
Fixture: `/opt/streetsmart-hermes-test/je-kill/fixture.json`

## What Pawel needs from Carlo (now)

Checked live on `hermes-test-01` on 2026-09-13:

1. **Re-stamp fixture approval** (expired ~14 days; gate is 7 days)

```bash
bash scripts/refresh-je-kill-fixture-approval.sh REAPPROVE_JE_KILL_01_FIXTURE
```

Keeps the existing disposable account/docs/locators; only refreshes
`approved_at` to now, still `approved_by=Carlo Ferrara` /
`approval_scope=JE-KILL-01`.

2. **Authenticate Test EZLynx Chrome**

- Service `robie-ezlynx-browser-test` is up on CDP `127.0.0.1:9222`
- Opening `https://app.ezlynx.com/web/` currently redirects to **Login**
- JE-KILL requires exactly one authenticated EZLynx page tab (not login)

After login/MFA on the Test profile, leave one EZLynx `/web/` tab open.

3. **Tell Pawel “go”**

Then Pawel will:
- ensure Test release SHA matches the workflow commit (deploy-test if needed)
- dispatch `RUN_JE_KILL_01_ON_HERMES_TEST_01`
- report the green run ID

## Round 3 (2026-09-13): both blockers cleared, inventory gate relaxed

Carlo re-stamped the fixture (`approved_at=2026-09-13T15:07:06Z`) and left
one authenticated SSRobie tab on `https://app.ezlynx.com/web/dashboard`.

Remaining trap: Chat job `c282de98` is `RUNNING` with **no lease**
(`lease_owner` empty). Carlo parks it later (step 4), not now. The old
inventory gate refused on any `RUNNING`/`VERIFYING` row, so round 3 would
have died with `JE-KILL REFUSED: 1 active Test Job(s) or lease(s)`.

Now (`scripts/run-je-kill-test-remote.sh` +
`robie_job_engine.je_kill_preflight.job_inventory_report`):

- **blocks** only on rows holding a `lease_owner` (any status) — only a
  lease holder can be driving the shared Test Chrome
- **ignores** unleased `RUNNING`/`VERIFYING` rows, printing them as
  `JE-KILL Test inventory: N unleased RUNNING/VERIFYING Job(s) ignored (no worker lease): c282de98 RUNNING`
- still emits `JE-KILL Test inventory: 0 blocking Jobs/leases` before the
  fixture/EZLynx preflight

Landed on `main` (Stage 2 / #386 lineage). Stage 3 keeps Test release
pointer SHA matched and adds the orphan unleased sweeper — see
`docs/STAGE3_JE_RELIABILITY_HOLD.md`. **Production JE-KILL remains HOLD.**

## Dirty disposable docs (2026-09-14)

A green JE-KILL leaves `JE-KILL-01` on the three fixture documents. The next
`before_action` kill then reconciles to `RECONCILED_APPLIED` without calling
`perform` → `action_attempts: expected 1, got 0`.

Mitigation in `PersistentChromeEzlynxPort.ensure_clean_destination()` (called
from `run_phase` after auth preflight): dismiss EZLynx release-notes overlays,
and if the applied-label locator is present, clear via row **edit** → fixture
label option → **Apply**, then require `Add label` again before spawning the
kill child.

## Non-negotiables

- No PAWIVA / `221398001`
- No Production
- No forging `approved_at` without Carlo’s re-approval command above
