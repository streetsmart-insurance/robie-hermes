# Empirical Applied-to-Wells candidate crosswalk, Test only

A separate pure candidate function, not wired into the live matcher or reader. No network, secrets, financial postings, bank actions or schedules. Never outputs cleared_bank_deposits. Trust3021only; Operating3018cannot qualify this lane.

The observed relationship is characters7-11(1-based) of the16-character email transfer ID and full bank TRN, plus exact net amount, account, observed1-5calendar-day lag and global uniqueness. This is a candidate-generation rule, NOT a vendor-documented identity mapping or clearing rule. Full IDs/source references and supplied PSP refund/return chain are preserved. Bank PAYOUT descriptor is preserved separately. No invented stable bank ID.

All collisions (even filtered-out wrong-account rows), duplicate IDs, grouped deposits, missing keys, untied lines, invalid amounts, wrong accounts, missing sources, nonposted/debit rows, returns/reversals and unsupported dates remain needs_review. Unmatched rows are retained. Candidate findings still state clearing/vendor proof and stable ID gaps.

Private historical replay:55email/bank pairs read Oct3from Accounting@settlement messages and Wells3021table. Every pair generated a review-only candidate, no cleared output. The input itself is private and excluded from GitHub. This measures fit to the observed sample, not generalization or fresh bank authentication. Synthetic tests cover adverse inputs separately.

Remaining: independently verified portal/ACHtrace crosswalk, stable bank identifiers, real clearing semantics, live source capture, freshness/window completeness, PSP-to-EZLynx identity/fee evidence, durable return monitoring and shadow acceptance. No automatic payout, refund, transfer, ledger post, note or task close. No merged/install/schedule/Prod changes.
