# Travelers Direct Bill Activity — DOM Mapping (2026-10-05)

**Source:** Live browser session as CarloF1 (Carlo Ferrara). Read-only; no files downloaded.
**Status:** MAPPED — ready for pull-logic implementation.

## Navigation Path
- Portal: `https://foragents.travelers.com/Business`
- "Agency Reports" tab → "Direct Bill Activity — Cancellation & Reinstatement Notices"
- List URL: `https://foragents.travelers.com/Business/billingandpolicyservices/directbillactivity`
- Tooltip: "This report displays, for the producer codes selected, direct billed policies in the various stages ..."

## Notices List Page
- `<h1>` "Direct Bill Activity"
- `<select id="select_date">` "Select Date" — native dropdown with ~25 discrete activity dates only (observed range 06/29/2026 → 10/05/2026)
- **No date-range picker, no "last 2 weeks" preset** — filter = pick one date, click Search → "Showing N results"
- **Columns:** Agent Code | Master Code | Prime Code | Agency (Agency Name) | Date Received | [unlabeled column with "view" link]
- Sample row: `0X4688 / 0CHJ29 / 0X4688 / STREETSMART RISK MGR / 10/05/2026 / view`
- Each date returned exactly 1 row for this agency (producer codes 0X4688 and 0725HN observed)

## View → Detail Page
- "view" link: `href="javascript:void(0);"` class `gridActionLink`
- Data attributes: `data-agentcode="0X4688"`, `data-datereceived="2026-10-05T00:00:00"` (ISO timestamp)
- Opens a **new tab** (not PDF, not modal): `https://foragents.travelers.com/Business/BillingAndPolicyServices/DirectBillActivityDetails`
- URL has **no query params** — state passed via data attributes (server-side post)
- Detail DOM: `div#agencyInfoDiv` (PRODUCER CODE, DATE, agency name/address) → `div#js-tab-1.tabpanel` → `div#billingActivityReportDetail` (bold section title + intro + `<table class="display dataTable word-break">`) → remittance address → form with Save Document button + Print Page link

## Section Types (one per detail page — the report "stage" for that date)
1. **Pre-cancellation Alert** (observed 10/05, 10/02): "A cancellation notice will be sent for the following policies on the date shown below. Total due amounts exclude installment charge."
   - Columns: Account Number | Policy Number | Account Name | DNOC Minimum Due | Partial Payment | Account Balance | Cancellation Notice Date
2. **Cancellation Alert** (observed 09/30): "The following policies will cancel on the date shown below if sufficient payment is not received:"
   - Columns: Account Number | Account Name | Policy Number | DNOC Minimum Due | Partial Payment | Account Balance | Cancellation Date
3. **Reinstatement** (observed 09/22, no bold title): "The following policies appeared previously as pre-cancellation policy. Since that date subsequent activity has taken place to clear the cancellation."
   - Columns: Account Number | Account Name | Policy Number | Agent Activity Date
   - Note: "Please note there may be other policies on the above accounts."

## Download / Document Pattern
- **Save Document** button: `<form action="/Business/BillingAndPolicyServices/SaveReportToWordDocument" method="post">` with hidden inputs `agentCode`, `date`, `__RequestVerificationToken`
- POST returns a **Word document** (data-coremetrics="AGTS_BI_DirectBillActRpt_Click_Download") — NOT clicked (read-only scope)
- Print Page: `javascript:window.print()`

## Pull-Logic Flow
1. List page: iterate activity dates in select_date (filter to recent window client-side)
2. Click "view" link (data-agentcode + data-datereceived) → new-tab detail page
3. Parse `div#billingActivityReportDetail` table for section type + rows
4. Optional: POST SaveReportToWordDocument for the Word doc per date

## Implementation Notes
- The "document" here is a report (Word doc per date), not per-policy PDFs — output contract needs: download Word doc per date, parse rows, ledger by (agentCode, date, policy)
- Date filtering must be client-side over the discrete date dropdown values
- Anti-forgery token required for the Word doc POST
