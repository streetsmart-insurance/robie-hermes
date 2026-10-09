# Quoting channels — reference

Detailed channel behavior learned from live quoting runs (September 2026).
The Skill states the rules; this file states the facts behind them.

## Tarmika

- Create Quote offers: BOP, Workers Compensation, Commercial Automobile,
  Cyber Liability, General Liability, Professional Liability.
- No Umbrella/Excess quote type. No Inland Marine quote type.
- Lines cannot be added after the quote is created.
- Market selection: only in-appetite markets are selectable. NOT IN APPETITE
  markets are greyed out and cannot be selected.
- `Quote Request Not Submitted` on a selected market = a required question
  was not answered (seen: `'None' is required but was not answered`). Answer
  the question; it is not a decline.
- Declines: the portal does not always name the declining carrier. Do not
  attribute a decline unless the portal names it.
- Responses arrive as referral quotes: Insurer Quote Status
  `Needs to be Referred`, Valid Till N/A, term dates often not displayed.
  These are not final/bound.
- Observed results (R&D Masonry WC 0000377933): Hanover $1,945.88, ERGO NEXT
  $3,012.89, Merchants $626, AmTrust `More Information Needed`.

## Carrier portals — commercial auto

Commercial auto is quoted on each carrier's own website, never Tarmika:
GEICO, GUARD, Progressive, Nationwide, Selective.

- GUARD may enforce its own minimums: observed forced building limit
  $870,565 (minimum vs $967,294 replacement cost) against a $212,000 request
  on BOP RDBP785355 ($20,634). Always read back the portal's actual values.
- GUARD may drop coverages: observed non-owned liability missing on auto
  RDAU785203 ($34,058, 10 vehicles / 9 drivers). Flag as a deviation.
- GUARD logins have been flaky: one saved login rejected as invalid, a second
  disabled at the carrier, a third required a 6-digit emailed code. Budget
  time for the login step; never report a quote as in-progress when the run
  is actually parked at login.
- Hartford EBC is unreachable from hermes-poc-01 (TCP hangs); run Hartford
  portal work in the sandbox browser, never the box.
- Selective's agent portal 403-blocks hermes-poc-01; run Selective portal
  work in the sandbox browser, never the box.

## EZLynx Submission Center

- Check each carrier's availability per line before promising the channel.
  Guard is not available in the Submission Center — Guard auto needs a portal
  quote.
- Per Carlo's direction, full packages go through Merchants via the
  Submission Center where Merchants is available.
- Observed (Commercial Property & GL submission 158204): Utica $67,842.92
  (property only — GL "Not Submitted" to Utica), Merchants $23,785,
  Selective $4,031, GUARD BizGUARD #EMBP768903 $80,876.69.

## Proposal generation per carrier

- **Hartford**: "Create Quote Proposal" on the premium page → Create (no
  email/send). Observed: 18 pages, 475,647 bytes, sha256-verified. Read back
  premium, term, and class from the PDF before filing.
- **Travelers**: the QUOTE PROPOSAL button has been unresponsive. A
  one-page print-to-PDF is a summary only — Travelers' own page states it is
  illustrative and not binding.
- **Nationwide**: Display Proposal has run without producing a PDF. Same
  summary rule applies.
- **USLI**: quote letters arrive as multi-page PDFs (observed 12-page
  MHB026S0976, $450.35). Bind status needs carrier proof — a bind request
  "under review" with the signed app outstanding is not bound.

## Underwriting facts — where they come from

- Inland marine equipment schedules: the supplemental application PDF on the
  EZLynx file. Never invented.
- Vehicle Original Cost New: the file or Carlo. Observed: 2014 RAM 2500 cost
  new not on file → carded to Carlo rather than substituting the other
  truck's $36,030.
- Class codes: use the file's literal value; flag mismatches (observed
  46622 Parking-private on the quote vs 4622 in Carlo's note).
