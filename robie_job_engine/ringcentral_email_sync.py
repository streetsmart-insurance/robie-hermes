#!/usr/bin/env python3
"""RingCentral Automated Email Report Ingestion Module.

Fetches scheduled Call Log / Analytics reports sent from RingCentral
to robie@streetsmart.insurance via the Gmail API, saving them directly into
an intake directory for zero-cost automated ingestion (no paid API tier required).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from typing import Mapping

from .ringcentral_workbooks import (
    DEFAULT_REQUIRED_SHEETS,
    MAX_WORKBOOK_BYTES,
    RingCentralEvidenceError,
    classify_report,
    read_workbook,
    validate_coverage,
    validate_queue_membership,
    write_evidence_manifest,
)

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
    """Automates pulling explicitly labeled RingCentral reports from Gmail."""

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

    @staticmethod
    def _parts(part: dict[str, Any]) -> Iterable[dict[str, Any]]:
        for child in part.get("parts", []) or []:
            yield child
            yield from RingCentralEmailSync._parts(child)

    @staticmethod
    def _subject(payload: dict[str, Any]) -> str:
        for header in payload.get("headers", []) or []:
            if str(header.get("name") or "").casefold() == "subject":
                return str(header.get("value") or "")
        return ""

    @staticmethod
    def _attachment_bytes(gmail_service: Any, message_id: str, part: dict[str, Any]) -> bytes:
        body = part.get("body", {}) or {}
        attachment_id = body.get("attachmentId")
        if attachment_id:
            response = (
                gmail_service.users().messages().attachments().get(
                    userId="me", messageId=message_id, id=attachment_id
                ).execute()
            )
            encoded = response.get("data", "")
        else:
            encoded = body.get("data", "")
        if not encoded:
            raise RingCentralEvidenceError("RingCentral workbook attachment has no data")
        try:
            data = base64.urlsafe_b64decode(encoded + "===")
        except Exception as exc:
            raise RingCentralEvidenceError("RingCentral workbook attachment is not valid base64") from exc
        if not data or len(data) > MAX_WORKBOOK_BYTES:
            raise RingCentralEvidenceError("RingCentral workbook attachment size is outside the allowed range")
        return data

    def fetch_report_bundle(
        self,
        gmail_service: Any,
        *,
        report_kind: str,
        required_users: Sequence[str],
        required_queues: Sequence[str],
        required_queue_members: Mapping[str, Sequence[str]],
        required_sheets: Sequence[str] | None = None,
        max_age_hours: int = 36,
        batch_window_minutes: int = 120,
        as_of: Optional[datetime] = None,
    ) -> Path:
        """Collect the newest complete, explicitly labeled XLSX evidence bundle."""
        if report_kind not in DEFAULT_REQUIRED_SHEETS:
            raise RingCentralEvidenceError(f"unsupported RingCentral report kind: {report_kind}")
        needed = tuple(required_sheets or DEFAULT_REQUIRED_SHEETS[report_kind])
        now = as_of or datetime.now(timezone.utc)
        query = "from:(ringcentral.com) has:attachment filename:xlsx newer_than:3d"
        response = gmail_service.users().messages().list(userId="me", q=query, maxResults=50).execute()
        candidates: list[dict[str, Any]] = []
        seen_digests: set[str] = set()
        for message_meta in response.get("messages", []) or []:
            message_id = str(message_meta.get("id") or "")
            if not message_id:
                continue
            message = gmail_service.users().messages().get(userId="me", id=message_id).execute()
            received = datetime.fromtimestamp(int(message.get("internalDate") or 0) / 1000, tz=timezone.utc)
            age_hours = (now - received).total_seconds() / 3600
            if age_hours < 0 or age_hours > max(1, max_age_hours):
                continue
            payload = message.get("payload", {}) or {}
            subject = self._subject(payload)
            for part in self._parts(payload):
                filename = str(part.get("filename") or "")
                if not filename.casefold().endswith(".xlsx"):
                    continue
                classified = classify_report(subject, filename)
                if classified != report_kind:
                    continue
                content = self._attachment_bytes(gmail_service, message_id, part)
                digest = hashlib.sha256(content).hexdigest()
                if digest in seen_digests:
                    continue
                seen_digests.add(digest)
                target = self.output_dir / f"ringcentral-{report_kind}-{received:%Y%m%dT%H%M%SZ}-{digest[:12]}.xlsx"
                target.write_bytes(content)
                workbook = read_workbook(target)
                candidates.append({
                    "path": str(target.resolve()),
                    "sha256": digest,
                    "received_at": received.isoformat(),
                    "message_id_sha256": hashlib.sha256(message_id.encode()).hexdigest(),
                    "sheets": workbook["sheets"],
                    "workbook": workbook,
                })
        if not candidates:
            raise FileNotFoundError(f"no fresh explicitly labeled RingCentral {report_kind} XLSX was found")
        candidates.sort(key=lambda item: item["received_at"], reverse=True)
        newest = datetime.fromisoformat(candidates[0]["received_at"])
        lower_bound = newest - timedelta(minutes=max(1, batch_window_minutes))
        selected: list[dict[str, Any]] = []
        available: set[str] = set()
        for candidate in candidates:
            if datetime.fromisoformat(candidate["received_at"]) < lower_bound:
                continue
            contributes = set(candidate["workbook"].get("tables", {})) - available
            if contributes or not selected:
                selected.append(candidate)
                available.update(candidate["workbook"].get("tables", {}))
            if all(sheet in available for sheet in needed):
                break
        missing = [sheet for sheet in needed if sheet not in available]
        if missing:
            raise RingCentralEvidenceError(
                f"fresh RingCentral {report_kind} evidence is missing worksheets: {', '.join(missing)}"
            )
        validate_coverage(
            [item["workbook"] for item in selected],
            required_users=required_users,
            required_queues=required_queues,
        )
        validate_queue_membership(
            [item["workbook"] for item in selected],
            required_queues=required_queues,
            required_users=required_users,
            required_queue_members=required_queue_members,
            require_observed_legs=report_kind == "weekly",
        )
        manifest = self.output_dir / f"ringcentral-{report_kind}-{newest:%Y%m%dT%H%M%SZ}.json"
        return write_evidence_manifest(
            manifest,
            report_kind=report_kind,
            attachments=[{key: value for key, value in item.items() if key != "workbook"} for item in selected],
            required_sheets=needed,
            required_users=required_users,
            required_queues=required_queues,
            required_queue_members=required_queue_members,
            collected_at=now,
        )


def collect_scheduled_ringcentral_report(
    *,
    service_account_email: str,
    mailbox: str,
    output_dir: Path,
    report_kind: str,
    required_users: Sequence[str],
    required_queues: Sequence[str],
    required_queue_members: Mapping[str, Sequence[str]],
    required_sheets: Sequence[str] | None = None,
    max_age_hours: int = 36,
    service_factory: Any = build_keyless_report_mailbox_service,
) -> Path:
    if not service_account_email or not mailbox:
        raise ValueError("RingCentral scheduled-email collection requires service account and mailbox")
    service = service_factory(service_account_email, mailbox)
    path = RingCentralEmailSync(output_dir).fetch_report_bundle(
        service,
        report_kind=report_kind,
        required_users=required_users,
        required_queues=required_queues,
        required_queue_members=required_queue_members,
        required_sheets=required_sheets,
        max_age_hours=max_age_hours,
    )
    return path
