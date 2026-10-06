# Utica First UFirst Now — DOM Mapping (2026-10-05)

**Source:** Live browser session as Carlo Ferrara, Agency Administrator, Partner 20000985, Streetsmart Risk Managers Inc. Read-only; no files downloaded.
**Status:** MAPPED — ready for pull-logic implementation.

## Portal
- URL: `https://ufirstnow.uticafirst.com/oneshield/sso` (SPA — URL never changes; session token in query)
- Tabs: HOME | CUSTOMERS | PARTNERS | POLICIES | POLICY TRANSACTIONS | QUOTES | ANALYSIS

## Canonical Alert List: POLICY TRANSACTIONS
- "POLICY | TRANSACTION LIST"
- Filter radios: Today / All / Processed / Unprocessed; SAVE FILTER, FILTER LIST, CLEAR FILTERS, EXPORT
- **Columns:** POLICY NUMBER | TRANSACTION TYPE | INSURED NAME | EFFECTIVE | PROCESSED | AGENT | BUSINESS INTRODUCER | PRODUCER | PREMIUM BEFORE | CHANGE | PREMIUM AFTER | STATUS | DOCUMENTS
- Cancellation-relevant TRANSACTION TYPE values: "Pending Cancellation(NOC)", "Cancellation", "Rescind Pending Cancellation", "Non-Renewal", "PreRenewal Notice"; related: "Reinstatement", "Change Payment Plan"
- No transaction-type dropdown — grid is the filter surface

## Alert → Document Flow
1. Row's DOCUMENTS cell → "Documents" link → "POLICY | TRANSACTION | DOCUMENT LIST"
2. View tabs: SUMMARY | APPLICATION | PREMIUM SUMMARY | UNDERWRITING ALERTS | FORMS | DOCUMENTS
3. Document grid columns: ID | NAME | CONTENT TYPE | DESCRIPTION | ADDED DATE | SOURCE | RENDERING STATUS
4. Observed: ID 504387230999 "NonPay Notice-Insured" + ID 504387231099 "NonPay Notice-Agent" (Document Package, UF Document Delivery, Completed, 10/04/2026)
5. Click NAME → PDF renders inline in iframe (`iframe#uxiframe-1314-iframeEl`)
6. **PDF URL pattern:** `/oneshield/DocGenServlet?docId={documentId}&USER_SESSION_GUID={sessionGuid}&DRAGON_TRANSACTION_ID={txnId}`

## Transaction Summary
- "POLICY | TRANSACTION | SUMMARY": Transaction Type, Created/Effective dates, Status, Description
- "Pending Cancellation Details": Cancellation Reason, Target Cancellation Date, Transaction Mailing Date, Cancellation Notice Description, Cancellation Method, Scheduled Payments grid
- Policy CURRENT SUMMARY shows "Pending Cancel Count: 3", "Non-Pay Cancel Count: 2"

## Batch Alternative: ANALYSIS → OTHER REPORTS → "Pending NonPay Cancellations"
- Pentaho PRPT rendered in iframe; controls: Select Date, Output Type (PDF/HTML/Excel/CSV/etc), Row Limit, View Report
- Columns: Agent Number | Policy Number | Insured Name | Policy Effective Date | PayPlan | Cancellation Date | Amount Past Due | Policy Balance
- **No drill-down links** — take Policy Number back to POLICY TRANSACTIONS for the PDF

## Bulletins
- Home Bulletin grid: zero rows. Bulletins dialog: empty. No cancellation notices here.

## Pull-Logic Summary
- Canonical: POLICY TRANSACTIONS (filter by transaction type) → per-row DOCUMENT LIST → DocGenServlet PDF
- Batch alt: Pending NonPay Cancellations report → per-row policy lookup → DocGenServlet PDF
