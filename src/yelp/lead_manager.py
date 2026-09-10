"""Yelp Lead Manager: Inbox scanning, SQLite tracking, and BDR alerting with Loop Prevention."""

import os
import sys
import time
import sqlite3
import logging
import argparse
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from src.yelp.extractor import extract_lead_from_message
from src.yelp.dispatcher import dispatch_bdr_alert
from src.email_outreach.auth_setup import get_carlo_gmail_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("yelp_lead_manager")

DEFAULT_DB_PATH = os.environ.get(
    "YELP_DB_PATH",
    "/opt/renewal-automation-system/data/yelp_leads.db"
)

# LOOP PREVENTION & RATE LIMITING SAFEGUARDS
MAX_ALERTS_PER_RUN = 3        # Circuit breaker: Never fire more than 3 alerts in a single run
MAX_ALERTS_PER_HOUR = 8       # Velocity limit: Prevent runaway notifications within an hour
MAX_LOOKBACK_HOURS = 48       # Age filter: Ignore/seed historical leads older than 48 hours without alerting
ALERT_PACING_SECONDS = 3      # Throttle: Wait between consecutive dispatches to prevent flooding


def get_db(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS yelp_leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT UNIQUE,
                thread_id TEXT,
                customer_name TEXT,
                phone TEXT,
                property_type TEXT,
                location_zip TEXT,
                timing TEXT,
                estimate_min REAL,
                estimate_max REAL,
                lead_url TEXT,
                reply_to_email TEXT,
                raw_snippet TEXT,
                status TEXT DEFAULT 'NEW',
                bdr_alerted INTEGER DEFAULT 0,
                bdr_alerted_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_yelp_msg_id ON yelp_leads(message_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_yelp_bdr ON yelp_leads(bdr_alerted);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_yelp_alert_time ON yelp_leads(bdr_alerted_at);")
    return conn


def is_lead_processed(conn: sqlite3.Connection, message_id: str) -> bool:
    """Idempotency check: ensures the same email message is NEVER processed twice."""
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM yelp_leads WHERE message_id = ?", (message_id,))
    return cur.fetchone() is not None


def get_hourly_alert_count(conn: sqlite3.Connection) -> int:
    """Rate limit check: count how many alerts were dispatched in the last 60 minutes."""
    cur = conn.cursor()
    cur.execute("""
        SELECT count(*) FROM yelp_leads 
        WHERE bdr_alerted = 1 
          AND bdr_alerted_at > datetime('now', '-1 hour')
    """)
    row = cur.fetchone()
    return row[0] if row else 0


def insert_lead(conn: sqlite3.Connection, lead: Dict[str, Any]) -> int:
    with conn:
        cur = conn.execute("""
            INSERT OR IGNORE INTO yelp_leads (
                message_id, thread_id, customer_name, phone,
                property_type, location_zip, timing,
                estimate_min, estimate_max, lead_url,
                reply_to_email, raw_snippet, status,
                bdr_alerted, bdr_alerted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NEW', 0, NULL)
        """, (
            lead["message_id"],
            lead["thread_id"],
            lead["customer_name"],
            lead["phone"],
            lead["property_type"],
            lead["location_zip"],
            lead["timing"],
            lead["estimate_min"],
            lead["estimate_max"],
            lead["lead_url"],
            lead["reply_to_email"],
            lead.get("raw_text_snippet", ""),
        ))
        return cur.lastrowid


def mark_bdr_alerted(conn: sqlite3.Connection, lead_id: int):
    with conn:
        conn.execute("""
            UPDATE yelp_leads
            SET bdr_alerted = 1,
                bdr_alerted_at = CURRENT_TIMESTAMP,
                status = 'ALERTED_BDR'
            WHERE id = ?
        """, (lead_id,))


def scan_and_process_leads(max_results: int = 10) -> List[Dict[str, Any]]:
    """Scan Carlo's inbox for new Yelp leads with multi-tier loop and spam safeguards."""
    conn = get_db()

    # Tier 1: Hourly Velocity Rate Limiter
    hourly_count = get_hourly_alert_count(conn)
    if hourly_count >= MAX_ALERTS_PER_HOUR:
        logger.warning(
            f"CIRCUIT BREAKER TRIPPED: {hourly_count} alerts dispatched in the last hour "
            f"(limit is {MAX_ALERTS_PER_HOUR}). Halting scan to prevent spam loop."
        )
        return []

    logger.info("Initializing Gmail API client...")
    svc = get_carlo_gmail_service()

    # Tier 2: Search Query with Age Window (newer_than:2d prevents historical backlog reprocessing)
    query = 'from:(messaging.yelp.com OR yelp.com) ("homeowner or property request" OR "Message from" OR "New Lead") newer_than:2d'
    logger.info(f"Querying Gmail: {query}")
    res = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    messages = res.get("messages", [])
    logger.info(f"Found {len(messages)} matching Yelp messages within last 48h.")

    processed = []
    alerts_this_run = 0

    for item in messages:
        # Tier 3: Per-Run Circuit Breaker
        if alerts_this_run >= MAX_ALERTS_PER_RUN:
            logger.warning(
                f"Per-run circuit breaker reached ({MAX_ALERTS_PER_RUN} alerts). "
                f"Remaining leads will be handled on next scheduled sweep."
            )
            break

        mid = item["id"]
        # Tier 4: Idempotency Key (SQLite UNIQUE message_id)
        if is_lead_processed(conn, mid):
            logger.debug(f"Message {mid} already processed. Skipping.")
            continue

        try:
            full_msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
            lead = extract_lead_from_message(full_msg)
            if not lead:
                logger.warning(f"Could not parse Yelp lead from message {mid}.")
                continue

            lead_id = insert_lead(conn, lead)
            logger.info(f"Recorded new lead ID {lead_id} ({lead['customer_name']}, {lead['property_type']})")

            # Dispatch BDR alert
            logger.info(f"Dispatching BDR alert for lead: {lead['customer_name']} (Phone: {lead['phone'] or 'N/A'})")
            dispatch_results = dispatch_bdr_alert(lead)
            logger.info(f"Dispatch results for {lead['customer_name']}: {dispatch_results}")

            mark_bdr_alerted(conn, lead_id)
            processed.append(lead)
            alerts_this_run += 1

            # Tier 5: Inter-Message Throttle / Pacing
            if alerts_this_run < MAX_ALERTS_PER_RUN:
                time.sleep(ALERT_PACING_SECONDS)

        except Exception as e:
            logger.error(f"Error processing Yelp message {mid}: {e}", exc_info=True)

    return processed


def list_leads():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, customer_name, phone, property_type, location_zip, status, created_at FROM yelp_leads ORDER BY id DESC LIMIT 25")
    rows = cur.fetchall()
    print(f"\n{'ID':<5} {'Customer':<20} {'Phone':<16} {'Type':<22} {'ZIP':<8} {'Status':<14} {'Created'}")
    print("-" * 105)
    for r in rows:
        print(f"{r['id']:<5} {r['customer_name']:<20} {r['phone'] or 'N/A':<16} {r['property_type']:<22} {r['location_zip']:<8} {r['status']:<14} {r['created_at']}")
    print()


def manual_alert(lead_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM yelp_leads WHERE id = ?", (lead_id,))
    row = cur.fetchone()
    if not row:
        print(f"Lead ID {lead_id} not found.")
        return
    lead = dict(row)
    print(f"Manually dispatching alert for {lead['customer_name']}...")
    res = dispatch_bdr_alert(lead)
    print("Dispatch result:", res)
    mark_bdr_alerted(conn, lead_id)


def update_phone_and_alert(lead_id: int, phone: str):
    conn = get_db()
    with conn:
        conn.execute("UPDATE yelp_leads SET phone = ? WHERE id = ?", (phone, lead_id))
    print(f"Updated phone for lead ID {lead_id} to: {phone}")
    manual_alert(lead_id)


def main():
    parser = argparse.ArgumentParser(description="Yelp Lead Management & BDR Dispatcher with Loop Prevention")
    parser.add_argument("--scan", action="store_true", help="Scan inbox for new Yelp leads and dispatch alerts")
    parser.add_argument("--list", action="store_true", help="List recorded leads")
    parser.add_argument("--alert", type=int, help="Manually dispatch alert for lead ID")
    parser.add_argument("--set-phone", nargs=2, metavar=("ID", "PHONE"), help="Update lead phone and re-alert")

    args = parser.parse_args()

    if args.list:
        list_leads()
    elif args.alert:
        manual_alert(args.alert)
    elif args.set_phone:
        update_phone_and_alert(int(args.set_phone[0]), args.set_phone[1])
    else:
        new_leads = scan_and_process_leads()
        print(f"Finished scan. Processed {len(new_leads)} new leads.")


if __name__ == "__main__":
    main()
