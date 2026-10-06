# Progressive FAO SmartView Alerts — DOM Mapping (2026-10-05)

**Source:** Live browser session as Carlo Ferrara (33617), read-only mapping.
**Status:** MAPPED — ready for pull-logic implementation.

## Alert → List Page Mapping

### 1. POLICY LEVEL ALERTS

**"Policies pending cancel or renewal"**
→ `https://www.foragentsonly.com/managepolicies/reports/policiesneedservice/policiespendingcancellation/`
- Tabs: Pending Cancellation Due to Non-Payment | Pending Cancellation Due to Underwriting Reasons | Pending Renewals
- Controls: Export, Print, "Show entries" (10/25/50/100), Filter results, "Table Column" combobox
- Columns: Primary Named Insured | Policy Number | Product | State | Agent Code | Producer | Cancel Effective Date | Amount Due (+ reason cell: NON-PAYMENT / UNDERWRITING)
- Row links: View Customer Summary (insured name), View Policy Summary (policy number → detail page), Make Payment

**"Policies require follow up: 11"**
→ `https://www.foragentsonly.com/managepolicies/reports/policiesneedservice/customerfollowuplegacy/?Source=MsgCenter`
- Columns: Primary Named Insured | Policy Number | Product | State | Agent Code | Producer | Memo Sent | Message Subject | Action
- Per-row "Details" link → "Select a Document" AJAX modal (Document | Date Signed)

**"e-Sign follow-up required: 8"**
→ `https://www.foragentsonly.com/managepolicies/reports/policiesneedservice/esignmanager/`
- Per-row "Download" link under "New Business Documents" (href="#", JS-triggered)

### 2. CUSTOMER ENDORSEMENTS
**"Customer policy changes: 12"**
→ `https://www.foragentsonly.com/managepolicies/policyactivity/processeddateresults/policychanges/`
- Tabs: Cancels, Lapses, Reinstates | New Business, Renewals, Quotes | Policy Changes | Payments, Adjustments | Communications
- Columns: Primary Named Insured | Policy Number | Product | State | Agent Code | Producer | Description | Processed | Effective | Net Premium Change | Policy Term

### 3. CLAIMS
**"Recent claims: 3"**
→ `https://www.foragentsonly.com/managepolicies/claimscenter/report/?source=msgcenter`
- Row link "View/Print Documents" → policy Documents tab

## Document Pull Routes

### ROUTE A — Policy Summary → DOCUMENTS tab (PRIMARY)
1. Dashboard alert → report list
2. Click policy-number link ("View Policy Summary")
3. Detail page: `clpolicy.foragentsonly.com/Express/Default.aspx?FinalDestination=PolicySummary&pageName=PolicySummary&...` (session params: wGuid, correlationId, otg, StoreKey)
4. Click DOCUMENTS tab → `pageName=PolicyDocuments&FinalDestination=Documents`
5. "Policy Documents" table: (icon) | Date | Delivery | Document name
6. Click document-name button → PDF opens inline in Chrome PDF viewer
7. PDF URL pattern:
```
https://clpolicy.foragentsonly.com/Express/PDFHandler.ashx?sessionID=<...>&RequestType=RTS&Type=POLICY2&PolicyNumber=<num>&DocumentType=<type>&Transaction=<txn>&Recipient=<rec>&Timestamp=<ISO-8601>&Location=DISK&Index=<encrypted>&Count=1&DocumentCategory=0&DeliveryType=<USPS|EMAIL>&wGuid=<...>&correlationId=<...>&pageName=<type>
```
8. **Verified:** DocumentType=CANCNTC (cancel notice), Transaction=CANU, Recipient=INS — policy 970498127 (Commercial Auto/Trucking)
9. PDF toolbar has Download/Print buttons (not page-level download)

### ROUTE B — "Select a Document" modal (Follow-up Manager)
1. Customer Follow-up Manager → "Details" link → modal (Document | Date Signed)
2. Document link → PDF in new tab:
```
https://www.foragentsonly.com/ManagePolicies/Policy/PDF/ViewPDF/?polNum=<num>&contentId=AWSPOL;<base64>
```
3. Base64 decodes to JSON: `{Key, EastBucket, WestBucket, PK, SK, Metadata:{PolicyNumber, Recipient, Transaction, DocumentType, MimeType}}`
4. **Verified:** DocumentType=ACEMEMO (underwriting memo), polNum=876263535, dated 09/29/2026

### ROUTE C — e-Sign manager direct download
- Per-row "Download" link, JS-triggered (href="#"), mechanism not activated during mapping

## Implementation Notes
- Every alert opens a full page, never a modal (except "Select a Document")
- Report "Export" = tabular data, NOT source PDFs
- "View Policy Summary" links are the universal drill-through
- No CAPTCHAs encountered
- Not yet mapped: "Save canceled/expired policies: 4", "Pending paperless: 6" (same families, open on demand)
