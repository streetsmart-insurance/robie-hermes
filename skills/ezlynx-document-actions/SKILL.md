---
name: "ezlynx-document-actions"
description: "EZLynx reassignment, document move, and labels with reusable preconditions, postconditions, retry rules, and destination evidence."
---

# EZLynx document actions (browser verification)

Use this Skill for the three mutating EZLynx browser actions. Pair with
`robie-playwright-browser` for locator mechanics. The Job Engine alone may
authorize `COMPLETE` after independent destination read-back.

## Actions

| Action | Purpose |
| --- | --- |
| `ezlynx.reassign` | Change Assigned Producer (stable assignee id wins) |
| `ezlynx.move_document` | Move a document through the Angular cross-frame dialog |
| `ezlynx.apply_label` | Apply a document label via user-like fill + Apply |

## Preconditions (before any click)

Shared:

- Authenticated EZLynx session on the persistent Test Chrome (CDP)
- Exact account / applicant id already resolved (no search guessing)
- Write-scope allowlist permits the applicant / account
- Payload includes every field in `browser_verification.ACTION_PRECONDITIONS`

Per action:

- **reassign** — `resource_id`, `applicant_id`, `assignee_id`, `assignee_name`
- **move_document** — `document_id`, `document_name`, `account_id`,
  `destination_id`, `destination_name`, `move_control` locator
- **apply_label** — `resource_id`, `account_id`, `document_name`, `label_id`,
  `label`, `label_control` locator

Missing fields → `NEEDS_CLARIFICATION`. Do not invent ids.

## Actions (deterministic)

1. Record `action_intent` checkpoint before the first mutation.
2. Prefer stable ids (`data-id` / `data-value`) over visible text.
3. For move: wait for the DocumentLibrary iframe, destination heading, and
   enabled Move button — never sleep-and-guess.
4. For label: `fill_like_user` only (never assign `element.value`).
5. Submit once per idempotency key. Do not repeat after a matching destination.

## Postconditions / evidence

Prefer `EZLYNX_API_READBACK` from a safe network capture when the EZLynx JSON
response exposes the destination fields. Otherwise require
`FRESH_PAGE_READBACK`: navigate/reload the live page and re-read the same
fields. Worker receipts, screenshots, and recordings are diagnostic only.

Required observed keys live in `browser_verification.ACTION_POSTCONDITIONS`.

## Retry rules

| Failure | Retry? |
| --- | --- |
| `PLAYWRIGHT_BLOCKED` / ambiguous locator | No |
| `AUTH_CHALLENGE` / session expired | No (refresh session Skill first) |
| Transient network / timeout | Yes, within Job Engine max attempts |
| Postcondition mismatch after read-back | No → `UNVERIFIED` |

## Runtime wiring

On `ROBIE_ENV=TEST` with `ROBIE_PLAYWRIGHT_CDP_URL` (or `ROBIE_BROWSER_CDP_URL`),
`build_runtime_engine` auto-wires `CdpEzlynxPort` for worker + verifier.
Production stays unavailable unless an explicit port is injected.

## Safety

- Never use PAWIVA / account `221398001` for tests
- Stop before Save / email / payment / bind unless the Job type explicitly
  authorizes that side effect
- Do not weaken COMPLETE: missing authoritative evidence stays `UNVERIFIED`
