# NatGen Policy Alerts — DOM Mapping (2026-10-05)

**Source:** Live browser session as Carlo, Streetsmart Risk Managers, Inc 9010158. Read-only; no files downloaded.
**Status:** MAPPED — ready for pull-logic implementation.

## Alert List Pages

### Pending Cancellations
- **URL:** `https://natgenagency.com/Reports/AgencyActivityReports.aspx?r=5`
- Reached by clicking dashboard "Pending Cancellations" link.
- **Columns:** POLICY | NAMED INSURED | PHONE # | PRODUCT | DIV | REASON | CANCEL DATE | AMOUNT DUE | ADDITIONAL PRODUCTS
- **Observed:** 3 rows (policies 2031936859 00, 2037678234 01, 2027592402 01)
- Controls: Print link, Export to Excel button

### Pending Non-Renewal
- **Same URL** (`?r=5` unchanged) — report type driven by Report Criteria combobox postback, not query string.
- **Columns:** POLICY | NAMED INSURED | PHONE | TYPE | PRODUCT | DIV | PROCESSED | EFFECTIVE | DESCRIPTION | PREMIUM | PRODUCER | ADDITIONAL PRODUCTS
- **Observed:** 2 rows (2035017657 - 00, 2025758382 - 01)
- Controls: Print link, Export to Excel, pagination ("Page 1 of 1 / View All / Rows per Page 10/20/30/40")

## Alert → Document Flow

1. Click policy number → "File Room - Policy Summary": `https://natgenagency.com/Policy/PolicySummary.aspx` (same-page nav, not PDF)
   - Shows policy metadata, coverage, billed/paid/commissions
   - Tabs: Policy History | DMV History | Email History | E-Signature History | Endmt Quote History
2. "Pending Cancel for NSF" / "Pending Cancel for Non Payment" rows appear in **Policy History** table
   - Columns: DATE EFFECTIVE | DATE PROCESSED | ACTIVITY | CHANGE IN PREMIUM | TOTAL COST | FORMS
3. FORMS cell has "View" link (href="#", JS-driven — no static URL on page)
4. Click "View" → PDF opens inline at: `https://natgenagency.com/Policy/DisplayPDF.aspx?iid=<guid>`
   - GUID is document-specific (observed: `e549962d-9b21-425d-7db0-08df22c41ea7`)
   - 4-page document observed; no download needed to view
5. **Reliable control:** the visible "View" text link (the row's direct link had empty accessible name)

## Alternate Source
- **Email History** section lists sent emails per endorsement (ENDMT, DESC, EMAIL, TIME SENT, OMIT, RESEND, VIEW EMAIL) — potential alternate source if notice was emailed.

## Implementation Notes
- Pull flow: report → PolicySummary.aspx → Policy History FORMS "View" → DisplayPDF.aspx?iid=
- Export to Excel exports the alert list only (not PDFs)
- Document GUIDs must be captured from the JS-driven View link at click time
