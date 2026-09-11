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
import re
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from src.email_outreach.gmail_client import GmailRenewalClient
from src.email_outreach.uw_reply_filer import run_uw_reply_filing

logger = logging.getLogger("robie_cleaner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

ROBIE_EMAIL = "robie@streetsmart.insurance"
DAILY_REPORT_SUBJECT = "Robie Daily Action Required"

AUTOMATED_SUBJECT_PREFIXES = (
    "automatic reply:", "auto:", "auto reply:", "out of office:",
    "undeliverable:", "delivery status notification", "failure notice",
    "verification code", "your verification code",
)

ACTIONABLE_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\baction required\b",
    r"\b(?:urgent|deadline|due today|past due)\b",
    r"\b(?:can|could|would) you\b",
    r"\bplease\s+(?:provide|send|review|complete|sign|confirm|respond|call|advise)\b",
    r"\b(?:need|needs|needed|missing|requires?|requested?)\s+(?:information|documents?|signature|response|approval|payment)\b",
    r"\b(?:non[- ]?renewal|declin(?:e|ed|ing)|cancel(?:lation|led|ing)?|lapse[sd]?)\b",
    r"\b(?:information|info) requested\b",
    r"\b(?:quote|proposal|renewal terms?|renewal offer|binder|endorsement|certificate|inspection|audit)\s+(?:attached|required|request(?:ed)?)\b",
    r"\battached\b.{0,40}\b(?:quote|proposal|renewal|binder|endorsement|certificate|inspection|audit)\b",
))

NOISE_QUERIES = [
    ("Mailer-Daemon Bounces", "from:mailer-daemon"),
    ("Postmaster Delivery Notices", "from:postmaster"),
    ("Automated Delivery Failures", "subject:\"Delivery Status Notification (Failure)\""),
    ("Automatic Replies & Out of Office", "subject:\"Automatic reply:\" OR subject:\"Out of Office\" OR subject:\"Auto-Reply:\" OR subject:\"Auto:\" OR subject:\"Undeliverable:\""),
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


def actionable_reason(sender: str, subject: str, snippet: str) -> Optional[str]:
    """Return the concrete action signal, or ``None`` for informational mail.

    This intentionally errs toward silence.  A message that merely belongs to a
    business category is not actionable; it must contain an explicit request,
    deadline, or coverage/policy risk signal.
    """
    sender_low = (sender or "").casefold()
    subject_low = (subject or "").strip().casefold()
    if ROBIE_EMAIL in sender_low:
        return None
    if "mailer-daemon" in sender_low or "postmaster" in sender_low:
        return None
    if any(subject_low.startswith(prefix) for prefix in AUTOMATED_SUBJECT_PREFIXES):
        return None
    text = f"{subject or ''}\n{snippet or ''}"
    for pattern in ACTIONABLE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0).strip()
    return None


def daily_report_already_sent(service: Any, now_et: datetime.datetime) -> bool:
    """Check Robie's Sent mailbox so hourly retries cannot send a second digest."""
    today = now_et.strftime("%Y/%m/%d")
    tomorrow = (now_et.date() + datetime.timedelta(days=1)).strftime("%Y/%m/%d")
    query = (
        f'in:sent subject:"{DAILY_REPORT_SUBJECT}" '
        f"after:{today} before:{tomorrow}"
    )
    result = service.users().messages().list(userId="me", q=query, maxResults=1).execute()
    return bool(result.get("messages"))

def run_daily_inbox_cleanup_and_report(
    client: Optional[GmailRenewalClient] = None,
    recipient: str = "carlo@streetsmart.insurance",
    dry_run: bool = False,
    days_to_check: int = 1,
    daily_report_hour: int = 8,
    force_email: bool = False,
    timezone_str: str = "America/New_York",
    now: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Clean the mailbox, file matched mail in EZLynx, and send one action digest."""
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

    # 1b. Save matched underwriter emails through the EZLynx API.  The filer is
    # fail-closed: it reuses an existing titled discussion and approved document
    # routing, or leaves the message unfiled for human attention.
    filing_result: Dict[str, Any] = {}
    try:
        filing_result = run_uw_reply_filing(gmail_client=client, dry_run=dry_run) or {}
        logger.info(
            "UW reply filing from cleaner: filed=%s skipped=%s dry_run=%s",
            filing_result.get("filed"),
            filing_result.get("skipped"),
            dry_run,
        )
    except Exception as e:
        logger.warning(f"UW reply filing skipped (cleaner continues; replies are not trashed): {e}")
        filing_result = {"error": str(e), "filed": 0, "skipped": 0}

    # 2. Gather Real & Actionable Messages
    real_messages: List[Dict[str, Any]] = []
    try:
        # Only unread inbox mail can require attention.  Previously every recent
        # message was counted as "actionable", creating the 100+ item digests.
        q_real = f"in:inbox is:unread newer_than:{days_to_check}d"
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

                reason = actionable_reason(sender, subject, snippet)
                if not reason:
                    continue

                category = categorize_message(sender, subject)

                real_messages.append({
                    "id": c["id"],
                    "from": sender,
                    "subject": subject,
                    "date": date_str,
                    "category": category,
                    "snippet": snippet,
                    "action_reason": reason,
                })
            except Exception as e:
                logger.debug(f"Error fetching message details for {c['id']}: {e}")
    except Exception as e:
        logger.error(f"Error querying recent messages: {e}")

    logger.info(f"Gathered {len(real_messages)} messages requiring human action.")

    # 2b. Pull only unresolved carrier decisions from the database. Historical
    # synced rows are evidence of completed automation, not work for Carlo.
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
                WHERE a.synced_to_ezlynx = 0
                  AND (p.status LIKE '%NON_RENEWAL%' OR a.note_text LIKE '%UNDERWRITER%' OR a.note_text LIKE '%NON-RENEWAL%' OR a.note_text LIKE '%Carrier Response%')
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
    ny_now = now or datetime.datetime.now(ZoneInfo(timezone_str))
    if ny_now.tzinfo is None:
        ny_now = ny_now.replace(tzinfo=ZoneInfo(timezone_str))
    else:
        ny_now = ny_now.astimezone(ZoneInfo(timezone_str))
    now_str = ny_now.strftime("%B %d, %Y")
    
    text_lines = [
        f"{DAILY_REPORT_SUBJECT} - {now_str}",
        "=" * 60,
        f"Items requiring action: {len(real_messages) + len(carrier_actions)}",
        f"Emails saved to EZLynx by API: {filing_result.get('saved_to_ezlynx', filing_result.get('filed', 0))}",
        "-" * 60,
        "Action required:"
    ]
    for m in real_messages:
        text_lines.append(f"  [{m['category']}] From: {m['from']} | Subject: {m['subject']}")
        text_lines.append(f"    Why: {m['action_reason']}")
        text_lines.append(f"    Snippet: {m['snippet'][:120]}...")
    for action in carrier_actions:
        text_lines.append(
            f"  [EZLynx filing pending] {action['insured_name']} | Pol #{action['policy_number']} | {action['status']}"
        )

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
            <span style="color:#b45309; font-size:12px; font-weight:600;">Action signal: {html.escape(m['action_reason'])}</span><br>
            <span style="color: #6b7280; font-size: 12px;">{html.escape(m['snippet'][:160])}</span>
          </td>
          <td style="padding: 10px 12px; font-size: 12px; color: #6b7280; white-space: nowrap;">{html.escape(m['date'])}</td>
        </tr>
        """

    if not rows_html:
        rows_html = "<tr><td colspan='4' style='padding:16px; text-align:center; color:#6b7280;'>No new inbound business messages in the past period.</td></tr>"

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
        <h2 style="margin: 0; font-size: 22px;">Robie Daily Action Required</h2>
        <p style="margin: 4px 0 0; opacity: 0.9; font-size: 14px;">Only unresolved items &bull; {now_str}</p>
      </div>

      <div style="background: #f9fafb; padding: 18px 24px; border: 1px solid #e5e7eb; border-top: none;">
        <div style="display: flex; gap: 16px; margin-bottom: 16px;">
          <div style="flex: 1; background: white; padding: 14px; border-radius: 6px; border: 1px solid #e5e7eb;">
            <div style="font-size: 12px; color: #6b7280; text-transform: uppercase; font-weight: 600;">Action Required</div>
            <div style="font-size: 24px; font-weight: bold; color: #dc2626; margin-top: 4px;">{len(real_messages) + len(carrier_actions)}</div>
          </div>
          <div style="flex: 1; background: white; padding: 14px; border-radius: 6px; border: 1px solid #e5e7eb;">
            <div style="font-size: 12px; color: #6b7280; text-transform: uppercase; font-weight: 600;">Saved to EZLynx by API</div>
            <div style="font-size: 24px; font-weight: bold; color: #059669; margin-top: 4px;">{filing_result.get('saved_to_ezlynx', filing_result.get('filed', 0))}</div>
          </div>
        </div>

        <h3 style="margin: 20px 0 10px; font-size: 16px; color: #111827;">Carrier emails not yet saved to EZLynx</h3>
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

        <h3 style="margin: 20px 0 10px; font-size: 16px; color: #111827;">Unread messages requiring action</h3>
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

    # 4. Dispatch at most once per Eastern calendar day. The hourly cleaner may
    # continue filing/cleaning in the background without emailing Carlo.
    report_sent = False
    subject_line = f"{DAILY_REPORT_SUBJECT} - {now_str} ({len(real_messages) + len(carrier_actions)} items)"
    actionable_count = len(real_messages) + len(carrier_actions)
    already_sent = False
    if not dry_run and not force_email and ny_now.hour >= daily_report_hour:
        try:
            already_sent = daily_report_already_sent(service, ny_now)
        except Exception as exc:
            logger.error("Daily digest Sent-mail dedupe failed closed: %s", exc)
            already_sent = True

    if actionable_count == 0:
        logger.info("No unresolved actionable items; daily email suppressed.")
        should_send_email = False
    elif not force_email and ny_now.hour < daily_report_hour:
        logger.info(
            f"Current time ({ny_now.strftime('%I:%M %p')} {timezone_str}) is before the "
            f"daily {daily_report_hour:02d}:00 report window; email suppressed."
        )
        should_send_email = False
    elif already_sent:
        logger.info("Today's actionable digest already exists in Sent; duplicate suppressed.")
        should_send_email = False
    else:
        should_send_email = True

    if not dry_run and recipient and should_send_email:
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
        if not should_send_email:
            logger.info("Report delivery skipped by daily/actionable gate.")
        else:
            logger.info("Dry run enabled or no recipient; skipped emailing report.")

    return {
        "noise_trashed": total_noise_trashed,
        "noise_breakdown": noise_breakdown,
        "real_messages_count": len(real_messages),
        "real_messages": real_messages,
        "report_sent": report_sent,
        "report_already_sent": already_sent,
        "uw_replies_filed": filing_result.get("filed", 0),
        "ezlynx_emails_saved": filing_result.get("saved_to_ezlynx", filing_result.get("filed", 0)),
        "uw_replies_skipped": filing_result.get("skipped", 0),
    }

def main():
    parser = argparse.ArgumentParser(description="Run Robie daily mailbox cleanup and report.")
    parser.add_argument("--recipient", default="carlo@streetsmart.insurance", help="Email recipient for daily report.")
    parser.add_argument("--dry-run", action="store_true", help="Audit without deleting or sending email.")
    parser.add_argument("--days", type=int, default=1, help="Number of days to scan for unread actionable emails.")
    parser.add_argument("--force-email", action="store_true", help="Force sending report regardless of scheduled hours.")
    parser.add_argument("--report-hour", type=int, default=8, help="Earliest Eastern hour for the once-daily report (default: 8).")
    args = parser.parse_args()

    result = run_daily_inbox_cleanup_and_report(
        recipient=args.recipient,
        dry_run=args.dry_run,
        days_to_check=args.days,
        daily_report_hour=args.report_hour,
        force_email=args.force_email,
    )
    print(f"\nExecution Finished: {result['noise_trashed']} noise trashed, {result['real_messages_count']} real messages found. Report sent: {result['report_sent']}")

if __name__ == "__main__":
    main()
