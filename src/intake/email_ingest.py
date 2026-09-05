"""Automated Email Report Ingestor.

Polls robie@streetsmart.insurance for scheduled EZLynx / Applied Systems renewal export CSVs,
downloads attachments into data/input_reports/, and syncs policies to SQLite database.
"""

import os
import re
import base64
import logging
from datetime import datetime, date
from pathlib import Path
from typing import List, Optional

from src.config import settings
from src.database.session import SessionLocal, init_db
from src.email_outreach.gmail_client import GmailRenewalClient
from src.intake.report_ingestor import ReportIngestor

logger = logging.getLogger("email_report_ingest")

class ScheduledReportEmailIngestor:
    """Monitors robie@streetsmart.insurance for scheduled EZLynx report deliveries."""

    def __init__(self, target_dir: Optional[Path] = None, gmail_client: Optional[GmailRenewalClient] = None):
        self.target_dir = target_dir or settings.input_reports_path
        self.gmail = gmail_client or GmailRenewalClient()
        self.ingestor = ReportIngestor(input_dir=self.target_dir)

    def fetch_and_ingest_email_reports(self) -> int:
        """
        Polls for scheduled report emails, downloads new CSV/XLSX attachments,
        and synchronizes policies into renewals.db.
        Returns the count of newly ingested / updated policy records.
        """
        active_inboxes = getattr(self.gmail, "inbox_services", {})
        svc = active_inboxes.get(settings.gmail_outreach_email)
        if not svc:
            logger.warning(f"No active Gmail service for {settings.gmail_outreach_email}. Skipping email report intake.")
            return 0

        # Query for emails with attachments from EZLynx or Applied or with renewal subject
        query = "(from:(appliedsystems.com OR ezlynx.com) OR subject:(EZLynx OR Renewal OR Scheduled)) has:attachment"
        logger.info(f"Querying {settings.gmail_outreach_email} for scheduled reports: '{query}'")

        try:
            res = svc.users().messages().list(userId="me", q=query, maxResults=20).execute()
            messages = res.get("messages", [])
        except Exception as e:
            logger.error(f"Failed to query Gmail messages for scheduled reports: {e}")
            return 0

        if not messages:
            logger.info("No matching scheduled report emails found.")
            return 0

        self.target_dir.mkdir(parents=True, exist_ok=True)
        downloaded_files: List[Path] = []

        for m in messages:
            msg_id = m["id"]
            try:
                msg_data = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
                payload = msg_data.get("payload", {})
                headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
                subject = headers.get("subject", "")

                parts = payload.get("parts", [payload])
                for part in parts:
                    filename = part.get("filename", "")
                    att_id = part.get("body", {}).get("attachmentId")
                    if filename and att_id and any(filename.lower().endswith(ext) for ext in (".csv", ".xlsx", ".xls")):
                        clean_fn = re.sub(r"[^\w\.-]", "_", filename)
                        safe_filename = f"EZLynx_Scheduled_{msg_id[:8]}_{clean_fn}"
                        dest = self.target_dir / safe_filename
                        if dest.exists():
                            logger.debug(f"Report file already exists: {dest.name}")
                            continue

                        att_data = svc.users().messages().attachments().get(
                            userId="me", messageId=msg_id, id=att_id
                        ).execute()
                        file_bytes = base64.urlsafe_b64decode(att_data["data"])
                        dest.write_bytes(file_bytes)
                        logger.info(f"Downloaded scheduled EZLynx report: {dest.name} (Subject: '{subject}')")
                        downloaded_files.append(dest)

            except Exception as e:
                logger.error(f"Error extracting attachment from message {msg_id}: {e}")

        if not downloaded_files:
            logger.info("No new report attachments were downloaded.")
            return 0

        # Sync downloaded files to database
        init_db()
        db = SessionLocal()
        total_synced = 0
        try:
            for filepath in downloaded_files:
                if filepath.name.startswith("."):
                    continue
                logger.info(f"Downloaded report ready for ingestion: {filepath.name}")
            new_count, in_win = self.ingestor.sync_to_database(db=db)
            total_synced = new_count
            logger.info(f"Successfully synced {new_count} new policies ({in_win} in 30-45d window)")
        finally:
            db.close()

        return total_synced


def ingest_reports_from_email() -> int:
    """Convenience entrypoint to poll and ingest email reports."""
    ingestor = ScheduledReportEmailIngestor()
    return ingestor.fetch_and_ingest_email_reports()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    synced = ingest_reports_from_email()
    print(f"Scheduled report intake complete. Policies synced: {synced}")

