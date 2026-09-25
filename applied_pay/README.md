# Applied Pay payout matcher - draft, PROPOSE ONLY

The matcher parses Applied's settlement email plus xlsx, reads QBO receipt/reversal JEs and existing Trust 3021 deposits, and returns review buckets. It does not post to QBO or EZLynx. `qbo_client.py` reads QBO via GET/query but rotates its refresh token into Secret Manager; production use of that credential write-back needs separate owner approval.

## Bank status and double-count controls

An Applied email is a scheduled settlement notice, not proof that the deposit cleared the bank. `email_parse.parse` labels it `status: scheduled`. QBO's existing deposits are also **not** bank-clearing proof. The matcher only makes a new `ready` proposal when an independently verified `cleared_bank_deposits` entry matches account `10002 Trust Checking WF (3021)`, amount and payout/next-day date, plus an exact `verified_payout_ref`, `verification_source: bank_record` and nonempty `bank_transaction_id`. A `status: cleared` flag without independent source and payout binding is insufficient. No bank-source integration is included in this PR; until one exists, the daily report will correctly say `scheduled_unlanded` or `unmatched`, not `ready`.

An existing QBO deposit may be marked `already_posted` only if its `groups` are exactly the matched JE IDs and line totals tie. A same-amount QBO deposit containing unrelated JEs stops. `already_posted` never creates another proposal. Unknown, ambiguous, missing or wrong-account JEs stop. A completed bank deposit may still need a person to inspect business context before any manual posting; the report is advice, not authorization.

## Fee and payable review

For new proposals the runner must check EZLynx notes for each payment, attach source-bound evidence (`ezlynx_note_check` with status `checked`, source `ezlynx`, the same PSP reference, note IDs and notes), and stop short of READY when that check is absent. An MBR or agency-fee note triggers fee review; a payable note (such as Shirley's example) is held as a payable, not relabeled as a fee. Conflicting fee/payable notes stop. Explicit premium and fee components can show a suggested split only when a bound EZLynx fee note exists and sums tie. Fee-only, small unexplained and note-flagged payments remain human review; no fee or payable account is guessed. The current PR has **no EZLynx note-fetch adapter**; until one is built and tested, new deposits cannot safely become READY.

Run synthetic safety checks with `python3 -m unittest applied_pay.tests.test_safety`. The committed `fixtures/snapshot_sept.json` is a **small, wholly invented regression fixture**: all amounts, dates, names and identifiers are invented. The bank notice is scheduled, never cleared. Do not infer real bank state or actual posting from that fixture. Historical real-data end-to-end verification belongs outside GitHub.

There is no box service or cron installed for this code. Email/xlsx retrieval, an independent bank-clearing feed, a fail-closed runner, idempotent state, alerting and a deployment unit/timer are still needed before unattended production operation. The existing weekday task can run it manually read-only in the meantime. Never schedule both that task and a box timer for the same report without a deduplication design.

The old real-data hand-match expectations have been replaced with a synthetic smoke check. The safety tests include duplicate transfers, false READY, missing bank proof and fee review.
