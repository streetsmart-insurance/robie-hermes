# Applied Pay payout matcher - draft, PROPOSE ONLY

The matcher parses Applied's settlement email plus xlsx, reads QBO receipt/reversal JEs and existing Trust 3021 deposits, and returns review buckets. It does not post to QBO or EZLynx. `qbo_client.py` reads QBO via GET/query but rotates its refresh token into Secret Manager; production use of that credential write-back needs separate owner approval.

## Bank status and double-count controls

An Applied email is a scheduled settlement notice, not proof that the deposit cleared the bank. `email_parse.parse` labels it `status: scheduled`. QBO's existing deposits are also **not** bank-clearing proof. The matcher only makes a new `ready` proposal when an independently verified `cleared_bank_deposits` entry matches account `10002 Trust Checking WF (3021)`, amount and payout/next-day date. It must carry `status: cleared`. No bank-source integration is included in this PR; until one exists, the daily report will correctly say `scheduled_unlanded` or `unmatched`, not `ready`.

An existing QBO deposit may be marked `already_posted` only if its `groups` are exactly the matched JE IDs and line totals tie. A same-amount QBO deposit containing unrelated JEs stops. `already_posted` never creates another proposal. Unknown, ambiguous, missing or wrong-account JEs stop. A completed bank deposit may still need a person to inspect business context before any manual posting; the report is advice, not authorization.

## Fee-aware review, never amount-only categorization

A settlement amount may combine premium and fees. If an upstream parser supplies explicit `premium_amount` and `fee_components` whose amounts tie to the settled line, the matcher reports the premium receipt and holds the fee for accounting review. Fee-only payments are held too. Caller-configured `fee_rules.note_terms` can flag a note or description as a possible fee; it does not infer an amount or account. An unrecognized or ambiguous fee cannot be safely classified from `$15`, `$16`, `$250` or `$500` alone. The intended MBR/MVR term needs Carlo's confirmation, and QBO fee-account mapping is still open. Fee-bearing items never make an automatic deposit proposal.

Run synthetic safety checks with `python3 -m unittest applied_pay.tests.test_safety`. The committed `fixtures/snapshot_sept.json` is a **small, wholly invented regression fixture**: all amounts, dates, names and identifiers are invented. The bank notice is scheduled, never cleared. Do not infer real bank state or actual posting from that fixture. Historical real-data end-to-end verification belongs outside GitHub.

There is no box service or cron installed for this code. Email/xlsx retrieval, an independent bank-clearing feed, a fail-closed runner, idempotent state, alerting and a deployment unit/timer are still needed before unattended production operation. The existing weekday task can run it manually read-only in the meantime. Never schedule both that task and a box timer for the same report without a deduplication design.

The old `tests/test_known_good.py` hand-match expectations are not valid under the new bank-clearing rule and retained real transfer/receipt identifiers; replace them with synthetic safety tests before running CI.
