# Stage 3 — Job Engine reliability (infra)

## Status

**Stage 3 infra: CLOSED** (Carlo bar 2026-09-16).

Acceptance bar (verbatim): *merged #465 + Test-deployed + sweeper parks a
seeded unleased RUNNING on Test. Nothing else for infra.*

| Gate | Evidence |
|------|----------|
| PR #465 merged | `0396832684b3090f7113297994ea6cd1182a4525` (2026-09-16T18:03:42Z) |
| Test deploy | [run 35132059530](https://github.com/streetsmart-insurance/robie-hermes/actions/runs/35132059530) success; headSha `0396832684b3090f7113297994ea6cd1182a4525` |
| Live Test pointers | `/opt/streetsmart-hermes-test/releases/0396832684b3/robie-hermes-0396832684b3` (both `current` and `releases/current`) |
| Sweeper proof | Job `1ae4a3cb-fe39-415f-add9-3a93fa57d91f`: seeded unleased `RUNNING` → parked `FAILED` with checkpoint `orphan_unleased_sweep` (`PROOF_OK`, 2026-09-16T18:06:49Z) |
| Test SSRobie password pin | Newest ENABLED `versions/7` in message-runtime + accountability env |
| Inventory gate | Live on Test release (`unleased RUNNING` ignore path in `run-je-kill-test-remote.sh`) |

**Production JE-KILL remains HOLD** until Carlo explicitly green-lights a named
digest after more clean Test disposables (Stage 4+/5). Do not fold Stage 4–6
into this close.

## Owner split

| Lane | Owner |
|------|--------|
| Boxes, GHA, Secret Manager, inventory/preflight gates, orphan sweeper | Pawel (infra) — **Stage 3 closed** |
| FormEntry / Coverages / HITL field work | Muse |
| Stage 4 Test JE-KILL disposables | Pawel (new Fiverr order) |
| Stage 5 Prod JE-KILL | Pawel, only after Carlo written green light; Dusty verifies box |
| Stage 6+ | StreetSmart internal unless Carlo opens a separate order |

## What Stage 3 infra delivered

1. **Inventory gate** — block only lease holders; ignore unleased
   `RUNNING`/`VERIFYING` (c282de98 class).
2. **Orphan unleased sweeper** — park stale unleased actives; log + Chat alert;
   wired into scheduler (`ROBIE_ORPHAN_UNLEASED_SECONDS`, default 3600).
3. **Test SSRobie secrets** — follow newest ENABLED (no stale `versions/1` pin).
4. **JE-KILL / Test preflight** — fail closed: fixture age, CDP AUTHENTICATED,
   lease inventory, release pointer SHA.
5. **Prod JE-KILL Stage 3** — held (intentional; not a missing deliverable).

## Explicitly out of Stage 3 infra (not blockers)

- Coverages FormEntry DOM dump (needs auth + FormEntry URL — Muse/Carlo)
- Fresh HITL email re-probe on current Prod zip
- Session 03:30 cookie A/B test
- Dusty’s SSRobie session repair on `hermes-poc-01`

## Non-negotiables

- No Production JE-KILL from Engineering without Carlo's explicit approval.
- No PAWIVA / `221398001`.
- Never clear a lease under a live worker.
- FormEntry/Coverages/HITL remain Muse's lane.
- Do not fold Stage 4–6 into Stage 3 Fiverr.
