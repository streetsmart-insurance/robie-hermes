#!/usr/bin/env python3
"""RingCentral Automated Email Report Ingestion Module.

Fetches scheduled Call Log / Analytics CSV reports sent from RingCentral
to robie@streetsmart.insurance via the Gmail API, saving them directly into
an intake directory for zero-cost automated ingestion (no paid API tier required).
"""

from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("robie.ringcentral_email_sync")

RC_REPORTS_DIR = Path(os.environ.get("ROBIE_RINGCENTRAL_REPORTS_DIR", "/tmp/ringcentral_reports"))


class RingCentralEmailSync:
    """Automates pulling scheduled RingCentral call log CSVs from Gmail."""

    def __init__(
        self,
        output_dir: Path = RC_REPORTS_DIR,
    ):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def fetch_latest_call_log_attachment(self, gmail_service: Any) -> Optional[Path]:
        """Queries Gmail for RingCentral scheduled report emails and downloads CSV attachments."""
        try:
            # Query for RingCentral report emails
            query = "from:(ringcentral.com) has:attachment (CallLog OR 'Call Log' OR Analytics OR report)"
            results = gmail_service.users().messages().list(userId="me", q=query, maxResults=5).execute()
            messages = results.get("messages", [])

            if not messages:
                logger.info("No recent RingCentral report emails found in mailbox.")
                return None

            for msg_meta in messages:
                msg_id = msg_meta["id"]
                msg = gmail_service.users().messages().get(userId="me", id=msg_id).execute()
                payload = msg.get("payload", {})
                parts = payload.get("parts", [])

                for part in parts:
                    filename = part.get("filename", "")
                    if filename.lower().endswith(".csv"):
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

                        dest_path = self.output_dir / f"RingCentral_CallLog_{int(time.time())}.csv"
                        with open(dest_path, "wb") as f:
                            f.write(file_data)

                        logger.info("Successfully downloaded RingCentral report attachment: %s", dest_path)
                        return dest_path

            logger.info("Checked RingCentral emails, but no CSV attachments were found.")
            return None

        except Exception as e:
            logger.error("Error fetching RingCentral email report: %s", e)
            return None
