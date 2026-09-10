import os
import base64
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from googleapiclient.discovery import build
from src.email_outreach.auth_setup import get_service_account_credentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("send_policy_confirmation")

def create_email_html():
    return """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #1f2937; line-height: 1.6; margin: 0; padding: 20px; background-color: #f9fafb; }
  .container { max-width: 650px; margin: 0 auto; background: #ffffff; border-radius: 8px; border: 1px solid #e5e7eb; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
  .header { background-color: #1e3a8a; color: #ffffff; padding: 24px; text-align: left; }
  .header h1 { margin: 0; font-size: 20px; font-weight: 600; letter-spacing: -0.025em; }
  .header p { margin: 4px 0 0 0; font-size: 13px; color: #93c5fd; }
  .content { padding: 24px; }
  .badge { display: inline-block; padding: 4px 10px; font-size: 12px; font-weight: 600; border-radius: 9999px; background-color: #dcfce7; color: #166534; margin-bottom: 16px; }
  .section-title { font-size: 15px; font-weight: 700; color: #111827; border-bottom: 1px solid #e5e7eb; padding-bottom: 6px; margin-top: 20px; margin-bottom: 12px; }
  .details-table { width: 100%; border-collapse: collapse; margin-bottom: 16px; font-size: 14px; }
  .details-table td { padding: 8px 10px; vertical-align: top; }
  .details-table tr:nth-child(even) { background-color: #f8fafc; }
  .label { font-weight: 600; color: #4b5563; width: 38%; }
  .value { color: #111827; }
  .alert-box { background-color: #eff6ff; border-left: 4px solid #3b82f6; padding: 12px 16px; border-radius: 4px; margin: 16px 0; font-size: 13px; color: #1e40af; }
  .footer { background-color: #f3f4f6; padding: 20px 24px; font-size: 12px; color: #6b7280; border-top: 1px solid #e5e7eb; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>StreetSmart Insurance</h1>
    <p>Policy Change Confirmation &amp; Coverage Summary</p>
  </div>
  <div class="content">
    <div class="badge">&#10003; Change Successfully Processed &amp; Bound</div>
    <p>Dear Jesus,</p>
    <p>We are pleased to confirm that your requested vehicle addition on your <strong>National General Personal Auto Policy</strong> has been officially processed and bound with the carrier effective <strong>September 8, 2026</strong>.</p>
    
    <div class="section-title">Vehicle &amp; Policy Details</div>
    <table class="details-table">
      <tr>
        <td class="label">Policyholder Name:</td>
        <td class="value"><strong>Jesus Solano</strong></td>
      </tr>
      <tr>
        <td class="label">Policy Number:</td>
        <td class="value"><strong>203193685900</strong> (Carrier: Integon National / National General)</td>
      </tr>
      <tr>
        <td class="label">Transaction:</td>
        <td class="value">Added Vehicle (Endorsement #12)</td>
      </tr>
      <tr>
        <td class="label">Effective Date:</td>
        <td class="value"><strong>September 8, 2026</strong></td>
      </tr>
      <tr>
        <td class="label">Added Vehicle:</td>
        <td class="value"><strong>2005 Ford Econoline E350 Super Duty Wagon</strong></td>
      </tr>
      <tr>
        <td class="label">VIN:</td>
        <td class="value"><span style="font-family: monospace; font-size: 13px;">1FBSS31L05HB40186</span></td>
      </tr>
      <tr>
        <td class="label">Coverages:</td>
        <td class="value"><strong>Liability Only</strong><br>
        &bull; Bodily Injury: $100,000 / $300,000<br>
        &bull; Property Damage: $50,000<br>
        &bull; NJ PIP Medical: $250,000 ($250 Deductible)<br>
        &bull; Comp / Collision: None (Liability Only)</td>
      </tr>
      <tr>
        <td class="label">Garaging Address:</td>
        <td class="value">55 Ford Rd, Howell, NJ 07731-2416</td>
      </tr>
      <tr>
        <td class="label">Drivers:</td>
        <td class="value">Jesus Solano (No new drivers added)</td>
      </tr>
    </table>

    <div class="section-title">Premium Adjustment &amp; Billing</div>
    <table class="details-table">
      <tr>
        <td class="label">Pro-Rated Change Amount:</td>
        <td class="value"><strong style="color: #047857;">+$227.28</strong> ($208.00 vehicle premium + $19.28 state &amp; policy fees)</td>
      </tr>
      <tr>
        <td class="label">Revised Term Premium:</td>
        <td class="value"><strong>$12,140.31</strong> (adjusted from $10,686.03)</td>
      </tr>
      <tr>
        <td class="label">Billing Method:</td>
        <td class="value">Direct Bill with National General. Your upcoming monthly installment statements will reflect this pro-rated adjustment.</td>
      </tr>
    </table>

    <div class="alert-box">
      <strong>Auto Insurance ID Cards:</strong><br>
      Your official electronic Auto Insurance Identification Card for the 2005 Ford Econoline has been generated and dispatched directly from National General to your email (<code>solano1777@gmail.com</code>). Please save a copy to your mobile device or print a copy for your glove compartment.
    </div>

    <p>If you have any questions, need temporary paper cards re-sent, or would like to discuss additional coverages, please do not hesitate to contact our office.</p>

    <p style="margin-top: 24px;">Sincerely,</p>
    <p style="margin: 0; font-weight: 600; color: #111827;">Jazmin Molina</p>
    <p style="margin: 0; font-size: 13px; color: #4b5563;">Assigned Producer | StreetSmart Insurance</p>
    <p style="margin: 2px 0 0 0; font-size: 13px; color: #6b7280;">Direct: (732) 462-8343 | Email: jazmin@streetsmart.insurance</p>
    <p style="margin: 2px 0 0 0; font-size: 13px; color: #6b7280;">CSR Follow-Up: Ana Flores (ana@streetsmart.insurance)</p>
  </div>
  <div class="footer">
    StreetSmart Risk Managers Inc. dba StreetSmart Insurance &bull; Licensed Insurance Agency &bull; (732) 462-8343
  </div>
</div>
</body>
</html>
"""

def create_email_plain():
    return """Dear Jesus,

We are pleased to confirm that your requested vehicle addition on your National General Personal Auto Policy has been officially processed and bound effective September 8, 2026.

POLICY CHANGE SUMMARY:
- Policyholder: Jesus Solano
- Policy Number: 203193685900 (National General / Integon National)
- Added Vehicle: 2005 Ford Econoline E350 Super Duty Wagon
- VIN: 1FBSS31L05HB40186
- Effective Date: September 8, 2026
- Coverage: Liability Only (BI $100k/$300k, PD $50k, NJ PIP $250k w/ $250 ded; No Comprehensive, No Collision)
- Garaging: 55 Ford Rd, Howell, NJ 07731
- Drivers: Jesus Solano (no new drivers added)

PREMIUM & BILLING ADJUSTMENT:
- Additional Pro-Rated Premium: +$227.28 ($208.00 vehicle premium + $19.28 fees)
- Revised Total Term Premium: $12,140.31
- Billing Method: Direct Bill with National General. Your upcoming monthly automatic installment will reflect this adjustment.

AUTO INSURANCE ID CARDS:
Your official electronic Auto Insurance Identification Card has been issued and emailed directly from National General to solano1777@gmail.com. Please save a copy to your phone or keep a printed copy in the vehicle.

If you have any questions or need anything else, please let us know!

Sincerely,
Jazmin Molina
Personal Lines Account Producer | StreetSmart Insurance
Direct: (732) 462-8343 | jazmin@streetsmart.insurance
CSR Follow-Up: Ana Flores (ana@streetsmart.insurance)
"""

def send():
    sender_email = "jazmin@streetsmart.insurance"
    to_email = "solano1777@gmail.com"
    cc_list = ["carlo@streetsmart.insurance", "ana@streetsmart.insurance"]
    subject = "StreetSmart Insurance: Policy Change Confirmation & Auto ID Card - Jesus Solano - Policy #203193685900 (Added 2005 Ford Econoline E350)"

    logger.info(f"Authenticating as {sender_email} via Domain-Wide Delegation...")
    creds = get_service_account_credentials(sender_email)
    if not creds:
        raise RuntimeError(f"Could not obtain service account credentials for {sender_email}")

    service = build("gmail", "v1", credentials=creds)

    msg = MIMEMultipart("alternative")
    msg["From"] = f"Jazmin Molina - StreetSmart Insurance <{sender_email}>"
    msg["To"] = to_email
    msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject

    plain_part = MIMEText(create_email_plain(), "plain", "utf-8")
    html_part = MIMEText(create_email_html(), "html", "utf-8")
    msg.attach(plain_part)
    msg.attach(html_part)

    raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    send_body = {"raw": raw_message}

    logger.info(f"Sending email to {to_email} with CC {cc_list}...")
    sent = service.users().messages().send(userId="me", body=send_body).execute()
    logger.info(f"Successfully sent confirmation email! Message ID: {sent.get('id')}, Thread ID: {sent.get('threadId')}")
    return sent

if __name__ == "__main__":
    send()
