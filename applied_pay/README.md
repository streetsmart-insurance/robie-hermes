# Applied Pay payout matcher - phase 1 (READ-ONLY)
Nothing here posts, edits or logs in anywhere. It turns snapshots into a report + proposed deposits.
- portal_parse.py   Applied Pay line-detail text -> payouts JSON (validates line counts)
- match.py          snapshot {cutoff, payouts, ledger, bank_deposits, aliases} -> 4-bucket report; `python3 match.py snap.json out.json`
- fixtures/         September portal dump (real) + ledger/bank from the hand-match (receipt order inside 015086-092, 015098-103, 015119-122 ASSUMED)
- tests/test_known_good.py  must reproduce the September hand-match
- portal_live_parse.py  parses the live Payouts page (all batches expanded, text read); verifies "N Items" per batch.
  Pull (cloud browser, Markley1): Marketplace > My Integrations > Applied Payments > ... > View > Go to Applied Pay;
  Payouts > header chevron (expand all) > scroll grid ~1500px per step, read text each step, merge by transfer id.
- fixtures/payouts_live*.json  live pull 2026-09-23 13:07 (33 payouts 8/03-9/22); Sept identical to hand fixture.
- tests: `python3 tests/test_known_good.py [snapshot_sept_livepull.json]`
- qbo_client.py  read-only QBO client with refresh-token write-back. Secrets come from GCP Secret Manager (qbo_production_*); the rotated refresh token is written back as a new version.
- qbo_snapshot.py  live read-only QBO side (receipt/-R JEs with cash-side account; 3021 deposits + which JEs they group).
  Fee/payable "applications" off Unapplied Cash are excluded. v87 token (reconnected 9/23).
- run_live_test.py  live pull + live QBO -> matcher, and checks every proposed deposit against the posted deposit.
  9/23 result: 9 of 9 proposed deposits identical to what accounting posted; 3 stops (9/01 Segura no source JE, 9/10+9/14 pair).

## Email source (preferred, from 9/23)
Applied emails "Batch Settlement Details for Primary Account Reconciled on <date>" (noreply_pay@mail.myappliedproducts.com) to Accounting@ each business morning ~7:50 ET. email_parse.parse(body_text, date_header, xlsx_path) -> same payout dict as portal_live_parse. Transfer ID + Total Deposit from body; lines from Transactions + Returns sheets; payout_date = email arrival date (ET). Verified 14/14 identical to the live portal pull for Sept (run_email_test.py: 9/9 proposed deposits identical to posted).

## Daily run (weekdays 8:20 AM ET, run by Instinct; PROPOSE ONLY)
1. Read the Applied "Batch Settlement Details" emails in Accounting@ Gmail (read-only) and download the xlsx.
2. email_parse.py -> payouts JSON (portal pull via portal_live_parse.py only as fallback if an email is missing).
3. On hermes-poc-01: qbo_snapshot.build(...) live read-only QBO query + match.Matcher -> 4-bucket report
   (ready / waiting approval / stopped / past cutoff). Receipts sitting on QBO "Trust" (id 295) are flagged.
4. Report is drafted/emailed to Accounting@ and summarized to Carlo. Nothing is ever posted to QBO.

Runtime dependencies on the box: python3 + openpyxl, gcloud with Secret Manager access to
qbo_production_client_id / qbo_production_client_secret / qbo_production_refresh_token / realm.
No cron or service is installed on the box; nothing here runs unless invoked.

## Data in this directory
fixtures/snapshot_sept.json holds real September 2026 payout + ledger data (customer names, amounts)
needed by tests/test_known_good.py. Raw portal dumps, live QBO snapshots and reports are NOT committed.
