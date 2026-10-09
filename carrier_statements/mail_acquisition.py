"""Read-only Gmail acquisition into a private, immutable source ledger.

Query exhaustion describes this mailbox/time/query only. It never establishes
carrier-month coverage, printed periods, financial reconciliation or send authority.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any


class AcquisitionError(RuntimeError):
    """Only a fixed reason code may leave the private acquisition boundary."""


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.microsecond:
        raise ValueError("exact_timezone_aware_seconds_required")
    return result.astimezone(timezone.utc)


def validate_config(config: dict) -> dict:
    """Exact private scope; no directory discovery or mailbox fallback."""
    result = dict(config)
    domain = str(result.get("approved_domain", "")).strip().casefold()
    mailbox = str(result.get("mailbox", "")).strip().casefold()
    approved = result.get("approved_mailboxes", [])
    if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,}", domain):
        raise ValueError("approved_domain_required")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._+-]*@" + re.escape(domain), mailbox):
        raise ValueError("mailbox_scope_invalid")
    if not isinstance(approved, list) or mailbox not in approved:
        raise ValueError("mailbox_not_explicitly_approved")
    for key in ("entity_id", "query", "service_account"):
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise ValueError("explicit_scope_required")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*@[a-z0-9][a-z0-9-]*\.iam\.gserviceaccount\.com", result["service_account"]):
        raise ValueError("service_account_reference_required")
    start, end = _time(result["received_start"]), _time(result["received_end"])
    if start >= end or end > datetime.now(timezone.utc):
        raise ValueError("received_window_invalid")
    # Gmail query syntax cannot widen the window outside these parentheses.
    result.update(mailbox=mailbox, approved_domain=domain,
                  received_start=start.isoformat(), received_end=end.isoformat())
    for key, default, ceiling in (("max_pages", 100, 10000), ("max_messages", 10000, 100000),
                                 ("max_raw_bytes", 40_000_000, 100_000_000)):
        value = result.get(key, default)
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError("acquisition_limit_invalid")
        result[key] = value
    return result


def _decode(raw: Any, limit: int) -> bytes:
    if not isinstance(raw, str) or not raw or len(raw) > (limit + 2) * 4 // 3 + 4:
        raise AcquisitionError("RAW_MISSING_OR_TOO_LARGE")
    try:
        blob = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise AcquisitionError("RAW_ENCODING_INVALID") from exc
    if not blob or len(blob) > limit:
        raise AcquisitionError("RAW_MISSING_OR_TOO_LARGE")
    return blob


class SourceLedger:
    """Private local writes only. Originals and run observations are append-only.

    A source hash can appear in multiple observations without a second blob.
    Changed/revised bytes get a new hash; nothing is silently replaced.
    """
    def __init__(self, root: Path):
        root = Path(root)
        if not root.is_absolute():
            raise ValueError("private_absolute_store_required")
        # Never follow an existing path component into another storage location.
        if any(p.is_symlink() for p in (root, *root.parents)):
            raise ValueError("store_symlink_refused")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.stat().st_mode & 0o077:
            raise ValueError("store_must_be_private")
        self.root = root
        for name in ("blobs", "runs"):
            folder = root / name
            if folder.is_symlink():
                raise ValueError("store_symlink_refused")
            folder.mkdir(exist_ok=True, mode=0o700)
            if folder.stat().st_mode & 0o077:
                raise ValueError("store_must_be_private")

    def _append(self, path: Path, data: bytes) -> bool:
        """Link a fully flushed temporary object; never expose a partial object."""
        temporary = path.parent / (".pending-" + uuid.uuid4().hex)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                return True
            except FileExistsError:
                if path.is_symlink() or path.stat().st_mode & 0o077 or path.read_bytes() != data:
                    raise AcquisitionError("EXISTING_SOURCE_CORRUPT_OR_UNSAFE")
                return False
        finally:
            temporary.unlink(missing_ok=True)

    def blob(self, data: bytes, extension: str) -> dict:
        digest = hashlib.sha256(data).hexdigest()
        if extension not in ("eml", "pdf", "xlsx", "xls", "csv", "txt", "bin"):
            raise ValueError("unsafe_blob_extension")
        path = self.root / "blobs" / (digest + "." + extension)
        created = self._append(path, data)
        return {"path": str(path), "sha256": digest, "bytes": len(data), "new_blob": created}

    def journal(self, run_id: str, phase: str, record: dict) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", run_id) or phase not in ("started", "finished"):
            raise ValueError("invalid_journal_identity")
        self._append(self.root / "runs" / (run_id + "." + phase + ".json"),
                     (json.dumps(record, sort_keys=True, indent=2) + "\n").encode())


def _attachments(raw: bytes, store: SourceLedger) -> list[dict]:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    result = []
    for index, part in enumerate(message.walk()):
        if index > 1000:
            raise AcquisitionError("MIME_PART_LIMIT")
        if part.defects:
            raise AcquisitionError("MIME_DEFECT")
        if part.is_multipart():
            if part.get_filename() or part.get_content_disposition() == "attachment":
                raise AcquisitionError("NESTED_ATTACHMENT_REVIEW_REQUIRED")
            continue
        filename = part.get_filename()
        if not filename and part.get_content_disposition() != "attachment":
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes) or not payload or part.defects:
            raise AcquisitionError("ATTACHMENT_INVALID")
        extension = Path(filename or "").suffix.casefold().lstrip(".")
        if extension not in ("pdf", "xlsx", "xls", "csv", "txt"):
            extension = "bin"
        # The original filename is evidence in the private manifest, never a path.
        result.append({"part_index": index, "original_filename": filename,
                       "mime_type": part.get_content_type(), **store.blob(payload, extension)})
    return result


def acquire(config: dict, store: SourceLedger, *, service_factory=None) -> dict:
    """Exhaust bounded Gmail search and preserve exact RFC822 bytes and attachments.

    Use no retries, sends, labels, URL visits or mailbox mutations. Provider
    exception text is suppressed. Failed runs retain their acquired observations.
    """
    config = validate_config(config)
    if service_factory is None:
        from robie_job_engine.gmail_accountability import build_keyless_delegated_service
        service_factory = build_keyless_delegated_service
    run_id = uuid.uuid4().hex
    started = datetime.now(timezone.utc).isoformat()
    start = int(_time(config["received_start"]).timestamp())
    end = int(_time(config["received_end"]).timestamp())
    query = f"({config['query']}) after:{start - 1} before:{end}"
    private = {"run_id": run_id, "started_at": started, "scope": config,
               "effective_query": query, "observations": [], "holds": []}
    store.journal(run_id, "started", {k: v for k, v in private.items() if k != "observations"})
    pages = 0
    query_exhausted = False
    try:
        users = service_factory(config["service_account"], config["mailbox"],
                                scopes=("https://www.googleapis.com/auth/gmail.readonly",)).users()
        profile = users.getProfile(userId="me").execute(num_retries=0)
        if not isinstance(profile, dict) or str(profile.get("emailAddress", "")).casefold() != config["mailbox"]:
            raise AcquisitionError("MAILBOX_IDENTITY_MISMATCH")
        seen_ids, seen_tokens = set(), set()
        token = None
        while True:
            if pages >= config["max_pages"]:
                raise AcquisitionError("PAGE_LIMIT_INCOMPLETE")
            args = {"userId": "me", "q": query, "maxResults": 100, "includeSpamTrash": True}
            if token:
                args["pageToken"] = token
            page = users.messages().list(**args).execute(num_retries=0)
            pages += 1
            if not isinstance(page, dict) or not isinstance(page.get("messages", []), list):
                raise AcquisitionError("SEARCH_RESPONSE_INVALID")
            for reference in page.get("messages", []):
                if not isinstance(reference, dict) or not isinstance(reference.get("id"), str) or not reference["id"]:
                    raise AcquisitionError("MESSAGE_REFERENCE_INVALID")
                mid = reference["id"]
                if mid in seen_ids:
                    raise AcquisitionError("DUPLICATE_PAGE_MESSAGE_INCOMPLETE")
                if len(seen_ids) >= config["max_messages"]:
                    raise AcquisitionError("MESSAGE_LIMIT_INCOMPLETE")
                seen_ids.add(mid)
                original = users.messages().get(userId="me", id=mid, format="raw").execute(num_retries=0)
                if not isinstance(original, dict) or original.get("id") != mid:
                    raise AcquisitionError("MESSAGE_ID_MISMATCH")
                if reference.get("threadId") and original.get("threadId") != reference["threadId"]:
                    raise AcquisitionError("MESSAGE_THREAD_MISMATCH")
                stamp = original.get("internalDate")
                if not isinstance(stamp, str) or not stamp.isdigit() or not start * 1000 <= int(stamp) < end * 1000:
                    raise AcquisitionError("RECEIVED_TIME_UNVERIFIED")
                blob = _decode(original.get("raw"), config["max_raw_bytes"])
                observation = {"entity_id": config["entity_id"], "mailbox": config["mailbox"],
                               "message_id": mid, "thread_id": original.get("threadId"),
                               "received_milliseconds": stamp, "retrieved_at": datetime.now(timezone.utc).isoformat(),
                               "original": store.blob(blob, "eml"), "attachments": [],
                               "printed_period": None, "carrier_id": None,
                               "source_classification": "UNVERIFIED"}
                private["observations"].append(observation)
                observation["attachments"] = _attachments(blob, store)
            next_token = page.get("nextPageToken")
            if next_token is None or next_token == "":
                query_exhausted = True
                break
            if not isinstance(next_token, str) or next_token in seen_tokens:
                raise AcquisitionError("PAGE_TOKEN_LOOP_OR_INVALID")
            seen_tokens.add(next_token)
            token = next_token
    except AcquisitionError as exc:
        private["holds"].append(str(exc))
    except Exception:
        private["holds"].append("SOURCE_READ_OR_STORAGE_FAILED")
    summary = {"run_id": run_id, "finished_at": datetime.now(timezone.utc).isoformat(),
               "status": "QUERY_ACQUIRED_REVIEW_ONLY" if query_exhausted and not private["holds"] else "UNVERIFIED",
               "query_exhausted": query_exhausted, "pages_read": pages,
               "messages_preserved": len(private["observations"]),
               "attachments_preserved": sum(len(x["attachments"]) for x in private["observations"]),
               "holds": private["holds"], "carrier_month_coverage": "UNVERIFIED",
               "complete_source_inventory": False, "financial_reconciled": False,
               "source_system_writes": 0, "send_enabled": False}
    # If the finished journal cannot be committed, no success is returned.
    store.journal(run_id, "finished", {**private, "summary": summary})
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="private reviewed JSON scope")
    parser.add_argument("--store", required=True, help="private absolute local evidence folder")
    parser.add_argument("--live-read", action="store_true")
    args = parser.parse_args(argv)
    if not args.live_read:
        parser.error("no reads performed; explicit --live-read is required")
    try:
        path = Path(args.config)
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("configuration_must_be_private")
        config = validate_config(json.loads(path.read_text()))
        result = acquire(config, SourceLedger(Path(args.store)))
    except Exception:
        print(json.dumps({"status": "UNVERIFIED", "holds": ["CONFIGURATION_OR_STORAGE_FAILED"]}))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "QUERY_ACQUIRED_REVIEW_ONLY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
