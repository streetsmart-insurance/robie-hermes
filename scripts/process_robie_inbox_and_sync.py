#!/usr/bin/env python3
"""
scripts/process_robie_inbox_and_sync.py

Processes actionable underwriter emails in Robie's inbox:
1. Ingests carrier responses (Markel, NJM, Asia Workers Comp, Chubb).
2. Downloads attached renewal documents (quotes, loss runs).
3. Uploads PDFs to the EZLynx Document Library for matching applicants.
4. Posts audit notes directly into authentic CSR discussion cards via EZLynx REST API.
5. Updates local SQLite tracking tables (policy_renewals, audit_note_logs, document_records).
6. Marks all processed emails, routine Applied queue notifications, and RingCentral logs as READ.
7. Purges mailer-daemon/delivery failures to trash.
"""

import sys
import os
import sqlite3
import logging
from pathlib import Path
from datetime import datetime

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("process_robie_inbox")

from src.email_outreach.auth_setup import get_robie_gmail_service
from src.email_outreach.gmail_client import GmailRenewalClient
from src.ezlynx.api_client import EZLynxApiClient
from src.config import settings

DB_PATH = Path("data/renewals.db")


def log_audit_db(conn, policy_id, applicant_id, policy_number, disc_title, note_text, note_id):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO audit_note_logs (policy_id, applicant_id, discussion_title, action_type, note_text, synced_to_ezlynx, ezlynx_note_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        policy_id,
        applicant_id,
        disc_title,
        "UNDERWRITER_REPLIED",
        note_text,
        1,
        str(note_id) if note_id else None,
        datetime.now().isoformat()
    ))
    conn.commit()


def log_document_db(conn, policy_id, applicant_id, policy_number, doc_type, file_name, file_path, ezlynx_doc_id):
    cur = conn.cursor()
    file_sz = file_path.stat().st_size if hasattr(file_path, "stat") and file_path.exists() else 0
    cur.execute("""
        INSERT INTO document_records (policy_id, file_name, file_path, file_size_bytes, source, uploaded_to_ezlynx, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        policy_id,
        file_name,
        str(file_path),
        file_sz,
        "EMAIL_ATTACHMENT",
        1,
        datetime.now().isoformat()
    ))
    conn.commit()


def main():
    logger.info("Starting Robie inbox processing and EZLynx dynamic sync...")
    gmail_client = GmailRenewalClient()
    svc = get_robie_gmail_service()
    ezlynx = EZLynxApiClient()
    conn = sqlite3.connect(str(DB_PATH))

    # =========================================================================
    # 1. PROCESS ACTIONABLE CARRIER EMAILS
    # =========================================================================

    # --- A. Kodomo Education Services LLC (Markel Loss Runs) ---
    kodomo_msg_id = "1a06e418dd5c8cc2"
    logger.info(f"--- Processing Markel response for Kodomo (msg {kodomo_msg_id}) ---")
    parsed_kodomo = gmail_client._fetch_and_parse_msg(svc, kodomo_msg_id, "robie@streetsmart.insurance")
    if parsed_kodomo:
        app_id = "199797602"
        pol_num = "CCP35165-01"
        carrier = "Markel Insurance Company"
        lob = "Commercial pkg"
        
        # Use pre-downloaded attachments
        downloaded_docs = [att["path"] for att in parsed_kodomo.get("attachments", []) if att.get("path")]

        # Upload document to EZLynx
        doc_res_id = None
        for doc in downloaded_docs:
            up_res = ezlynx.upload_document(
                applicant_id=app_id,
                file_path=doc,
                folder_name="Renewal Offers/Declarations",
                description="Markel 5-Year Loss Run Report",
                policy_number=pol_num,
                doc_type="Loss Runs",
                label_to_apply="Loss Runs"
            )
            doc_res_id = up_res.get("document_id")
            log_document_db(conn, 62, app_id, pol_num, "Loss Runs", doc.name, doc, doc_res_id)
            logger.info(f"Uploaded {doc.name} for Kodomo: {up_res}")

        # Post Note to Authentic Discussion Card
        kodomo_clean_msg = parsed_kodomo.get("clean_reply_text", "")
        kodomo_msg_section = f'\nUnderwriter Message:\n"{kodomo_clean_msg}"\n' if kodomo_clean_msg else ""
        note_body = (
            f"Policy: #{pol_num} ({lob} - {carrier})\n"
            "Carrier Response Received - Markel Insurance Company:\n"
            f"Sender: {parsed_kodomo.get('sender')}\n"
            "Attachment: CCP35165.pdf (5-Year Loss Run Report)\n"
            "Summary: Markel provided the requested 5-year Loss Run Report for Kodomo Education Services LLC. "
            f"Document uploaded to client Documents tab.\n"
            f"{kodomo_msg_section}\n"
            "Robie Renewal Engine\n\n"
            "Robie was here"
        )
        note_res = ezlynx.add_note_to_discussion(
            applicant_id=app_id,
            policy_number=pol_num,
            line_of_business=lob,
            carrier_name=carrier,
            note_text=note_body
        )
        logger.info(f"Posted note for Kodomo: {note_res}")
        log_audit_db(conn, 62, app_id, pol_num, note_res.get("discussion_title"), note_body, note_res.get("note_id"))

        # Mark as Read
        gmail_client.mark_message_read(kodomo_msg_id, "robie@streetsmart.insurance")

    # --- B. A&B Finish Carpenters LLC (NJM Quote & Claim Report) ---
    ab_msg_ids = ["1a06c6f6b5b7879a", "1a06d7ed837859ad"]
    logger.info(f"--- Processing NJM responses for A&B Finish Carpenters (msgs {ab_msg_ids}) ---")
    app_id = "144897143"
    pol_num = "106793-4-25"
    carrier = "NJCRIB - New Jersey Manufacturers Assigned Risk"
    lob = "Workers comp"

    ab_docs = []
    ab_clean_msgs = []
    for mid in ab_msg_ids:
        p_msg = gmail_client._fetch_and_parse_msg(svc, mid, "robie@streetsmart.insurance")
        if p_msg:
            if p_msg.get("clean_reply_text"):
                ab_clean_msgs.append(f"[{p_msg.get('sender', 'Underwriter')}]: \"{p_msg.get('clean_reply_text')}\"")
            for att in p_msg.get("attachments", []):
                fpath = att.get("path")
                if fpath:
                    ab_docs.append((fpath, "Renewal" if "Quotation" in att["filename"] else "Loss Runs"))


    # Upload all downloaded docs
    for doc_path, dtype in ab_docs:
        desc = "NJM Workers Comp Renewal Quote" if dtype == "Renewal" else "NJM Claim Report / Loss Runs"
        up_res = ezlynx.upload_document(
            applicant_id=app_id,
            file_path=doc_path,
            folder_name="Renewal Offers/Declarations",
            description=desc,
            policy_number=pol_num,
            doc_type=dtype,
            label_to_apply="Renewals" if dtype == "Renewal" else "Loss Runs"
        )
        log_document_db(conn, 72, app_id, pol_num, dtype, doc_path.name, doc_path, up_res.get("document_id"))
        logger.info(f"Uploaded {doc_path.name} for A&B Finish: {up_res}")

    # Post Note to Authentic Discussion Card
    ab_msg_section = "\nUnderwriter Messages:\n" + "\n".join(ab_clean_msgs) + "\n" if ab_clean_msgs else ""
    note_body = (
        f"Policy: #{pol_num} ({lob} - {carrier})\n"
        "Carrier Response Received - NJM Insurance:\n"
        "Sender: WCUMail@njm.com / Liam Egenton <legenton@njm.com>\n"
        "Attachments: WCU Quotation (Quote #QQ41152139), Claim Report\n"
        "Summary: NJM provided official Renewal Quotation (Quote #QQ41152139) and Claim Report for A&B Finish Carpenters LLC. "
        f"Documents uploaded to client Documents tab. Assigned CSR notified for renewal review.\n"
        f"{ab_msg_section}\n"
        "Robie Renewal Engine\n\n"
        "Robie was here"
    )
    note_res = ezlynx.add_note_to_discussion(
        applicant_id=app_id,
        policy_number=pol_num,
        line_of_business=lob,
        carrier_name=carrier,
        note_text=note_body
    )
    logger.info(f"Posted note for A&B Finish: {note_res}")
    log_audit_db(conn, 72, app_id, pol_num, note_res.get("discussion_title"), note_body, note_res.get("note_id"))

    # Create Follow-up Task for CSR (Eimy Ramos)
    task_res = ezlynx.create_user_task(
        applicant_id=app_id,
        title=f"Review Renewal Quote: A&B Finish Carpenters LLC (NJM #{pol_num})",
        description=f"NJM Renewal Quote #QQ41152139 received and uploaded to client Documents tab. Please review quote terms and present to client.",
        assigned_user="Eimy Ramos",
        due_days_out=3
    )
    logger.info(f"Created CSR task for A&B Finish: {task_res}")

    # Mark A&B Finish emails as Read
    for mid in ab_msg_ids:
        gmail_client.mark_message_read(mid, "robie@streetsmart.insurance")

    # --- C. Yes We Do LLC (Associated Specialty / Barb McCanney) ---
    ywd_msg_ids = ["1a06d45685255eae", "1a06c07264b9cf22"]
    logger.info(f"--- Processing Associated Specialty updates for Yes We Do LLC (msgs {ywd_msg_ids}) ---")
    app_id = "21588091"
    pol_num = "PWC1239278"
    carrier = "Associated Specialty Insurance Agency MGA"
    lob = "Workers comp"

    ywd_clean_msgs = []
    for mid in ywd_msg_ids:
        p_msg = gmail_client._fetch_and_parse_msg(svc, mid, "robie@streetsmart.insurance")
        if p_msg and p_msg.get("clean_reply_text"):
            ywd_clean_msgs.append(f"[{p_msg.get('sender', 'Underwriter')}]: \"{p_msg.get('clean_reply_text')}\"")

    ywd_msg_section = "\nUnderwriter Message(s):\n" + "\n".join(ywd_clean_msgs) + "\n" if ywd_clean_msgs else ""
    note_body = (
        f"Policy: #{pol_num} ({lob} - {carrier})\n"
        "Underwriter Update - Associated Specialty (Barb McCanney):\n"
        "Sender: Barbara McCanney <BMcCanney@asiaworkerscomp.com>\n"
        "Summary: Underwriter Barb McCanney confirmed renewal quote will be forwarded once received and inquired regarding renewal competition.\n"
        f"{ywd_msg_section}\n"
        "Robie Renewal Engine\n\n"
        "Robie was here"
    )
    note_res = ezlynx.add_note_to_discussion(
        applicant_id=app_id,
        policy_number=pol_num,
        line_of_business=lob,
        carrier_name=carrier,
        note_text=note_body
    )
    logger.info(f"Posted note for Yes We Do LLC: {note_res}")
    log_audit_db(conn, 74, app_id, pol_num, note_res.get("discussion_title"), note_body, note_res.get("note_id"))

    # Mark Yes We Do emails as Read
    for mid in ywd_msg_ids:
        gmail_client.mark_message_read(mid, "robie@streetsmart.insurance")

    # --- D. Dean & Danielle Lacorte (Chubb Customer Center notices) ---
    lacorte_msg_ids = ["1a06ca7eacf13fa4", "1a06c99ccd9d2718", "1a06e1936f474e43", "1a06c016fed6f45f"]
    logger.info(f"--- Processing Chubb notices for Dean & Danielle Lacorte (msgs {lacorte_msg_ids}) ---")
    app_id = "58768907"

    # Post to Homeowners
    note_ho = (
        "Policy: #13332766-03 (Homeowners - Chubb Group)\n"
        "Carrier Notice - Chubb Customer Center:\n"
        "Sender: Chubb Customer Center <customercenter@chubb.com>\n"
        "Summary: Chubb Customer Center correspondence verification notice received regarding CCC producer code.\n\n"
        "Robie Renewal Engine\n\n"
        "Robie was here"
    )
    note_res_ho = ezlynx.add_note_to_discussion(
        applicant_id=app_id,
        policy_number="13332766-03",
        line_of_business="Homeowners",
        carrier_name="Chubb Group",
        note_text=note_ho
    )
    logger.info(f"Posted note for Lacorte HO: {note_res_ho}")
    log_audit_db(conn, 16, app_id, "13332766-03", note_res_ho.get("discussion_title"), note_ho, note_res_ho.get("note_id"))

    # Post to Auto
    note_auto = (
        "Policy: #268224700A (Auto (Personal) - Chubb Group)\n"
        "Carrier Notice - Chubb Customer Center:\n"
        "Sender: Chubb Customer Center <customercenter@chubb.com>\n"
        "Summary: Chubb Customer Center correspondence verification notice received regarding CCC producer code.\n\n"
        "Robie Renewal Engine\n\n"
        "Robie was here"
    )
    note_res_auto = ezlynx.add_note_to_discussion(
        applicant_id=app_id,
        policy_number="268224700A",
        line_of_business="Auto (Personal)",
        carrier_name="Chubb Group",
        note_text=note_auto
    )
    logger.info(f"Posted note for Lacorte Auto: {note_res_auto}")
    log_audit_db(conn, 17, app_id, "268224700A", note_res_auto.get("discussion_title"), note_auto, note_res_auto.get("note_id"))

    # Mark Lacorte emails as Read
    for mid in lacorte_msg_ids:
        gmail_client.mark_message_read(mid, "robie@streetsmart.insurance")

    # --- E. Auto-replies and Test reminders ---
    misc_read_ids = [
        "1a06c0972cccb3e0",  # Angela Conklin (Travelers / ROBIE Test)
        "1a06d39d5e1f0e53",  # Jake Ferrara (ROBIE Test)
        "1a06e1922727d73b",  # Jake Ferrara auto-reply
        "1a06c018608309d6",  # Auto Submissions bhhomestate auto-reply
    ]
    for mid in misc_read_ids:
        gmail_client.mark_message_read(mid, "robie@streetsmart.insurance")

    # =========================================================================
    # 2. BATCH CLEANUP: MARK ROUTINE AUTOMATED EMAILS AS READ
    # =========================================================================
    logger.info("--- Cleaning up routine automated emails (Applied queues, RingCentral, Newsletters) ---")

    # 1. Applied Reporting daily audit queue emails
    res_app = svc.users().messages().list(userId="me", q="is:unread from:DoNotReply@appliedsystems.com", maxResults=500).execute()
    app_ids = [m["id"] for m in res_app.get("messages", [])]
    if app_ids:
        logger.info(f"Marking {len(app_ids)} Applied Reporting queue emails as READ...")
        gmail_client.batch_mark_read(app_ids, "robie@streetsmart.insurance")

    # 2. RingCentral daily call logs & reports
    res_rc = svc.users().messages().list(userId="me", q="is:unread (from:service@ringcentral.com OR from:analytics.portal@ringcentral.com)", maxResults=500).execute()
    rc_ids = [m["id"] for m in res_rc.get("messages", [])]
    if rc_ids:
        logger.info(f"Marking {len(rc_ids)} RingCentral report emails as READ...")
        gmail_client.batch_mark_read(rc_ids, "robie@streetsmart.insurance")

    # 3. Expired verification codes
    res_codes = svc.users().messages().list(userId="me", q="is:unread (subject:\"verification code\" OR subject:\"Your Verification Code\")", maxResults=100).execute()
    code_ids = [m["id"] for m in res_codes.get("messages", [])]
    if code_ids:
        logger.info(f"Marking {len(code_ids)} expired verification code emails as READ...")
        gmail_client.batch_mark_read(code_ids, "robie@streetsmart.insurance")

    # 4. Routine vendor newsletters & marketing
    res_news = svc.users().messages().list(userId="me", q="is:unread (from:newsletter@usli.com OR from:agencyservice@digital.pieinsurance.com OR from:info@rpsins.com OR from:onboarding@openly.com OR from:support@useascend.com)", maxResults=100).execute()
    news_ids = [m["id"] for m in res_news.get("messages", [])]
    if news_ids:
        logger.info(f"Marking {len(news_ids)} vendor marketing/newsletter emails as READ...")
        gmail_client.batch_mark_read(news_ids, "robie@streetsmart.insurance")

    # 5. Out of office auto replies
    res_ooo = svc.users().messages().list(userId="me", q="is:unread (subject:\"out of the office\" OR subject:\"out of office\" OR subject:\"automatic reply\" OR subject:\"auto reply\")", maxResults=100).execute()
    ooo_ids = [m["id"] for m in res_ooo.get("messages", [])]
    if ooo_ids:
        logger.info(f"Marking {len(ooo_ids)} Out of Office auto-replies as READ...")
        gmail_client.batch_mark_read(ooo_ids, "robie@streetsmart.insurance")

    # 6. Trash pure noise: Mailer-Daemon & Postmaster
    res_noise = svc.users().messages().list(userId="me", q="from:mailer-daemon OR from:postmaster OR subject:\"Delivery Status Notification (Failure)\"", maxResults=100).execute()
    noise_ids = [m["id"] for m in res_noise.get("messages", [])]
    if noise_ids:
        logger.info(f"Moving {len(noise_ids)} mailer-daemon/bounce failure emails to TRASH...")
        for mid in noise_ids:
            gmail_client.trash_message(mid, "robie@streetsmart.insurance")

    conn.close()
    logger.info("Robie inbox processing, dynamic EZLynx sync, and read cleanup successfully finished!")


if __name__ == "__main__":
    main()
