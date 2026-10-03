# Wells guest reader first Test step

Captured-page shadow normalizer only. No network, login, scheduling, postings, money movement or email. Prod execution explicitly rejected. The authenticated guest session was inspected manually Oct 3: both StreetSmart account pages ending3021and3018are accessible; UI contains Pending/Authorized/Posted sections. This inspection is not an unattended server reader or proof of guest permissions on every endpoint. The code accepts operator-reviewed view-only capture metadata and compares page rows with the original artifact. That verifies integrity, not the origin of an externally supplied artifact.

3021is the Trust reconciliation lane for Applied settlement credits.3018is a separate Operating observation lane for commission/remittance credits, expenses, card payments and returns. It must not qualify a3021deposit or make an earned-cash decision from balance differences.

Observed Applied bank TRN references differ from email transfer IDs in the earlier matcher trial. Preserve bank reference candidates separately. No amount/date-only crosswalk. No stable transaction ID was proved from the table, so IDs remain null. Row positions/hashes are evidence locations, never bank IDs. Posted, available and collected balances are distinct; no observed UI status is silently renamed cleared. All rows remain review-only and cleared_bank_deposits stays empty.

Remaining: confirm identity/view-only entitlement, transaction details/export stable IDs and status semantics, full date-window pagination, source-to-row independent review, exact Applied-to-bank reference crosswalk and return/reversal handling, fresh capture source, private durable evidence/exception queue, payment-bound EZLynx evidence, Test shadow acceptance, reviewed deployment and schedule. No login credential is embedded or copied to Test.

Capital One is the next design lane, blocked on verified card view access. Capture issuer transaction IDs, posted/pending status, merchant, amounts, dates, card last4, purchases/refunds/payments and source freshness; compare against QBO without postings. Card payment evidence is not earned Trust cash and card available credit is not bank-clearing proof. QBO day-staleness is unverified in this draft. Existing access task remains with its owner; no recovery/reset or credential request here.

Synthetic fixtures only in repository. Real private bank captures stay outside GitHub. No claim of full shadow reconciliation or current financial totals.
