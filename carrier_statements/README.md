# carrier_statements - agency-bill statement parser (iteration 1, READ-ONLY)

Turns carrier statements (PDF / Excel from the Accounting@ inbox) into one standard structure, ties
every file to its own printed totals, and prints an open-items summary per carrier for the monthly
August-onward reconciliation. Nothing here sends, posts, pays or writes to any system.

## Standard structure (model.py)
statement: carrier, account_no, statement_date, total_due, lines[], groups{policy/insured: printed balance},
lines_total, ties, problems[]
line: insured, policy, invoice, txn_type, description, eff_date, due_date, gross, comm_rate, comm_amt, net,
kind (charge | tax_fee | credit | payment | adjustment | commission_due_to_agency), group, flags[]

## Tie-out rule (same philosophy as applied_pay)
- sum(line.net) must equal the printed statement total, and each policy's lines must equal its printed
  policy balance. Anything that doesn't tie is STOPPED and reported, never forced.
- XS Brokers prints policy balances net of payments it doesn't list: the gap becomes one flagged
  "not itemized on statement" line per policy, derived from the printed balance.

## Parsers (registry.py maps sender domain -> parser; unknown senders are reported, not guessed)
| carrier | format | module |
|---|---|---|
| RPS (rpsins.com) | PDF broker statement | parsers/rps.py (flags "commission retained due to aging balance") |
| Asero / TIP National (tipnational.com) | PDF statement | parsers/asero.py (old PFC credits flagged for quarantine) |
| PPIB (specialtyprogramgroup.com), XPT (xptspecialty.com) | PDF "Account Statement" | parsers/spg.py |
| XS Brokers (xsbrokers.com) | PDF account statement | parsers/xsb.py |
| One80 / Diesel IEX (one80.com) | weekly xlsx | parsers/one80.py |
| Flood Risk Solutions (floodsol.com) | commission report xlsx (pays us) | parsers/flood.py |

Seven wholly invented fixtures cover the parser layouts and tie-out controls. They do not prove any actual carrier balance, remittance, or bank clearing. Keep real statement verification in a private, non-GitHub workflow.

## Run / test
- `python3 tests/test_parsers.py` - wholly invented synthetic fixtures (names, policy numbers, amounts, dates and references are fake).
- `python3 run_statements.py rps=/path/stmt.pdf xsb=/path/x.pdf` or `--gmail search.json`.
- Needs python3, openpyxl, pdftotext (poppler-utils). Nothing is installed or scheduled on any server.

## Next iteration (not built)
Match each open item to EZLynx invoices / receipts, premium-finance funding and QBO; record status in the
AppSheet Insurance Carriers tab; more carriers as statements arrive (RockLake, Trinity, Tapco, AmWINS, ...).
