"""robie-filer: file robie@ email and attachments into EZLynx Documents.

Runs every 10 minutes from ``robie-filer.timer``. One run does three things:

1. **Queue** (sheet tab "Queue"): rows with a blank or ``Pending`` status.
   ``ATTACHMENT`` uploads one Gmail attachment. ``EMAIL`` uploads the email
   as a PDF and then each attachment. The row gets ``Filed`` and the
   document id(s), or ``Error`` and the exact error.
2. **Needs review** (tab "Needs review"): a row with ``resolved_applicant_id``
   filled and ``resolved_by`` blank is filed to that client.
3. **Automatic** (``--auto``): every new robie@ message (inbox and sent) is
   matched by :mod:`robie_filer_match`. One client: filed. Otherwise a
   Needs review row. Skipped mail is only recorded.

Every PDF filed is read on this host (``pdftotext``, OCR when a page has no
text) and a dec-page summary is saved next to the ledger and on the "Filed"
tab. A row with ``discussion_id`` also gets the note
``Saved to Documents: <document_name>`` on that discussion.

Safety:

- Dry run unless ``--live``. A dry run makes no upload, note, or sheet write.
- Uploads go through ``upload_document_via_api`` (DocumentApi + read-back of
  the new id). Notes go through ``file_note_to_existing_discussion``
  (existing discussion only, its own ledger and read-back). No browser.
- Which client may receive a write is decided by ``ezlynx_write_scope``.
  Without ``--any-applicant`` that is the compiled allowlist (the test
  account). ``--any-applicant`` needs the filer operation scope in
  ``ezlynx_write_scope``; it widens only document upload and note append.
- A SQLite ledger keyed by Gmail message id and attachment means nothing is
  filed twice. An upload that started but was not confirmed is settled on
  the next run by searching the client's documents for the exact name
  before uploading again.
- Only robie@ is read (Carlo 2026-10-08). Gmail access is read-only.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import mimetypes
import os
import re
import sqlite3
import sys
import time
from base64 import urlsafe_b64decode
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from . import robie_filer_match as matching
from .robie_filer_render import attachment_parts, body_text, header, render_email_pdf

logger = logging.getLogger("robie_filer")

MAILBOX = "robie@streetsmart.insurance"
DEFAULT_SHEET_ID = "1SBV8SddORPneCO2Pevc97wj5dI4l9TaR7Q2GLxrJINs"
DEFAULT_STATE_DIR = "/var/lib/robie-filer"
DEFAULT_INDEX_CSV = "/var/lib/robie-filer/cert-applicant-index.csv"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

QUEUE_TAB = "Queue"
REVIEW_TAB = "Needs review"
FILED_TAB = "Filed"
README_TAB = "Read me"
QUEUE_HEADERS = [
    "type", "gmail_message_id", "attachment_name", "applicant_id", "policy_master_id",
    "document_name", "label", "discussion_id", "status", "ezlynx_document_id", "error",
]
REVIEW_HEADERS = [
    "date", "from", "subject", "gmail_message_id", "thread_id", "reason",
    "candidate_applicant_ids", "resolved_applicant_id", "resolved_by",
]
FILED_HEADERS = [
    "filed_at", "source", "gmail_message_id", "applicant_id", "account_name", "how_matched",
    "document_name", "ezlynx_document_id", "summary",
]
README_TEXT = (
    "Robie checks the Queue tab every 10 minutes. To file something, add a row and leave "
    "status blank. Robie fills in status, ezlynx_document_id and error. Rows in Needs review "
    "are emails Robie could not match to one client. Fill in resolved_applicant_id and Robie "
    "will file them. The Filed tab lists every document Robie filed, with a short dec-page "
    "summary for PDFs."
)

FILED = "Filed"
ERROR = "Error"
PENDING = "Pending"
MAX_AUTO_MESSAGES_PER_RUN = 50
MAX_DOCUMENT_NAME = 150
ERROR_TEXT_LIMIT = 400
NOTE_PREFIX = "Saved to Documents: "


class FilerError(RuntimeError):
    """A row or message could not be filed. The text goes to the sheet."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def short_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    try:
        from .secrets import redact_text

        text = redact_text(text)
    except Exception:  # noqa: BLE001 - redaction is best effort on the message
        pass
    return text[:ERROR_TEXT_LIMIT]


def safe_document_name(name: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", str(name or "")).strip()
    return re.sub(r"\s+", " ", cleaned)[:MAX_DOCUMENT_NAME] or "Document"


def email_document_name(message: dict[str, Any]) -> str:
    stamp = ""
    try:
        stamp = datetime.fromtimestamp(int(message.get("internalDate", 0)) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass
    subject = header(message, "Subject") or "(no subject)"
    return safe_document_name(f"Email {stamp} - {subject}.pdf")


def note_text(document_name: str) -> str:
    """The task note. Phone-like numbers become ``#``: the call system dials them.

    Uses the same pattern ``reject_phone_numbers`` refuses, so the note is
    never refused for a number in a file name.
    """

    from .ezlynx_discussions import _PHONE_LIKE

    return NOTE_PREFIX + _PHONE_LIKE.sub("#", document_name)


# ----------------------------------------------------------------- ledger
class Ledger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY, thread_id TEXT, outcome TEXT,
                applicant_id TEXT, reason TEXT, at TEXT);
            CREATE TABLE IF NOT EXISTS documents (
                doc_key TEXT PRIMARY KEY, message_id TEXT, applicant_id TEXT,
                document_name TEXT, status TEXT, document_id TEXT, at TEXT);
            """
        )
        self.db.commit()

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.db.commit()

    def message_outcome(self, message_id: str) -> str | None:
        row = self.db.execute("SELECT outcome FROM messages WHERE message_id=?", (message_id,)).fetchone()
        return row[0] if row else None

    def record_message(self, message_id: str, thread_id: str, outcome: str, applicant_id: str = "", reason: str = "") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, thread_id, outcome, applicant_id, reason, utc_now()),
        )
        self.db.commit()

    def thread_applicants(self, thread_id: str) -> list[str]:
        rows = self.db.execute(
            "SELECT DISTINCT applicant_id FROM messages WHERE thread_id=? AND outcome='FILED' AND applicant_id!=''",
            (thread_id,),
        ).fetchall()
        return [row[0] for row in rows]

    def document(self, doc_key: str) -> tuple[str, str, str, str] | None:
        row = self.db.execute(
            "SELECT status, document_id, applicant_id, document_name FROM documents WHERE doc_key=?", (doc_key,)
        ).fetchone()
        return tuple(row) if row else None  # type: ignore[return-value]

    def reserve_document(self, doc_key: str, message_id: str, applicant_id: str, document_name: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO documents VALUES (?, ?, ?, ?, 'pending', '', ?)",
            (doc_key, message_id, applicant_id, document_name, utc_now()),
        )
        self.db.commit()

    def confirm_document(self, doc_key: str, document_id: str) -> None:
        self.db.execute(
            "UPDATE documents SET status='filed', document_id=?, at=? WHERE doc_key=?",
            (document_id, utc_now(), doc_key),
        )
        self.db.commit()


# ---------------------------------------------------------------- outside
class Sheet(Protocol):
    def ensure_layout(self) -> None: ...
    def queue_rows(self) -> list[tuple[int, dict[str, str]]]: ...
    def update_queue_result(self, row_number: int, status: str, document_ids: str, error: str) -> None: ...
    def review_rows(self) -> list[tuple[int, dict[str, str]]]: ...
    def append_review(self, values: list[str]) -> None: ...
    def update_review_resolution(self, row_number: int, text: str) -> None: ...
    def append_filed(self, values: list[str]) -> None: ...


class GoogleSheet:
    """Sheets v4 client on the host's Application Default Credentials."""

    def __init__(self, sheet_id: str, service: Any | None = None) -> None:
        self.sheet_id = sheet_id
        if service is None:
            import google.auth
            from googleapiclient.discovery import build

            credentials, _ = google.auth.default(scopes=[SHEETS_SCOPE])
            service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        self.values = service.spreadsheets().values()
        self.sheets = service.spreadsheets()

    def _get(self, rng: str) -> list[list[str]]:
        return self.values.get(spreadsheetId=self.sheet_id, range=rng).execute().get("values", [])

    def _put(self, rng: str, rows: list[list[str]]) -> None:
        self.values.update(
            spreadsheetId=self.sheet_id, range=rng, valueInputOption="RAW", body={"values": rows}
        ).execute()

    def _append(self, tab: str, row: list[str]) -> None:
        self.values.append(
            spreadsheetId=self.sheet_id, range=f"'{tab}'!A1", valueInputOption="RAW",
            insertDataOption="INSERT_ROWS", body={"values": [row]},
        ).execute()

    def ensure_layout(self) -> None:
        meta = self.sheets.get(spreadsheetId=self.sheet_id).execute()
        titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
        missing = [t for t in (QUEUE_TAB, REVIEW_TAB, FILED_TAB, README_TAB) if t not in titles]
        if missing:
            self.sheets.batchUpdate(
                spreadsheetId=self.sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": t}}} for t in missing]},
            ).execute()
        for tab, headers in ((QUEUE_TAB, QUEUE_HEADERS), (REVIEW_TAB, REVIEW_HEADERS), (FILED_TAB, FILED_HEADERS)):
            current = (self._get(f"'{tab}'!A1:Z1") or [[]])[0]
            if [c.strip() for c in current] != headers:
                if any(c.strip() for c in current) and tab == QUEUE_TAB:
                    raise FilerError(f"Queue headers changed: {current}. Expected {headers}.")
                self._put(f"'{tab}'!A1", [headers])
        if not self._get(f"'{README_TAB}'!A1"):
            self._put(f"'{README_TAB}'!A1", [[README_TEXT]])

    def _rows(self, tab: str, headers: list[str]) -> list[tuple[int, dict[str, str]]]:
        last = chr(ord("A") + len(headers) - 1)
        out = []
        for offset, values in enumerate(self._get(f"'{tab}'!A2:{last}"), start=2):
            padded = list(values) + [""] * (len(headers) - len(values))
            row = {name: str(value).strip() for name, value in zip(headers, padded)}
            if any(row.values()):
                out.append((offset, row))
        return out

    def queue_rows(self) -> list[tuple[int, dict[str, str]]]:
        return self._rows(QUEUE_TAB, QUEUE_HEADERS)

    def update_queue_result(self, row_number: int, status: str, document_ids: str, error: str) -> None:
        self._put(f"'{QUEUE_TAB}'!I{row_number}:K{row_number}", [[status, document_ids, error]])

    def review_rows(self) -> list[tuple[int, dict[str, str]]]:
        return self._rows(REVIEW_TAB, REVIEW_HEADERS)

    def append_review(self, values: list[str]) -> None:
        self._append(REVIEW_TAB, values)

    def update_review_resolution(self, row_number: int, text: str) -> None:
        self._put(f"'{REVIEW_TAB}'!I{row_number}", [[text]])

    def append_filed(self, values: list[str]) -> None:
        self._append(FILED_TAB, values)


class Mailbox:
    """Read-only robie@ Gmail over domain-wide delegation."""

    def __init__(self, service: Any) -> None:
        self.service = service

    @classmethod
    def from_env(cls) -> "Mailbox":
        from .gmail_report_ingestion import build_readonly_delegated_service

        account = str(os.environ.get("ROBIE_GMAIL_DELEGATION_SA") or "").strip()
        if not account:
            raise FilerError("ROBIE_GMAIL_DELEGATION_SA is not configured")
        return cls(build_readonly_delegated_service(account, MAILBOX))

    def message(self, message_id: str) -> dict[str, Any]:
        return self.service.users().messages().get(userId="me", id=message_id, format="full").execute()

    def attachment_bytes(self, message_id: str, part: dict[str, Any]) -> bytes:
        body = part.get("body") or {}
        data = body.get("data")
        if not data:
            data = (
                self.service.users().messages().attachments()
                .get(userId="me", messageId=message_id, id=body["attachmentId"]).execute()["data"]
            )
        return urlsafe_b64decode(data + "=" * (-len(data) % 4))

    def new_message_ids(self, after_epoch: int, limit: int) -> list[str]:
        query = f"(in:inbox OR in:sent) after:{after_epoch}"
        ids: list[str] = []
        token = None
        while len(ids) < limit:
            resp = self.service.users().messages().list(
                userId="me", q=query, maxResults=min(100, limit - len(ids)), pageToken=token
            ).execute()
            ids.extend(m["id"] for m in resp.get("messages", []))
            token = resp.get("nextPageToken")
            if not token:
                break
        return list(reversed(ids))  # oldest first, so threads file in order


class Ezlynx:
    """The EZLynx calls the filer uses. All writes read back."""

    def __init__(self, api: Any, discussion_client: Any | None = None, note_ledger: Path | None = None) -> None:
        self.api = api
        self._discussions = discussion_client
        self.note_ledger = note_ledger

    @classmethod
    def from_env(cls, state_dir: Path) -> "Ezlynx":
        from .ezlynx_api import EzlynxApiClient, load_ezlynx_api_config

        return cls(EzlynxApiClient(load_ezlynx_api_config()), note_ledger=state_dir / "discussion-note-ledger.json")

    def find_document(self, applicant_id: str, document_name: str) -> str:
        from .ezlynx_api import extract_document_api_results

        rows = extract_document_api_results(self.api.search_applicant_documents(applicant_id))
        ids = [str(r.get("id")) for r in rows if str(r.get("name") or "").strip() == document_name]
        return ids[0] if len(ids) == 1 else ""

    def upload(self, applicant_id: str, document_name: str, data: bytes, *, policy_master_id: str, content_type: str) -> str:
        from .ezlynx_api_only_writes import upload_document_via_api

        result = upload_document_via_api(
            applicant_id, document_name, data, client=self.api, filename=document_name,
            policy_master_id=policy_master_id or None, file_content_type=content_type,
        )
        return str(result["document_id"])

    def policy_lookup(self, number: str) -> list[tuple[str, str]]:
        from .ascend_notice_driver import _APPLICANT_ID_KEYS, _first_present, _policy_rows, _row_policy_number

        want = re.sub(r"[^A-Z0-9]", "", number.upper())
        try:
            result = self.api.search_policy_by_number(number)
        except Exception as exc:  # noqa: BLE001 - a failed lookup is a miss, not a match
            logger.warning("policy lookup failed for a candidate: %s", type(exc).__name__)
            return []
        out = []
        for row in _policy_rows(result):
            if re.sub(r"[^A-Z0-9]", "", _row_policy_number(row).upper()) == want:
                out.append((_first_present(row, _APPLICANT_ID_KEYS), str(row.get("policyId") or "")))
        return out

    def add_note(self, applicant_id: str, discussion_id: str, text: str) -> str:
        from .ezlynx_discussions import DiscussionApiClient, file_note_to_existing_discussion

        if self._discussions is None:
            from .ascend_notice_driver import discussion_config_from_api_config

            self._discussions = DiscussionApiClient(discussion_config_from_api_config(self.api._config))
        filed = file_note_to_existing_discussion(
            self._discussions, applicant_id, text, discussion_id=discussion_id, ledger_path=self.note_ledger
        )
        if filed.get("status") != "filed":
            raise FilerError(f"note not confirmed: {filed.get('status')} {filed.get('reason') or ''}".strip())
        return str(filed.get("note_id") or "")


# ------------------------------------------------------------------ filer
@dataclass
class Options:
    live: bool = False
    auto: bool = False
    any_applicant: bool = False
    since_days: int = 0


class Filer:
    def __init__(self, *, sheet: Sheet, mailbox: Mailbox, ezlynx: Ezlynx, ledger: Ledger,
                 index: matching.BookIndex, state_dir: Path, options: Options) -> None:
        self.sheet = sheet
        self.mailbox = mailbox
        self.ezlynx = ezlynx
        self.ledger = ledger
        self.index = index
        self.state_dir = state_dir
        self.options = options
        self.report: list[dict[str, Any]] = []

    # -- one document
    def _file_document(self, *, doc_key: str, message_id: str, applicant_id: str, document_name: str,
                       data: bytes, content_type: str, policy_master_id: str, source: str, how: str) -> str:
        existing = self.ledger.document(doc_key)
        if existing and existing[0] == "filed":
            return existing[1]
        if not self.options.live:
            self.report.append({"would_file": document_name, "applicant_id": applicant_id, "bytes": len(data), "how": how})
            return "dry-run"
        if existing and existing[0] == "pending":
            adopted = self.ezlynx.find_document(existing[2], existing[3])
            if adopted:
                self.ledger.confirm_document(doc_key, adopted)
                return adopted
        self.ledger.reserve_document(doc_key, message_id, applicant_id, document_name)
        document_id = self.ezlynx.upload(
            applicant_id, document_name, data, policy_master_id=policy_master_id, content_type=content_type
        )
        self.ledger.confirm_document(doc_key, document_id)
        summary = self._summarize(document_id, document_name, data, content_type)
        self.sheet.append_filed([
            utc_now(), source, message_id, applicant_id, self.index.name(applicant_id), how,
            document_name, document_id, summary,
        ])
        return document_id

    def _summarize(self, document_id: str, document_name: str, data: bytes, content_type: str) -> str:
        if not (content_type == "application/pdf" or document_name.lower().endswith(".pdf")):
            return ""
        if document_name.startswith("Email "):
            return ""
        try:
            from .robie_filer_extract import summarize_pdf

            summary = summarize_pdf(data)
        except Exception as exc:  # noqa: BLE001 - the filing stands without a summary
            return f"summary unavailable: {short_error(exc)}"
        out = self.state_dir / "summaries"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{document_id}.json").write_text(json.dumps(summary.as_dict(), indent=2))
        return summary.one_line()

    def _file_email(self, message: dict[str, Any], applicant_id: str, *, policy_master_id: str,
                    source: str, how: str, include_email_pdf: bool = True) -> list[str]:
        ids = []
        message_id = str(message["id"])
        if include_email_pdf:
            ids.append(self._file_document(
                doc_key=f"{message_id}:email", message_id=message_id, applicant_id=applicant_id,
                document_name=email_document_name(message), data=render_email_pdf(message),
                content_type="application/pdf", policy_master_id=policy_master_id, source=source, how=how,
            ))
        for part in attachment_parts(message):
            ids.append(self._file_attachment(message, part, applicant_id, policy_master_id=policy_master_id,
                                             document_name="", source=source, how=how))
        return ids

    def _file_attachment(self, message: dict[str, Any], part: dict[str, Any], applicant_id: str, *,
                         policy_master_id: str, document_name: str, source: str, how: str) -> str:
        message_id = str(message["id"])
        filename = str(part.get("filename") or "attachment")
        content_type = str(part.get("mimeType") or "") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        name = safe_document_name(document_name or filename)
        data = self.mailbox.attachment_bytes(message_id, part)
        return self._file_document(
            doc_key=f"{message_id}:att:{filename}", message_id=message_id, applicant_id=applicant_id,
            document_name=name, data=data, content_type=content_type, policy_master_id=policy_master_id,
            source=source, how=how,
        )

    # -- 2a / 2b / 2e
    def process_queue(self) -> None:
        for row_number, row in self.sheet.queue_rows():
            if row.get("status") not in ("", PENDING):
                continue
            try:
                ids = self._queue_row(row)
                note_error = ""
                if row.get("discussion_id") and self.options.live:
                    name = row.get("document_name") or row.get("attachment_name") or "email"
                    try:
                        self.ezlynx.add_note(row["applicant_id"], row["discussion_id"], note_text(name))
                    except Exception as exc:  # noqa: BLE001 - the document is filed; say the note failed
                        note_error = f"Document filed; note failed: {short_error(exc)}"
                self.report.append({"queue_row": row_number, "status": FILED, "ids": ids})
                if self.options.live:
                    self.sheet.update_queue_result(row_number, FILED, ", ".join(ids), note_error)
            except Exception as exc:  # noqa: BLE001 - every failure is written to the row
                self.report.append({"queue_row": row_number, "status": ERROR, "error": short_error(exc)})
                if self.options.live:
                    self.sheet.update_queue_result(row_number, ERROR, "", short_error(exc))

    def _queue_row(self, row: dict[str, str]) -> list[str]:
        kind = row.get("type", "").upper()
        applicant = row.get("applicant_id", "")
        if not applicant.isdigit():
            raise FilerError("applicant_id must be the EZLynx applicant number")
        if not row.get("gmail_message_id"):
            raise FilerError("gmail_message_id is required")
        message = self.mailbox.message(row["gmail_message_id"])
        master = row.get("policy_master_id", "")
        if kind == "ATTACHMENT":
            wanted = row.get("attachment_name", "")
            parts = [p for p in attachment_parts(message) if str(p.get("filename")) == wanted]
            if len(parts) != 1:
                names = [str(p.get("filename")) for p in attachment_parts(message)]
                raise FilerError(f"attachment '{wanted}' found {len(parts)} times; attachments are {names}")
            return [self._file_attachment(message, parts[0], applicant, policy_master_id=master,
                                          document_name=row.get("document_name", ""), source="queue", how="sheet row")]
        if kind == "EMAIL":
            return self._file_email(message, applicant, policy_master_id=master, source="queue", how="sheet row")
        raise FilerError("type must be ATTACHMENT or EMAIL")

    # -- Needs review resolutions
    def process_review_resolutions(self) -> None:
        for row_number, row in self.sheet.review_rows():
            applicant = row.get("resolved_applicant_id", "")
            if not applicant or row.get("resolved_by"):
                continue
            try:
                if not applicant.isdigit():
                    raise FilerError("resolved_applicant_id must be the EZLynx applicant number")
                message = self.mailbox.message(row["gmail_message_id"])
                ids = self._file_email(message, applicant, policy_master_id="", source="review", how="person in sheet")
                if self.options.live:
                    self.ledger.record_message(message["id"], message.get("threadId", ""), "FILED", applicant, "resolved in sheet")
                    self.sheet.update_review_resolution(row_number, f"Filed {', '.join(ids)} at {utc_now()}")
                self.report.append({"review_row": row_number, "status": FILED, "ids": ids})
            except Exception as exc:  # noqa: BLE001
                self.report.append({"review_row": row_number, "status": ERROR, "error": short_error(exc)})
                if self.options.live:
                    self.sheet.update_review_resolution(row_number, f"Error: {short_error(exc)}")

    # -- 2c
    def process_new_mail(self) -> None:
        start = self.ledger.meta("auto_start_epoch")
        if self.options.since_days:
            start = str(int(time.time()) - self.options.since_days * 86400)
        elif start is None:
            start = str(int(time.time()))
            if self.options.live:
                self.ledger.set_meta("auto_start_epoch", start)
        for message_id in self.mailbox.new_message_ids(int(start), MAX_AUTO_MESSAGES_PER_RUN):
            if self.ledger.message_outcome(message_id):
                continue
            message = self.mailbox.message(message_id)
            thread = str(message.get("threadId") or "")
            decision = matching.match_message(
                message, header=header, body=body_text(message), index=self.index,
                policy_lookup=self.ezlynx.policy_lookup, thread_applicants=self.ledger.thread_applicants(thread),
            )
            entry = {"message": message_id, "subject": header(message, "Subject")[:80], "outcome": decision.outcome,
                     "reason": decision.reason, "applicant_id": decision.applicant_id, "how": decision.how}
            self.report.append(entry)
            if decision.outcome == matching.SKIPPED:
                if self.options.live:
                    self.ledger.record_message(message_id, thread, "SKIPPED", reason=decision.reason)
                continue
            if decision.outcome == matching.REVIEW:
                if self.options.live:
                    self.sheet.append_review([
                        header(message, "Date"), header(message, "From"), header(message, "Subject"),
                        message_id, thread, decision.reason, ", ".join(decision.candidates), "", "",
                    ])
                    self.ledger.record_message(message_id, thread, "REVIEW", reason=decision.reason)
                continue
            try:
                ids = self._file_email(message, decision.applicant_id, policy_master_id=decision.policy_master_id,
                                       source="auto", how=decision.how)
                entry["ids"] = ids
                if self.options.live:
                    self.ledger.record_message(message_id, thread, "FILED", decision.applicant_id, decision.how)
            except Exception as exc:  # noqa: BLE001 - one message never stops the run
                entry["error"] = short_error(exc)
                if self.options.live:
                    self.sheet.append_review([
                        header(message, "Date"), header(message, "From"), header(message, "Subject"),
                        message_id, thread, f"matched {decision.applicant_id} by {decision.how}; filing failed: {short_error(exc)}",
                        decision.applicant_id, "", "",
                    ])
                    self.ledger.record_message(message_id, thread, "ERROR", decision.applicant_id, short_error(exc))

    def run(self) -> list[dict[str, Any]]:
        if self.options.live:
            self.sheet.ensure_layout()
        self.process_queue()
        self.process_review_resolutions()
        if self.options.auto:
            self.process_new_mail()
        return self.report


# ------------------------------------------------------------------- main
def _register_any_applicant_scope() -> None:
    from . import ezlynx_write_scope

    register = getattr(ezlynx_write_scope, "register_filer_operation_scope", None)
    if register is None:
        raise SystemExit(
            "--any-applicant needs the filer operation scope in ezlynx_write_scope "
            "(not in this release). Writes stay on the applicant allowlist."
        )
    register()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="upload, note, and write the sheet (default: dry run)")
    parser.add_argument("--auto", action="store_true", help="also match and file new robie@ mail")
    parser.add_argument("--any-applicant", action="store_true", help="document upload and note append to any client")
    parser.add_argument("--since-days", type=int, default=0, help="dry-run look-back for --auto (ignores the start mark)")
    parser.add_argument("--sheet-id", default=os.environ.get("ROBIE_FILER_SHEET_ID", DEFAULT_SHEET_ID))
    parser.add_argument("--state-dir", default=os.environ.get("ROBIE_FILER_STATE_DIR", DEFAULT_STATE_DIR))
    parser.add_argument("--index-csv", default=os.environ.get("ROBIE_FILER_INDEX_CSV", DEFAULT_INDEX_CSV))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.live and args.since_days:
        parser.error("--since-days is for dry runs only")
    if args.live and args.auto and not args.any_applicant:
        parser.error("--live --auto needs --any-applicant: matched clients are not on the test allowlist")
    if args.any_applicant:
        _register_any_applicant_scope()

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = open(state_dir / "robie-filer.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.info("another robie-filer run holds the lock; exiting")
        return 0

    index = matching.load_book_index(args.index_csv) if args.auto else matching.BookIndex()
    filer = Filer(
        sheet=GoogleSheet(args.sheet_id), mailbox=Mailbox.from_env(), ezlynx=Ezlynx.from_env(state_dir),
        ledger=Ledger(state_dir / "robie-filer.db"), index=index, state_dir=state_dir,
        options=Options(live=args.live, auto=args.auto, any_applicant=args.any_applicant, since_days=args.since_days),
    )
    report = filer.run()
    print(json.dumps({"live": args.live, "auto": args.auto, "at": utc_now(), "items": report}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
