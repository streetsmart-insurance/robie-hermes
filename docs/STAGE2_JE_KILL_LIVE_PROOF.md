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

## Non-negotiables

- No PAWIVA / `221398001`
- No Production
- No forging `approved_at` without Carlo’s re-approval command above
