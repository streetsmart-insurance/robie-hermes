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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("robie.ringcentral_email_sync")

RC_REPORTS_DIR = Path(os.environ.get("ROBIE_RINGCENTRAL_REPORTS_DIR", "/tmp/ringcentral_reports"))
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def build_keyless_report_mailbox_service(service_account_email: str, mailbox: str) -> Any:
    """Build a keyless delegated Gmail client for Robie's report mailbox only."""
    import google.auth
    from google.auth import iam
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    signer = iam.Signer(Request(), source, service_account_email)
    delegated = service_account.Credentials(
        signer=signer,
        service_account_email=service_account_email,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[GMAIL_READONLY_SCOPE],
        subject=mailbox,
    )
    return build("gmail", "v1", credentials=delegated, cache_discovery=False)


class RingCentralEmailSync:
    """Automates pulling scheduled RingCentral call log CSVs from Gmail."""

    def __init__(
        self,
        output_dir: Path = RC_REPORTS_DIR,
    ):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def fetch_latest_call_log_attachment(
        self,
        gmail_service: Any,
        *,
        max_age_hours: int = 36,
        as_of: Optional[datetime] = None,
    ) -> Optional[Path]:
        """Queries Gmail for RingCentral scheduled report emails and downloads CSV attachments."""
        try:
            # Query for RingCentral report emails
            query = "from:(ringcentral.com) has:attachment (CallLog OR 'Call Log' OR Analytics OR report) newer_than:3d"
            results = gmail_service.users().messages().list(userId="me", q=query, maxResults=5).execute()
            messages = results.get("messages", [])

            if not messages:
                logger.info("No recent RingCentral report emails found in mailbox.")
                return None

            for msg_meta in messages:
                msg_id = msg_meta["id"]
                msg = gmail_service.users().messages().get(userId="me", id=msg_id).execute()
                internal_date = int(msg.get("internalDate") or 0)
                if internal_date:
                    received = datetime.fromtimestamp(internal_date / 1000, tz=timezone.utc)
                    age_hours = ((as_of or datetime.now(timezone.utc)) - received).total_seconds() / 3600
                    if age_hours < 0 or age_hours > max(1, max_age_hours):
                        continue
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


def collect_scheduled_ringcentral_report(
    *,
    service_account_email: str,
    mailbox: str,
    output_dir: Path,
    max_age_hours: int = 36,
    service_factory: Any = build_keyless_report_mailbox_service,
) -> Path:
    if not service_account_email or not mailbox:
        raise ValueError("RingCentral scheduled-email collection requires service account and mailbox")
    service = service_factory(service_account_email, mailbox)
    path = RingCentralEmailSync(output_dir).fetch_latest_call_log_attachment(
        service, max_age_hours=max_age_hours
    )
    if path is None:
        raise FileNotFoundError("no fresh RingCentral scheduled CSV was found")
    return path
