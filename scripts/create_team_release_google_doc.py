#!/usr/bin/env python3
"""Generates the StreetSmart Team Release Guide in Google Docs via Google Drive API."""

import io
import json
import logging
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2.credentials import Credentials

TOKEN_PATH = "/opt/streetsmart-hermes/.hermes/robie_google_token.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

HTML_CONTENT = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { font-family: Arial, sans-serif; line-height: 1.6; color: #1f2937; margin: 40px; }
  h1 { color: #1e3a8a; border-bottom: 2px solid #1e3a8a; padding-bottom: 8px; font-size: 26pt; }
  h2 { color: #1d4ed8; margin-top: 24px; border-bottom: 1px solid #e5e7eb; padding-bottom: 4px; font-size: 18pt; }
  h3 { color: #2563eb; margin-top: 18px; font-size: 14pt; }
  p, li { font-size: 11pt; }
  .badge { display: inline-block; padding: 4px 10px; background-color: #10b981; color: white; border-radius: 4px; font-weight: bold; font-size: 10pt; }
  .callout { background-color: #f3f4f6; border-left: 4px solid #2563eb; padding: 14px 18px; margin: 16px 0; border-radius: 0 6px 6px 0; }
  .callout-warning { background-color: #fffbeb; border-left: 4px solid #f59e0b; padding: 14px 18px; margin: 16px 0; border-radius: 0 6px 6px 0; }
  .callout-success { background-color: #ecfdf5; border-left: 4px solid #10b981; padding: 14px 18px; margin: 16px 0; border-radius: 0 6px 6px 0; }
  table { width: 100%; border-collapse: collapse; margin: 18px 0; }
  th { background-color: #f1f5f9; text-align: left; padding: 10px 12px; border: 1px solid #cbd5e1; font-weight: 600; font-size: 10.5pt; }
  td { padding: 9px 12px; border: 1px solid #e2e8f0; font-size: 10pt; vertical-align: top; }
  code { background-color: #f1f5f9; padding: 2px 6px; border-radius: 4px; font-family: "Courier New", monospace; font-size: 10pt; }
  ul, ol { padding-left: 24px; }
  li { margin-bottom: 6px; }
  .footer { margin-top: 40px; padding-top: 12px; border-top: 1px solid #cbd5e1; font-size: 9.5pt; color: #64748b; font-style: italic; }
</style>
</head>
<body>

<h1>StreetSmart Insurance — Ascend API Automation & Robie AI Release Guide</h1>

<p>
  <span class="badge">STATUS: LIVE IN PRODUCTION</span> &nbsp;|&nbsp; 
  <strong>Release Version:</strong> 2.4.0 (Ascend Full Suite) &nbsp;|&nbsp; 
  <strong>Effective Date:</strong> September 7, 2026<br>
  <strong>Audience:</strong> Commercial & Personal Lines Producers, Account Managers, CSRs, Accounting & Finance Team (<code>Markley1</code>)
</p>

<div class="callout-success">
  <strong>🚀 What's New:</strong> Robie now autonomously manages our <strong>Ascend Premium Financing & Payment Agreement</strong> lifecycle, automatically files agreements directly into <strong>EZLynx discussion cards</strong>, syncs policy events (cancellations, past due, signed agreements, reinstatements) every 1 hour, routes accounting audit tasks directly to <strong>Markley1</strong> in EZLynx, and connects to <strong>QuickBooks Online</strong>.
</div>

<h2>1. How to Generate an Ascend Agreement (Producers & Account Managers)</h2>

<p>You can now generate Ascend financing agreements without logging into the Ascend portal. Robie handles extraction, underwriting parameter validation, and EZLynx filing automatically.</p>

<h3>Option A: Email Robie (Simplest & Recommended)</h3>
<ol>
  <li><strong>Send an email</strong> to <code>robie@streetsmart.insurance</code> from your StreetSmart email.</li>
  <li><strong>Attach the quote</strong> (PDF) or paste the quote details into the body.</li>
  <li><strong>Underwriting Parameter Verification:</strong> Robie requires 4 essential underwriting parameters to ensure accurate financing:
    <ul>
      <li><strong>Agency Fee:</strong> Is there an agency fee? (Default is <code>$350.00</code>, or specify another amount).</li>
      <li><strong>Commission Rate:</strong> What is our agency commission rate? (e.g., <code>10%</code>, <code>12%</code>, <code>15%</code>).</li>
      <li><strong>Surplus Lines Tax:</strong> Are there surplus lines taxes or stamping fees? (Yes/No, or amount).</li>
      <li><strong>Terrorism Coverage (TRIA):</strong> Does the quote include or exclude terrorism coverage? (Especially important when quotes provide dual options).</li>
    </ul>
  </li>
  <li><strong>Autonomous Clarification:</strong> If any of these 4 parameters are missing or ambiguous in your quote document, Robie will immediately email you back asking for clarification. Simply hit reply with your answers (e.g., <em>"agency fee $350, commission 12%, no surplus lines, TRIA included"</em>).</li>
  <li><strong>Instant Delivery:</strong> Robie generates the Ascend agreement in under 2 seconds, posts the checkout link into the insured's EZLynx discussion card, and emails you back the client checkout link and premium breakdown.</li>
</ol>

<h3>Option B: Google Chat</h3>
<p>You can also message <code>@Robie</code> in your team Google Chat space with the quote. Robie will clarify any missing fields and provide the agreement link directly in chat.</p>

<div class="callout">
  <strong>💡 Client Payment Options:</strong> Ascend agreements automatically provide the insured with both options: <strong>Pay in Full (ACH/Debit/Credit)</strong> or <strong>Monthly Financing (Automatic ACH/Card installments)</strong>.
</div>

<h2>2. EZLynx Account Event Synchronizations (Hourly)</h2>

<p>Every 60 minutes, Robie scans Ascend for real-time customer and policy events and synchronizes them directly into EZLynx:</p>

<table>
  <thead>
    <tr>
      <th>Event Type</th>
      <th>Ascend Trigger</th>
      <th>EZLynx Action</th>
      <th>Assigned To</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><strong>Cancellation Notice</strong></td>
      <td>Notice of Cancellation issued by finance company</td>
      <td>
        • Adds account label: <code>Cancellation Notice</code><br>
        • Embeds cancellation notice PDF into account docs<br>
        • Creates high-priority task with plain text amount due & due date<br>
        • Posts discussion note signed <em>"Robie was here"</em>
      </td>
      <td><strong>CSR / Producer</strong> on policy</td>
    </tr>
    <tr>
      <td><strong>Past Due Notice</strong></td>
      <td>Payment past due / grace period notice</td>
      <td>
        • Posts discussion note with plain text amount due & due date<br>
        • Includes direct payment portal link<br>
        • <em>Note: No account label is applied per agency guidelines</em>
      </td>
      <td>Logged to Notes</td>
    </tr>
    <tr>
      <td><strong>Agreement Signed</strong></td>
      <td>Client signs agreement & completes checkout</td>
      <td>
        • Matches EZLynx account<br>
        • Posts agreement signed discussion note with down payment breakdown<br>
        • Creates high-priority task: <code>🚨 READY TO BIND: [Policy #] - [Carrier] - [Insured]</code><br>
        • Sends instant Google Chat card alert to team
      </td>
      <td><strong>CSR / Producer</strong></td>
    </tr>
    <tr>
      <td><strong>Reinstatement Payment</strong></td>
      <td>Client pays past-due invoice on cancelled policy</td>
      <td>
        • Posts reinstatement paid note with receipt link<br>
        • Creates high-priority task: <code>🚨 REINSTATEMENT PAID: Request Carrier Reinstatement</code><br>
        • <strong>Auto-drafts carrier email</strong> ready to copy/send with payment proof
      </td>
      <td><strong>CSR / Producer</strong></td>
    </tr>
    <tr>
      <td><strong>Supplier Payout Discrepancy</strong></td>
      <td>Wholesaler payout in status <code>failed</code> or <code>unpaid</code></td>
      <td>
        • Creates high-priority audit task in EZLynx with Ascend payout ID, wholesaler name, and ledger link<br>
        • <em>Note: Suppressed from Google Chat per agency directive</em>
      </td>
      <td><strong>Markley1</strong> (Accounting)</td>
    </tr>
  </tbody>
</table>

<h2>3. Accounting & Finance Workflows (<code>Markley1</code>)</h2>

<div class="callout-warning">
  <strong>⚠️ Attention Accounting Team (<code>Markley1</code>):</strong><br>
  All Ascend supplier/wholesaler payout issues (e.g., failed ACH remittances to wholesalers like XPT Specialty, TAPCO, RT Specialty, or Hull & Co.) are automatically flagged as audit tasks in EZLynx assigned directly to <strong><code>Markley1</code></strong>.
</div>

<ul>
  <li><strong>Task Title Format:</strong> <code>⚠️ ACCOUNTING AUDIT: FAILED Supplier Payout to [Wholesaler] ($Amount)</code></li>
  <li><strong>What to Do When Assigned a Task:</strong>
    <ol>
      <li>Open the task in EZLynx to view the failure reason, scheduled remittance date, and Ascend payout ID.</li>
      <li>Click the direct Ascend payout ledger link embedded in the task notes.</li>
      <li>Verify wholesaler banking details or retry remittance in Ascend.</li>
      <li>Once resolved, close the EZLynx task.</li>
    </ol>
  </li>
  <li><strong>QuickBooks Online (QBO) Integration:</strong> Robie automatically creates <code>Deposit</code> records for agency commission payouts and prepares <code>Bill</code> / <code>BillPayment</code> entries for wholesaler settlements.</li>
</ul>

<h2>4. Frequently Asked Questions (FAQ)</h2>

<p><strong>Q: What if the quote includes terrorism coverage, but the document shows two options (with and without TRIA)?</strong><br>
A: Robie's dual-option detector will recognize that TRIA is optional and will explicitly ask you whether to include terrorism coverage before generating the agreement.</p>

<p><strong>Q: What is the default agency fee if I don't specify one?</strong><br>
A: The standard agency fee default is <code>$350.00</code>. If a policy has a different fee (or $0.00 fee), simply tell Robie in your email or reply.</p>

<p><strong>Q: Can Robie handle mid-term policy endorsements?</strong><br>
A: Yes. You can send endorsement documents to Robie. Robie extracts the additional premium and endorsement description and attaches the billable directly to the customer's existing Ascend financing program.</p>

<p><strong>Q: Who do I contact if I notice an issue with an Ascend agreement or sync?</strong><br>
A: Reach out to Carlo Ferrara or post in the engineering support channel. All syncs and operations are backed by immutable audit logs on host <code>hermes-poc-01</code>.</p>

<div class="footer">
  StreetSmart Insurance Agency &bull; Autonomous Operations System &bull; Document Reference: SS-ASCEND-REL-2026-09
</div>

</body>
</html>
"""


def main():
    with open(TOKEN_PATH, "r", encoding="utf-8") as f:
        token_data = json.load(f)
    creds = Credentials.from_authorized_user_info(token_data)
    drive_service = build("drive", "v3", credentials=creds)

    file_metadata = {
        "name": "StreetSmart Insurance — Ascend API Automation & Robie AI Release Guide",
        "mimeType": "application/vnd.google-apps.document",
    }

    media = MediaIoBaseUpload(
        io.BytesIO(HTML_CONTENT.encode("utf-8")),
        mimetype="text/html",
        resumable=True
    )

    logging.info("Creating Google Doc from HTML...")
    file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, name, webViewLink"
    ).execute()

    file_id = file.get("id")
    web_link = file.get("webViewLink")
    logging.info("Created Google Doc ID: %s", file_id)
    logging.info("View Link: %s", web_link)

    # Share with domain / anyone with link
    try:
        drive_service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
        ).execute()
        logging.info("Document permissions updated: accessible to team via link.")
    except Exception as exc:
        logging.warning("Could not set anyone permission: %s", exc)

    print(json.dumps({
        "status": "SUCCESS",
        "file_id": file_id,
        "title": file.get("name"),
        "url": web_link
    }, indent=2))


if __name__ == "__main__":
    main()
