# Hermes handoff — Friday 28 Aug 2026 after 2:32pm ET

Start here if you are ChatGPT, Claude, Cursor, Grok, Jake, or another
operator continuing StreetSmart Hermes. The Ascend API implementation is
merged and live on Test, but API execution is intentionally disabled until a
sandbox credential is stored in an isolated Test secret.

Detailed Ascend handoff: [docs/ascend-api-handoff-2026-08-28.md](docs/ascend-api-handoff-2026-08-28.md)

---

## Non-negotiable safety boundary

1. Never use PAWIVA or account `221398001` for a test.
2. Do not make a Production Ascend program the first API write test.
3. Stop before Save, email, checkout, payment, or bind in browser audits.
4. Never paste an Ascend API key, password, or email 2SV code into chat,
   commands, payloads, logs, screenshots, or GitHub.
5. Ascend login is Robie (`robie@streetsmart.insurance` + email 2SV). A human
   enters the 2SV code.
6. Test/Production Secret Manager isolation is not proven. Treat secrets as
   possibly shared until an operator proves otherwise.
7. Do not deploy to Production (`hermes-poc-01`) until the API path records a
   successful sandbox create plus independent read-back.

---

## Current GitHub state

GitHub `main`: `4df60a0955f3500bdf51de2e81526dbe15a5a802`.

Merged work:
- PR 59 — guarded `ascend.create_program` API worker, verifier, planning CLI, and documentation.
- PR 60 — mark the synthetic missing-PDF release replay as Test.
- PR 61 — scope release logic subprocesses to Test.
- PR 62 — isolate release tests from the VM's live Drive-synced skill snapshot.

In-flight PRs:
- **PR 57 (`feat/playwright-hardening-punchlist`)**: Playwright hardening suite (tracing, locator registry, route-interception fixtures, split retry policy, visual snapshot diffing, idle-gated Chrome refresh).

---

## Proven Test deployment

Host: `hermes-test-01.c.streetsmart-hermes-poc.internal`

- Release: `4df60a0955f3`
- Archive SHA-256: `9c9f27e754beff6c84d9c616f861d49406e8ae0a413b61295ec14e22081ce308`
- `/opt/streetsmart-hermes-test/current` points to `/opt/streetsmart-hermes-test/releases/4df60a0955f3/robie-hermes-4df60a0955f3`.
- `/opt/streetsmart-hermes-test/releases/current` points to the same release.
- Actual gateway service: `robie-gateway.service`.
- Pointer flip: `2026-08-28T18:32:05.061477+00:00`.
- Gateway `ActiveEnterTimestamp`: `2026-08-28T18:32:18+00:00`.
- Official install proof: `done=true`, `live=true`, `pointer_only=false`.
- Install-proof Job: `be57b415-58bd-4e3c-96c1-6f907a227f64`.
- All deployed `test_ascend_api.py` tests passed on the Test VM.

---

## Playwright Hardening Suite Status (PR 57)

- **PW-01 Tracing**: `PlaywrightTraceManager` wraps CDP execution (`deploy/hermes/tools/playwright_tool.py`) with `ROBIE_JOB_ID` and writes `playwright-trace.zip` to the artifact folder.
- **PW-02 Locator Registry**: `LocatorRegistry` with declarative JSON schemas (`locators/ascend.json`, `locators/ezlynx.json`), queried first in `_find_labeled_control`.
- **PW-03 Retry Policy**: Explicit failure classification in `robie_job_engine/retry_policy.py` wired into `engine.py`: `LOCATOR_AMBIGUITY`, `EMPTY_OR_CORRUPT_ARTIFACT`, `AUTH_CHALLENGE` are hard-gated non-retryable; `TRANSIENT_NETWORK` is retryable; other errors fall back to worker flag.
- **PW-04 Route Interception Fixtures**: 5 offline Playwright fixture tests in `tests/test_playwright_route_fixtures.py`.
- **PW-05 Snapshot Diffing**: `SnapshotDiffManager` and `scripts/run-snapshot-diff.py` to detect UI drift before live jobs run.
- **PW-06 Zero-Job-Aware Chrome Refresh**: `scripts/chrome_refresh_if_idle.py` wired into `deploy/systemd/robie-chrome-refresh.service`.
