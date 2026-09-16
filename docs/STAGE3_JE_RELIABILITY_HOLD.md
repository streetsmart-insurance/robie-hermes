# Stage 3 — Job Engine reliability hold (Production JE-KILL)

Status: **HOLD Production JE-KILL** until more clean Test disposables land,
unless Carlo explicitly green-lights the exact QA-certified digest.

## Owner split

| Lane | Owner |
|------|--------|
| Boxes, GHA, Secret Manager, inventory/preflight gates, orphan sweeper | Pawel (infra) |
| FormEntry / Coverages / HITL field work | Muse |

## Infra checklist (this release)

1. **Inventory gate (Option 1)** — block only lease holders; ignore unleased
   `RUNNING`/`VERIFYING` (c282de98 class). Code on `main` via Stage 2
   preflight; keep Test release pointer SHA matched to the workflow commit.
2. **Orphan unleased sweeper** — park stale `RUNNING`/`VERIFYING` with empty
   `lease_owner`, log, Chat ops alert + per-thread Chat notify for Chat jobs.
   Wired into the scheduler tick (`ROBIE_ORPHAN_UNLEASED_SECONDS`, default 3600).
3. **Test SSRobie secrets** — stop trusting stale `ezlynx-password` pins
   (`versions/1` in accountability env). Bootstrap + credential loader follow
   newest ENABLED. Operator script:
   `scripts/rewrite-test-login-secret-pins.sh` on `hermes-test-01`.
4. **JE-KILL / Test preflight** — fail closed with a clear line for fixture age,
   CDP AUTHENTICATED, lease inventory, and release pointer SHA.
5. **Production JE-KILL Stage 3** — **do not run** until ≥2 additional clean
   Test disposable proofs after Stage 2, unless Carlo says go for a named digest.

## Non-negotiables

- No Production JE-KILL from Engineering without Carlo's explicit approval.
- No PAWIVA / `221398001`.
- Never clear a lease under a live worker.
- FormEntry/Coverages/HITL remain Muse's lane.
