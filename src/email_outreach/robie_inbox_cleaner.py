"""Robie Daily Inbox Cleaner & Reporter.

Scans robie@streetsmart.insurance, automatically identifies and trashes disposable noise
(mailer-daemon bounces, system delivery notices, test loops), categorizes authentic inbound
messages (renewals, quotes, team coordination, portal registrations), and sends an executive
daily summary report directly to Carlo.
"""

import argparse
import datetime
import html
import logging
import os
import sys
from typing import Any, Dict, List, Optional

from src.email_outreach.gmail_client import GmailRenewalClient

logger = logging.getLogger("robie_cleaner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

ROBIE_EMAIL = "robie@streetsmart.insurance"

NOISE_QUERIES = [
    ("Mailer-Daemon Bounces", "from:mailer-daemon"),
    ("Postmaster Delivery Notices", "from:postmaster"),
    ("Automated Delivery Failures", "subject:\"Delivery Status Notification (Failure)\""),
    ("Test Suite Remnants", "subject:\"David Clark\"")
]

def parse_headers(payload: Dict[str, Any]) -> Dict[str, str]:
    headers = {}
    for h in payload.get("headers", []):
        headers[h.get("name", "").lower()] = h.get("value", "")
    return headers

def categorize_message(sender: str, subject: str) -> str:
    s_low = sender.lower()
    sub_low = subject.lower()

    if any(k in s_low or k in sub_low for k in ["tapco", "coterie", "hartford", "foremost", "next insurance", "portal", "access application"]):
        return "Carrier Portals & Onboarding"
    if any(k in s_low or k in sub_low for k in ["quote", "renewal", "underwrit", "binder", "policy", "premium"]):
        return "Active Renewals & Underwriter Quotes"
    if any(d in s_low for d in ["@streetsmart.insurance"]):
        return "Internal Team Coordination"
    if "ezlynx" in s_low or "applied" in s_low or "ezlynx" in sub_low:
        return "EZLynx System & Tickets"
    return "General Communications"

def run_daily_inbox_cleanup_and_report(
    client: Optional[GmailRenewalClient] = None,
    recipient: str = "carlo@streetsmart.insurance",
    dry_run: bool = False,
    days_to_check: int = 2
) -> Dict[str, Any]:
    """Cleans out noise in Robie's mailbox and emails a categorized daily report."""
    if client is None:
        client = GmailRenewalClient()

    service = client.inbox_services.get(ROBIE_EMAIL)
    if not service:
        logger.error(f"Cannot access Gmail API service for {ROBIE_EMAIL}")
        return {"error": "Gmail service unavailable", "noise_trashed": 0, "real_messages_count": 0, "report_sent": False}

    logger.info(f"Starting daily inbox audit for {ROBIE_EMAIL} (dry_run={dry_run})...")

    # 1. Purge Noise
    total_noise_trashed = 0
    noise_breakdown = {}

    for label, query in NOISE_QUERIES:
        try:
            res = service.users().messages().list(userId="me", q=query, maxResults=500).execute()
            messages = res.get("messages", [])
            count = len(messages)
            noise_breakdown[label] = count

            if count > 0:
                logger.info(f"Found {count} messages matching noise category: {label}")
                for m in messages:
                    if not dry_run:
                        try:
                            service.users().messages().trash(userId="me", id=m["id"]).execute()
                            total_noise_trashed += 1
                        except Exception as e:
                            logger.warning(f"Error trashing message {m['id']}: {e}")
                    else:
                        total_noise_trashed += 1
        except Exception as e:
            logger.error(f"Error checking noise query '{query}': {e}")
            noise_breakdown[label] = 0

    logger.info(f"Noise cleanup complete: {total_noise_trashed} messages moved to trash.")

    # 2. Gather Real & Actionable Messages
    real_messages: List[Dict[str, Any]] = []
    try:
        # Check messages received within days_to_check or unread
        q_real = f"newer_than:{days_to_check}d"
        res_real = service.users().messages().list(userId="me", q=q_real, maxResults=200).execute()
        candidates = res_real.get("messages", [])

        for c in candidates:
            try:
                msg_data = service.users().messages().get(userId="me", id=c["id"], format="full").execute()
                payload = msg_data.get("payload", {})
                headers = parse_headers(payload)

                sender = headers.get("from", "Unknown")
                subject = headers.get("subject", "(No Subject)")
                date_str = headers.get("date", "")
                snippet = msg_data.get("snippet", "")

                # Filter out Robie's self-sent audit reports or noise remnants
                if ROBIE_EMAIL.lower() in sender.lower() and "audit" in subject.lower():
                    continue
                if "mailer-daemon" in sender.lower() or "postmaster" in sender.lower():
                    continue

                # Filter out automatic bounce-backs / out-of-office notices
                sub_clean = subject.strip().lower()
                if any(sub_clean.startswith(p) for p in [
                    "automatic reply:", "auto:", "auto reply:", "out of office:", "undeliverable:",
                    "delivery status notification", "failure notice"
                ]):
                    noise_breakdown["Automated Server Auto-Replies"] = noise_breakdown.get("Automated Server Auto-Replies", 0) + 1
                    total_noise_trashed += 1
                    continue

                category = categorize_message(sender, subject)

                real_messages.append({
                    "id": c["id"],
                    "from": sender,
                    "subject": subject,
                    "date": date_str,
                    "category": category,
                    "snippet": snippet
                })
            except Exception as e:
                logger.debug(f"Error fetching message details for {c['id']}: {e}")
    except Exception as e:
        logger.error(f"Error querying recent messages: {e}")

    logger.info(f"Gathered {len(real_messages)} authentic messages for reporting.")

    # 2b. Pull Underwriter Responses & Carrier Decisions from Database
    carrier_actions = []
    try:
        db_paths = ["data/renewals.db", "/opt/renewal-automation-system/data/renewals.db"]
        active_db = next((p for p in db_paths if os.path.exists(p)), None)
        if active_db:
            import sqlite3
            conn = sqlite3.connect(active_db)
            cur = conn.cursor()
            cur.execute("""
                SELECT p.insured_name, p.policy_number, p.carrier_name, p.status, a.note_text, a.created_at, a.synced_to_ezlynx, p.applicant_id
                FROM audit_note_logs a
                JOIN policy_renewals p ON a.policy_id = p.id
                WHERE (p.status LIKE '%NON_RENEWAL%' OR a.note_text LIKE '%UNDERWRITER%' OR a.note_text LIKE '%NON-RENEWAL%' OR a.note_text LIKE '%Carrier Response%')
                ORDER BY a.id DESC LIMIT 10
            """)
            for row in cur.fetchall():
                carrier_actions.append({
                    "insured_name": row[0],
                    "policy_number": row[1],
                    "carrier_name": row[2],
                    "status": row[3],
                    "summary": row[4].split("\n")[0][:120],
                    "timestamp": row[5],
                    "synced": bool(row[6]),
                    "applicant_id": row[7]
                })
            conn.close()
    except Exception as e:
        logger.warning(f"Error querying carrier actions from db: {e}")

    # 3. Build HTML & Text Report
    now_str = datetime.datetime.now().strftime("%B %d, %Y")
    
    text_lines = [
        f"Robie Daily Mailbox Audit & Cleanup Report - {now_str}",
        "=" * 60,
        f"Mailbox: {ROBIE_EMAIL}",
        f"Noise Purged to Trash: {total_noise_trashed}",
        f"Real Inbound Messages: {len(real_messages)}",
        "-" * 60,
        "Noise Categories Purged:"
    ]
    for label, cnt in noise_breakdown.items():
        text_lines.append(f"  • {label}: {cnt}")

    text_lines.append("-" * 60)
    text_lines.append("Authentic Messages Ingested:")
    for m in real_messages:
        text_lines.append(f"  [{m['category']}] From: {m['from']} | Subject: {m['subject']}")
        text_lines.append(f"    Snippet: {m['snippet'][:120]}...")

    text_report = "\n".join(text_lines)

    # HTML formatting
    rows_html = ""
    for m in real_messages:
        cat_badge = f"<span style='background:#e0f2fe; color:#0369a1; padding:2px 8px; border-radius:12px; font-size:12px;'>{html.escape(m['category'])}</span>"
        rows_html += f"""
        <tr style="border-bottom: 1px solid #e5e7eb;">
          <td style="padding: 10px 12px; font-size: 13px;">{cat_badge}</td>
          <td style="padding: 10px 12px; font-size: 13px; font-weight: 500;">{html.escape(m['from'])}</td>
          <td style="padding: 10px 12px; font-size: 13px;">
            <strong>{html.escape(m['subject'])}</strong><br>
            <span style="color: #6b7280; font-size: 12px;">{html.escape(m['snippet'][:160])}</span>
          </td>
          <td style="padding: 10px 12px; font-size: 12px; color: #6b7280; white-space: nowrap;">{html.escape(m['date'])}</td>
        </tr>
        """

    if not rows_html:
        rows_html = "<tr><td colspan='4' style='padding:16px; text-align:center; color:#6b7280;'>No new inbound business messages in the past period.</td></tr>"

    noise_items_html = "".join([f"<li><strong>{html.escape(k)}:</strong> {v} purged</li>" for k, v in noise_breakdown.items() if v > 0] or ["<li>No noise messages detected today.</li>"])

    carrier_actions_html = ""
    for ca in carrier_actions:
        ez_link = f"https://app.ezlynx.com/web/account/{ca['applicant_id']}/activity"
        sync_badge = "<span style='background:#dcfce7; color:#166534; padding:2px 6px; border-radius:10px; font-size:11px;'>✅ Synced</span>" if ca["synced"] else "<span style='background:#fef3c7; color:#92400e; padding:2px 6px; border-radius:10px; font-size:11px;'>⏳ Queued</span>"
        status_badge = f"<span style='background:#fee2e2; color:#991b1b; padding:2px 6px; border-radius:10px; font-size:11px;'>{html.escape(ca['status'])}</span>" if "NON_RENEWAL" in ca["status"] else f"<span style='background:#e0f2fe; color:#075985; padding:2px 6px; border-radius:10px; font-size:11px;'>{html.escape(ca['status'])}</span>"
        
        carrier_actions_html += f"""
        <tr style="border-bottom: 1px solid #e5e7eb;">
          <td style="padding: 10px 12px; font-size: 13px;"><a href="{ez_link}" style="color:#2563eb; text-decoration:none; font-weight:600;">{html.escape(ca['insured_name'])}</a><br><span style="color:#6b7280; font-size:11px;">Pol #{html.escape(ca['policy_number'])}</span></td>
          <td style="padding: 10px 12px; font-size: 13px;">{html.escape(ca['carrier_name'])}</td>
          <td style="padding: 10px 12px; font-size: 13px;">{status_badge}</td>
          <td style="padding: 10px 12px; font-size: 12px; color:#374151;">{html.escape(ca['summary'])}</td>
          <td style="padding: 10px 12px; font-size: 12px;">{sync_badge}</td>
        </tr>
        """
    if not carrier_actions_html:
        carrier_actions_html = "<tr><td colspan='5' style='padding:14px; text-align:center; color:#6b7280;'>No carrier decisions or non-renewals logged in the past 48 hours.</td></tr>"

    html_report = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 800px; margin: 0 auto; color: #1f2937;">
      <div style="background: linear-gradient(135deg, #1e3a8a, #2563eb); padding: 24px; border-radius: 8px 8px 0 0; color: white;">
        <h2 style="margin: 0; font-size: 22px;">Robie Daily Mailbox Audit &amp; Cleanup</h2>
        <p style="margin: 4px 0 0; opacity: 0.9; font-size: 14px;">Daily Health &amp; Inbound Report &bull; {now_str}</p>
      </div>

      <div style="background: #f9fafb; padding: 18px 24px; border: 1px solid #e5e7eb; border-top: none;">
        <div style="display: flex; gap: 16px; margin-bottom: 16px;">
          <div style="flex: 1; background: white; padding: 14px; border-radius: 6px; border: 1px solid #e5e7eb;">
            <div style="font-size: 12px; color: #6b7280; text-transform: uppercase; font-weight: 600;">Noise Purged (Trash)</div>
            <div style="font-size: 24px; font-weight: bold; color: #dc2626; margin-top: 4px;">{total_noise_trashed}</div>
          </div>
          <div style="flex: 1; background: white; padding: 14px; border-radius: 6px; border: 1px solid #e5e7eb;">
            <div style="font-size: 12px; color: #6b7280; text-transform: uppercase; font-weight: 600;">Actionable Inbound Messages</div>
            <div style="font-size: 24px; font-weight: bold; color: #059669; margin-top: 4px;">{len(real_messages)}</div>
          </div>
        </div>

        <div style="background: white; padding: 16px; border-radius: 6px; border: 1px solid #e5e7eb; margin-bottom: 20px;">
          <h4 style="margin: 0 0 8px; font-size: 14px; color: #374151;">🧹 Purge Breakdown:</h4>
          <ul style="margin: 0; padding-left: 20px; font-size: 13px; color: #4b5563;">
            {noise_items_html}
          </ul>
        </div>

        <h3 style="margin: 20px 0 10px; font-size: 16px; color: #111827;">📬 Carrier Decisions &amp; Underwriter Responses (Last 48 Hours)</h3>
        <table style="width: 100%; border-collapse: collapse; background: white; border-radius: 6px; overflow: hidden; border: 1px solid #e5e7eb; margin-bottom: 24px;">
          <thead>
            <tr style="background: #f3f4f6; text-align: left;">
              <th style="padding: 10px 12px; font-size: 12px; color: #4b5563;">Insured Account</th>
              <th style="padding: 10px 12px; font-size: 12px; color: #4b5563;">Carrier</th>
              <th style="padding: 10px 12px; font-size: 12px; color: #4b5563;">Status</th>
              <th style="padding: 10px 12px; font-size: 12px; color: #4b5563;">Underwriter Summary</th>
              <th style="padding: 10px 12px; font-size: 12px; color: #4b5563;">EZLynx</th>
            </tr>
          </thead>
          <tbody>
            {carrier_actions_html}
          </tbody>
        </table>

        <h3 style="margin: 20px 0 10px; font-size: 16px; color: #111827;">📬 Genuine Inbound Messages (Last 48 Hours)</h3>
        <table style="width: 100%; border-collapse: collapse; background: white; border-radius: 6px; overflow: hidden; border: 1px solid #e5e7eb;">
          <thead>
            <tr style="background: #f3f4f6; text-align: left;">
              <th style="padding: 10px 12px; font-size: 12px; color: #4b5563;">Category</th>
              <th style="padding: 10px 12px; font-size: 12px; color: #4b5563;">From</th>
              <th style="padding: 10px 12px; font-size: 12px; color: #4b5563;">Subject &amp; Snippet</th>
              <th style="padding: 10px 12px; font-size: 12px; color: #4b5563;">Date</th>
            </tr>
          </thead>
          <tbody>
            {rows_html}
          </tbody>
        </table>

        <div style="margin-top: 24px; padding-top: 14px; border-top: 1px solid #e5e7eb; font-size: 12px; color: #9ca3af; text-align: center;">
          Autonomous Cleaned &amp; Reported by Robie AI Engine &bull; StreetSmart Insurance
        </div>
      </div>
    </div>
    """

    # 4. Dispatch Email Report
    report_sent = False
    subject_line = f"Robie Daily Mailbox Audit & Cleanup Report - {now_str} ({len(real_messages)} active, {total_noise_trashed} purged)"

    if not dry_run and recipient:
        try:
            send_res = client.send_email(
                to_email=recipient,
                subject=subject_line,
                body_text=text_report,
                html_body=html_report
            )
            logger.info(f"Successfully dispatched daily audit report to {recipient} (ID: {send_res.get('id')})")
            report_sent = True
        except Exception as e:
            logger.error(f"Failed to dispatch daily report email to {recipient}: {e}")
    else:
        logger.info(f"Dry run enabled or no recipient; skipped emailing report.")

    return {
        "noise_trashed": total_noise_trashed,
        "noise_breakdown": noise_breakdown,
        "real_messages_count": len(real_messages),
        "real_messages": real_messages,
        "report_sent": report_sent
    }

def main():
    parser = argparse.ArgumentParser(description="Run Robie daily mailbox cleanup and report.")
    parser.add_argument("--recipient", default="carlo@streetsmart.insurance", help="Email recipient for daily report.")
    parser.add_argument("--dry-run", action="store_true", help="Audit without deleting or sending email.")
    parser.add_argument("--days", type=int, default=2, help="Number of days lookback for authentic emails.")
    args = parser.parse_args()

    result = run_daily_inbox_cleanup_and_report(
        recipient=args.recipient,
        dry_run=args.dry_run,
        days_to_check=args.days
    )
    print(f"\nExecution Finished: {result['noise_trashed']} noise trashed, {result['real_messages_count']} real messages found. Report sent: {result['report_sent']}")

if __name__ == "__main__":
    main()
