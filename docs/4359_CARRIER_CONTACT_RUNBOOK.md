# RUNBOOK — 4359 Phase 2: carrier-contact worker

Phase 2 of the 4359 program. For open policy changes the CSR hasn't
progressed 5+ days after the phase-1 nag, this worker checks EZLynx for an
already-downloaded endorsement FIRST, then emails the carrier's
policy-change address (email-first), or queues the change for manual action
/ directory fill-in.

**Grace period: 5 days** (`GRACE_DAYS`). Carlo approved 5 (was 7) on
2026-09-27: phase-3 reply tracking suppresses engaged CSRs, so the
shorter window only ever bites on fully-silent CSRs.

**Status: code + tests + PR only. NOT merged, NOT deployed. Dry-run only
until Carlo reviews the dry-run proof and approves go-live.**

## What it does per run

1. Reads the phase-1 sent store (`sent.json`) for nag dates — only changes
   nagged 5+ days ago (`GRACE_DAYS`) with no CSR progress are eligible.
2. Skips anything phase 3 owns (`docs_claimed`, `confirmed`, `discrepancy`,
   `needs_human` — not even the DocumentApi check runs for those).
3. If the CSR is engaged (`in_progress`), runs the endorsement check only
   and never emails.
4. **Endorsement-landed check (DocumentApi, read-only) BEFORE any carrier
   contact.** If an endorsement naming the policy is already in EZLynx, the
   change is recorded as `endorsement_found` and handed to phase 3 — the
   carrier is never bothered.
5. Resolves `Master Company` against the curated routing table
   (`robie_job_engine/data/carrier_policy_change_routes.json`):
   - `ok` → email the policy-change address from
     `robie@streetsmart.insurance` (dedup: no re-contact within
     `RECONTACT_DAYS` = 14 days).
   - `manual` (portal/phone/fax-only or ambiguous) → manual-action queue
     for an agent. Never emailed.
   - `missing` or unresolvable → `awaiting-directory` queue. Never
     emailed, never guessed.
6. Records carrier state (`carrier_contacted`, `awaiting_carrier`,
   `endorsement_found`) with timestamps/history in `carrier_contact.json`
   and cross-posts history events to phase 3's FollowupStore (never changes
   phase 3's status; never marks anything confirmed).

Fail-closed: stale/missing 4359 report, DocumentApi outage, or a bad
created date holds the affected rows — no email goes out unless that row's
own endorsement check succeeded.

Dry-run (default): sends nothing, persists nothing — not even in-memory
state on the injected follow-up store.

## Routing table

Source: the 2026-09-27 carrier-directory sweep (206 records). Statuses:

| status | meaning | count (2026-09-27) |
|---|---|---|
| `ok` | safe to email | 79 |
| `manual` | directory route needs an agent (portal/phone/fax-only, ambiguous) | 9 |
| `missing` | no actionable route — Nicole's fill-in queue | 118 |

The sweep counted 103 present / 103 missing under a looser bar (any
route-keyword row with an email/phone/URL/annotation, including
heuristic-derived rows). The table applies the worker's stricter bar:
only routes an agent can act on. The delta (annotation-only, video-only,
heuristic rows with no safe contact) ships as `missing`; the ones the
sweep explicitly marked present-but-unactionable are listed in the table
header (`loose_bar_present_but_unactionable`).

**Refresh without code edits:** when Nicole's directory fill-in adds
policy-change contacts, re-run
`tools/extract_carrier_policy_change_routes.py` →
`tools/draft_carrier_routes.py` →
`tools/curate_carrier_routes.py <draft> robie_job_engine/data/carrier_policy_change_routes.json`,
review the diff, and open a PR. The worker loads the table at startup;
the table path can also be overridden with `ROBIE_4359_CARRIER_ROUTES`.

## Running

```bash
# Dry-run (default, safe):
python3 -m robie_job_engine.run_policy_change_carrier_contact \
  --evidence-out /tmp/carrier-contact-evidence.json

# Live (only after Carlo's approval):
ROBIE_4359_CARRIER_MODE=live python3 -m robie_job_engine.run_policy_change_carrier_contact \
  --evidence-out /opt/streetsmart-hermes/robie-job-engine/data/overdue_policy_change_reports/evidence-carrier-contact-latest.json
```

Exit codes: 0 = ok, 2 = fail-closed (contract), 1 = unexpected.

## Deploy (DESIGNED, not built — do not deploy without Carlo's approval)

- Service: `deploy/systemd/robie-4359-carrier-contact.service`
- Timer: `deploy/systemd/robie-4359-carrier-contact.timer`
  (`OnCalendar=*-*-* 09:00:00 America/New_York` — daily 09:00 ET, after
  phase 3's proposed 07:30 ET run, no collision with phase 1's Tuesday
  08:00 ET run).
- Pinned dry-run: `Environment=ROBIE_4359_CARRIER_MODE=dry-run`.
- Deploy path: `/opt/streetsmart-hermes/4359-weekly` symlink (never flip
  `/opt/streetsmart-hermes/current`).
- State: `.../data/overdue_policy_change_reports/carrier_contact.json`
- Evidence: `.../data/overdue_policy_change_reports/evidence-carrier-contact-latest.json`

## Dependency

Phase 3 (PR #646, `feat/4359-followup-confirmation`) must merge before
phase 2 is promoted: the worker cross-posts carrier events to phase 3's
FollowupStore. Phase 2 never marks anything confirmed — confirmation
checking stays phase 3's job.

## Bland AI voice calling

Approved conceptually for carrier calls (never clients). DESIGNED only —
not built, not tested. A future `voice` route type would slot into the
manual-action queue path.

## Portal path — Robie works carrier websites directly (DESIGNED, not built)

Carlo 2026-09-27: "we would be going on carrier websites as well." The
9 `manual`-route carriers include portal-only routes (e.g. Kingstone:
"Done Online via System"). Today those go to the human manual-action
queue; the defined next build is a portal worker that works them
directly.

**Contract (already in code):** `build_portal_action()` in
`robie_job_engine/policy_change_carrier_contact.py` produces a
`PortalAction` record per portal route — carrier, carrier record ID,
portal URL straight from the routing table, route label/notes, policy +
account, and the action needed. Every `manual_action` queue entry now
carries `portal_actions`, so the portal URL is in the evidence a human —
or the future worker — starts from. A route whose directory entry names
a portal without a URL ships with `portal_url: null`; the human fills it
in, and it flows back into the routing table via the refresh pipeline.

**Build plan for the portal worker:**
1. Runs on the ROBIE browser runtime on hermes-poc-01 (same anti-
   detection + residential proxy as the EZLynx browser work). One tested
   Playwright flow per carrier: sign in → navigate to the policy-change /
   endorsement inquiry → submit the chase OR read back the change status.
2. Start with the carriers holding the most open changes. Each flow gets
   the reliability-battery treatment: wrong-policy submit, duplicate
   submit, session expiry mid-flow, and unexpected navigation must all
   fail closed (no write, queue entry kept, evidence logged).
3. Credentials: Secret Manager only, read at runtime by the box service
   identity. Never in code, notes, or the routing table.
4. Portal outcomes cross-post to phase 3's FollowupStore as history
   events only; confirmation stays phase 3's job.
5. **Known constraint — bot-blocked portals:** some carrier portals
   bot-block the box's egress (documented exceptions: Hartford EBC is
   unreachable from the box entirely; Selective's agent portal 403s the
   box — see the workspace AGENTS.md). A blocked portal stays in the
   human manual-action queue with its URL attached; it is never retried
   blindly. Test reachability per carrier before building its flow.
