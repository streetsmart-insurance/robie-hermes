#!/usr/bin/env python3
"""Magellan AI Automated Email Report Ingestion Module.

Fetches scheduled Magellan AI Sentiment CSV/JSON reports sent to
robie@streetsmart.insurance via the Gmail API, saving them directly into
an intake directory for zero-cost automated sentiment auditing.
"""

from __future__ import annotations

import base64
import csv
import io
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from robie_job_engine.magellan_client import MagellanCallRecord

logger = logging.getLogger("robie.magellan_email_sync")

MAGELLAN_REPORTS_DIR = Path(os.environ.get("ROBIE_MAGELLAN_REPORTS_DIR", "/tmp/magellan_reports"))


class MagellanEmailSync:
    """Automates pulling scheduled Magellan AI sentiment CSVs from Gmail."""

    def __init__(
        self,
        output_dir: Path = MAGELLAN_REPORTS_DIR,
    ):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def fetch_latest_sentiment_attachment(self, gmail_service: Any) -> Optional[Path]:
        """Queries Gmail for Magellan AI report emails and downloads attachments."""
        try:
            query = "from:(magellan.insure) has:attachment (Sentiment OR Analytics OR Call OR Report)"
            results = gmail_service.users().messages().list(userId="me", q=query, maxResults=5).execute()
            messages = results.get("messages", [])

            if not messages:
                logger.info("No recent Magellan report emails found in mailbox.")
                return None

            for msg_meta in messages:
                msg_id = msg_meta["id"]
                msg = gmail_service.users().messages().get(userId="me", id=msg_id).execute()
                payload = msg.get("payload", {})
                parts = payload.get("parts", [])

                for part in parts:
                    filename = part.get("filename", "")
                    if filename.lower().endswith(".csv") or filename.lower().endswith(".json"):
                        body = part.get("body", {})
                        attachment_id = body.get("attachmentId")

                        if attachment_id:
                            att = (
                                gmail_service.users()
                                .messages()
                                .attachments()
                                .get(userId="me", messageId=msg_id, id=attachment_id)
                                .execute()
                            )
                            file_data = base64.urlsafe_b64decode(att.get("data", "") + "===")
                        elif body.get("data"):
                            file_data = base64.urlsafe_b64decode(body.get("data", "") + "===")
                        else:
                            continue

                        dest_path = self.output_dir / f"Magellan_Report_{int(time.time())}.csv"
                        with open(dest_path, "wb") as f:
                            f.write(file_data)

                        logger.info("Successfully downloaded Magellan report attachment: %s", dest_path)
                        return dest_path

            logger.info("Checked Magellan emails, but no attachments were found.")
            return None

        except Exception as e:
            logger.error("Error fetching Magellan email report: %s", e)
            return None

    @classmethod
    def parse_magellan_csv(cls, csv_content_or_path: Path | str) -> List[MagellanCallRecord]:
        """Parses Magellan AI export CSV into MagellanCallRecord objects."""
        if isinstance(csv_content_or_path, Path) or (isinstance(csv_content_or_path, str) and "\n" not in csv_content_or_path and Path(csv_content_or_path).exists()):
            with open(csv_content_or_path, "r", encoding="utf-8-sig") as f:
                content = f.read()
        else:
            content = str(csv_content_or_path)

        reader = csv.DictReader(io.StringIO(content))
        records: List[MagellanCallRecord] = []

        for r in reader:
            row_lower = {k.strip().lower(): v.strip() for k, v in r.items() if k}
            raw_phone = row_lower.get("from") or row_lower.get("caller phone") or row_lower.get("phone") or ""
            raw_sent = row_lower.get("sentiment") or "Neutral"
            raw_tags = [t.strip() for t in (row_lower.get("tags") or row_lower.get("intent tags") or "").split(",") if t.strip()]

            records.append(
                MagellanCallRecord(
                    date_time=datetime.now(timezone.utc),
                    from_phone=raw_phone,
                    to_phone=row_lower.get("to") or "",
                    duration_seconds=int(row_lower.get("duration") or 0),
                    sentiment=raw_sent,
                    tags=raw_tags,
                    is_handled=row_lower.get("handled", "true").lower() in ("true", "1", "yes"),
                    caller_name=row_lower.get("caller name"),
                    transcript_summary=row_lower.get("summary") or row_lower.get("notes"),
                    at_risk_flag="at-risk" in [t.lower() for t in raw_tags] or "cancellation" in [t.lower() for t in raw_tags] or "sad" in raw_sent.lower(),
                    cancellation_flag="cancellation" in [t.lower() for t in raw_tags],
                )
            )

        return records
