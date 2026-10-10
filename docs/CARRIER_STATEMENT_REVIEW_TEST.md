# Carrier statement review, Test draft

Port of the clean synthetic parser draft #577. No customer-derived fixture values are included.

## What this proves

Existing parser fixtures cover seven source layouts. The review contract requires a supplied source file path, content digest, source ID, agency, carrier, account and printed date. Missing amounts, unitemized balance adjustments and quarantined credits are held. Arithmetic totals tying is not financial reconciliation.

Matching requires agency, carrier, account, policy and invoice together, one stable destination record, and exact signed cents. No insured-name or amount-only match. Every result is review-only, not cleared and not pay-ready. Credits, payments and adjustments need allocation-owner evidence. Bank landing, premium-finance funding and receipts remain unverified.

## Not built or activated

Live statement intake, AppSheet roster/status writes, EZLynx invoice/receipt connectors, financing/bank connectors, statement imports, payments, email chases, a schedule and a production release are not included. Caller-supplied record data is not itself verified external evidence. Test results prove the contract, not actual carrier balances.

Use explicit saved attachment paths. Do not derive paths from filenames. Real statement replay belongs in private scratch, never repository fixtures. Review the actual source date rather than the email date. Wrong-year statements are held.

## Test

`python carrier_statements/tests/test_parsers.py`

`PYTHONPATH=. python -m pytest tests/test_carrier_statement_review_contract.py`
