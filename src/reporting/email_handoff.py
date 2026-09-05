"""Automated email delivery of the Daily Handoff Report to StreetSmart leadership and team."""

import os
import logging
from datetime import date, datetime
from typing import List, Optional, Any, Dict, Union
import markdown

from src.email_outreach.gmail_client import GmailRenewalClient
from src.reporting.daily_handoff import DailyHandoffReporter

logger = logging.getLogger("email_handoff")

HANDOFF_RECIPIENT_LIST = [
    "carlo@streetsmart.insurance",
    "jake@streetsmart.insurance",
    "gabrielac@streetsmart.insurance",
    "sandy@streetsmart.insurance",
    "ashley@streetsmart.insurance",
]

def send_daily_handoff_email(
    report_markdown: str,
    report_date: Optional[date] = None,
    recipients: Optional[List[str]] = None,
    sender: str = "robie@streetsmart.insurance"
) -> dict:
    """
    Renders the daily handoff markdown into HTML and sends it via Gmail API
    from robie@streetsmart.insurance to the designated team distribution list.
    """
    if not report_date:
        report_date = date.today()
    if not recipients:
        recipients = HANDOFF_RECIPIENT_LIST

    date_str = report_date.strftime("%B %d, %Y")
    subject = f"StreetSmart Autonomous Renewal Daily Handoff - {date_str}"

    # Convert markdown to clean email-friendly HTML
    html_body = markdown.markdown(report_markdown, extensions=['tables', 'fenced_code'])
    
    # Wrap in clean responsive email container
    styled_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #2d3748; padding: 20px; }}
            h1 {{ color: #1a365d; border-bottom: 2px solid #e2e8f0; padding-bottom: 8px; }}
            h2 {{ color: #2b6cb0; margin-top: 24px; border-bottom: 1px solid #edf2f7; padding-bottom: 6px; }}
            h3 {{ color: #2d3748; margin-top: 18px; }}
            table {{ border-collapse: collapse; width: 100%; margin: 16px 0; font-size: 14px; }}
            th, td {{ border: 1px solid #cbd5e0; padding: 8px 12px; text-align: left; }}
            th {{ background-color: #f7fafc; font-weight: 600; color: #4a5568; }}
            tr:nth-child(even) {{ background-color: #f8fafc; }}
            code {{ background-color: #edf2f7; padding: 2px 6px; border-radius: 4px; font-family: monospace; font-size: 13px; }}
            a {{ color: #3182ce; text-decoration: none; font-weight: 500; }}
            a:hover {{ text-decoration: underline; }}
            blockquote {{ border-left: 4px solid #4299e1; margin: 0; padding-left: 12px; color: #718096; }}
            .footer {{ margin-top: 30px; font-size: 12px; color: #a0aec0; border-top: 1px solid #e2e8f0; padding-top: 12px; }}
        </style>
    </head>
    <body>
        {html_body}
        <div class="footer">
            <p>Sent autonomously by Robie • StreetSmart Insurance Automation Engine</p>
        </div>
    </body>
    </html>
    """

    client = GmailRenewalClient()
    
    logger.info(f"Dispatching Daily Handoff Report to: {', '.join(recipients)}")
    result = client.send_email(
        to_email=recipients[0],
        cc=recipients[1:] if len(recipients) > 1 else None,
        subject=subject,
        body_text=report_markdown,
        html_body=styled_html
    )
    return result

def notify_csr_of_underwriter_reply(
    policy: Any,
    classification: Any,
    sender: str,
    attachments: Optional[List[Dict[str, Any]]] = None,
    client: Optional[GmailRenewalClient] = None,
    note_synced: bool = False,
    task_created: bool = False,
    note_id: Optional[Union[str, int]] = None,
    clean_reply_text: Optional[str] = None
) -> dict:
    """
    Sends an immediate high-priority email notification to the assigned CSR (CC Carlo and Jake)
    when an underwriter response is received. Accurately reports whether discussion note / task
    has been posted to EZLynx via REST API or is pending live browser sync.
    """
    from src.email_outreach.thread_tracker import resolve_outreach_cc_list, CSR_EMAIL_DIRECTORY

    csr_email = None
    if policy.assigned_agent:
        csr_email = CSR_EMAIL_DIRECTORY.get(policy.assigned_agent.strip())
        if not csr_email:
            name_parts = policy.assigned_agent.replace(",", " ").split()
            for part in name_parts:
                part_clean = part.strip().lower()
                for key, email in CSR_EMAIL_DIRECTORY.items():
                    if part_clean in key.lower():
                        csr_email = email
                        break
                if csr_email:
                    break

    if not csr_email:
        csr_email = "sandy@streetsmart.insurance"

    client = client or GmailRenewalClient()
    subject = f"[URGENT / CSR ACTION] Carrier Response Received: {policy.insured_name} - Pol #{policy.policy_number}"

    app_id = policy.applicant_id or ""
    ezlynx_url = f"https://app.ezlynx.com/web/account/{app_id}/activity" if app_id else "https://app.ezlynx.com"

    att_info = ""
    if attachments:
        att_names = [a.get("filename", "document.pdf") for a in attachments]
        att_info = f"\n• Attached Documents: {', '.join(att_names)}"

    underwriter_msg_section = f'\n• Underwriter Message:\n  "{clean_reply_text}"' if clean_reply_text else ""

    note_info = f" (Note ID: {note_id})" if note_id else ""
    if note_synced and task_created:
        ezlynx_action_text = f"✅ The note has been logged directly to the EZLynx discussion card via REST API{note_info} and a review task has been created."
    elif note_synced:
        ezlynx_action_text = f"✅ The note has been logged directly to the EZLynx discussion card via REST API{note_info}."
    else:
        ezlynx_action_text = "⏳ Underwriter email captured by Robie. Discussion card update & task creation queued for live EZLynx browser sync."

    body = (
        f"Hi {policy.assigned_agent or 'CSR'},\n\n"
        f"An underwriter response has just been received regarding upcoming renewal terms:\n\n"
        f"• Named Insured: {policy.insured_name}\n"
        f"• Policy Number: {policy.policy_number}\n"
        f"• Carrier: {policy.carrier_name}\n"
        f"• Expiration Date: {policy.expiration_date}\n"
        f"• Underwriter Sender: {sender}\n"
        f"• Status / Classification: {classification.intent}\n"
        f"• Summary: {classification.summary}{att_info}{underwriter_msg_section}\n"
        f"• EZLynx Activity: {ezlynx_url}\n\n"
        f"{ezlynx_action_text}\n\n"
        f"Best,\n"
        f"Robie\n"
        f"StreetSmart Insurance Autonomous System"
    )

    # Instant CSR alert + Carlo (existing handoff path; outbound CSR CC rules stay on outreach).
    cc_list = []
    carlo_email = "carlo@streetsmart.insurance"
    if csr_email and csr_email.lower() != carlo_email:
        cc_list.append(carlo_email)

    logger.info(f"Sending immediate underwriter reply notification to {csr_email} for Pol #{policy.policy_number}")
    return client.send_email(
        to_email=csr_email,
        cc=cc_list,
        subject=subject,
        body_text=body
    )

if __name__ == "__main__":
    import sys
    report_file = "reports/daily_handoff_2026_09_03.md"
    if os.path.exists(report_file):
        with open(report_file) as f:
            content = f.read()
        print(f"Loaded {report_file}. Ready to send.")
        # Test dry-run or send if requested
        if "--send" in sys.argv:
            res = send_daily_handoff_email(content)
            print("Email sent successfully:", res)
