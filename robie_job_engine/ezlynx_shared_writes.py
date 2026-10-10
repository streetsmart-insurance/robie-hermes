"""Shared Job Engine job types for EZLynx writes: one way to file, one proof.

Every Robie worker that has to put a document or a note into EZLynx creates
one of these jobs instead of calling DocumentApi or DiscussionApi itself.

``ezlynx.document_upload``
    Done means the exact document (same name, same SHA-256 bytes) is on that
    applicant, shown by a fresh DocumentApi search that read every page
    (``complete`` is True) and a fresh download whose hash matches. A list
    that was not read in full is UNVERIFIED and the file is never uploaded
    again on a guess.

``ezlynx.note_append``
    Done means exactly one new note with exactly that text is in that
    discussion. EZLynx's ``POST .../notes`` returns no note id, so the worker
    records every note id in the discussion before the post (a read proven
    complete) and the verifier accepts exactly one id that was not there
    before, carrying exactly that normalized text. Never reposted on resume.

How a write is kept to one:

- The job's idempotency key is the business identity (applicant + file
  SHA-256 + document name; discussion + body SHA-256 + caller job id), so a
  rerun finds the existing job instead of starting a second one.
- Before the POST the worker inserts a ``ezlynx_write_intent`` checkpoint
  (a durable compare-and-set) holding the pre-write snapshot of ids. Once that
  row exists the worker never POSTs again for the job, whatever happens.
- A crash between the POST and the read-back leaves the intent behind. The
  verifier works from that snapshot and its own fresh read, never from the
  worker's report, so the job ends COMPLETE (it landed once) or UNVERIFIED
  (it cannot be shown), and is never reposted.

The verifiers read through ``EzlynxApiClientReadPort`` (search, download,
discussion reads). The write scope (``ezlynx_write_scope``) is checked before
any write and again inside the real clients.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult

DOCUMENT_UPLOAD = "ezlynx.document_upload"
NOTE_APPEND = "ezlynx.note_append"
SHARED_WRITE_ACTIONS = frozenset({DOCUMENT_UPLOAD, NOTE_APPEND})
DOCUMENT_UPLOAD_WORKER = "ezlynx-document-upload"
NOTE_APPEND_WORKER = "ezlynx-note-append"
INTENT_KIND = "ezlynx_write_intent"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Evidence keys the JobStore requires before it allows COMPLETE.
REQUIRED_EVIDENCE: dict[str, dict[str, Any]] = {
    DOCUMENT_UPLOAD: {
        "applicant_id": None,
        "document_id": None,
        "document_name": None,
        "sha256": None,
        "search_complete": True,
    },
    NOTE_APPEND: {
        "applicant_id": None,
        "discussion_id": None,
        "note_id": None,
        "body_norm_sha256": None,
        "read_complete": True,
        "new_matching_notes": 1,
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_hex(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------- note helpers
# Shared with task_assignment_worker (#831): notes are told apart by the one
# new id with exactly the text Robie sent.


def norm_note_text(text: Any) -> str:
    """Whitespace-normalized note text. The comparison form for every note."""
    return " ".join(str(text or "").split())


def body_norm_sha256(body: Any) -> str:
    return sha256_hex(norm_note_text(body))


def complete_note_ids(client: Any, discussion_id: str) -> list[str] | None:
    """Every note id in the discussion, or None when the read is not provably whole."""
    from .ezlynx_discussions import (
        _note_id_of,
        discussion_note_snapshot,
        iter_discussion_notes,
        with_notes_read_is_complete,
    )

    reader = getattr(client, "get_discussion_with_notes", None)
    if not callable(reader):
        return None
    try:
        plain = discussion_note_snapshot(client.get_discussion(discussion_id))
        if plain.get("note_count") == 0:
            return []
        record = reader(discussion_id)
    except Exception:  # noqa: BLE001 - no proof means not complete
        return None
    if not with_notes_read_is_complete(record, plain):
        return None
    return sorted({_note_id_of(row) for row in iter_discussion_notes(record) if _note_id_of(row)})


def new_note_ids_with_text(
    client: Any, prior_ids: Any, discussion_id: str, body: str
) -> list[str] | None:
    """Ids not in ``prior_ids`` whose text is exactly ``body``.

    None when there is no recorded snapshot or the read after the post is not
    provably whole. The caller decides what one, none, or several mean.
    """
    from .ezlynx_discussions import _note_body, _note_id_of, iter_discussion_notes

    if not isinstance(prior_ids, list):
        return None
    after = complete_note_ids(client, discussion_id)
    reader = getattr(client, "get_discussion_with_notes", None)
    if after is None or not callable(reader):
        return None
    before = {str(x) for x in prior_ids}
    try:
        record = reader(discussion_id)
    except Exception:  # noqa: BLE001
        return None
    want = norm_note_text(body)
    return [
        _note_id_of(row)
        for row in iter_discussion_notes(record)
        if _note_id_of(row)
        and _note_id_of(row) not in before
        and _note_id_of(row) in after
        and norm_note_text(_note_body(row)) == want
    ]


# ------------------------------------------------------------ document helpers


def document_search_read_every_page(payload: Any) -> bool:
    """True only when the search says, explicitly, that every row was read.

    ``document_search_is_complete`` is False only when ``complete`` is False.
    Done needs the positive statement, so a payload without ``complete``
    (an old client or an unpaged fake) is not proof either.
    """
    from .ezlynx_api import document_search_is_complete

    return (
        isinstance(payload, dict)
        and payload.get("complete") is True
        and document_search_is_complete(payload)
    )


def _document_rows(payload: Any) -> list[dict[str, str]]:
    from .ezlynx_api import extract_document_api_results

    return extract_document_api_results(payload)


def _search(reader: Any, applicant_id: str) -> Any:
    """A fresh DocumentApi search payload (with ``complete``/``pages_read``)."""
    search = getattr(reader, "document_search", None)
    if callable(search):
        return search(applicant_id)
    return reader.search_applicant_documents(applicant_id)


def _download(reader: Any, document_id: str) -> bytes:
    body = reader.download_document(document_id)
    return getattr(body, "body", body) or b""


# ------------------------------------------------------------- job identities


def document_upload_key(applicant_id: str, file_sha256: str, document_name: str) -> str:
    raw = json.dumps(
        [DOCUMENT_UPLOAD, str(applicant_id).strip(), str(file_sha256).strip().lower(),
         str(document_name).strip()],
        separators=(",", ":"),
    )
    return f"{DOCUMENT_UPLOAD}:{sha256_hex(raw)}"


def note_append_key(discussion_id: str, body_sha: str, caller_job_id: str) -> str:
    raw = json.dumps(
        [NOTE_APPEND, str(discussion_id).strip(), str(body_sha).strip().lower(),
         str(caller_job_id).strip()],
        separators=(",", ":"),
    )
    return f"{NOTE_APPEND}:{sha256_hex(raw)}"


def document_resource_id(applicant_id: str) -> str:
    return f"ezlynx-applicant:{str(applicant_id).strip()}/documents"


def discussion_resource_id(discussion_id: str) -> str:
    return f"ezlynx-discussion:{str(discussion_id).strip()}"


def ensure_document_upload_job(
    store: Any,
    *,
    applicant_id: str,
    document_name: str,
    file_path: str | Path,
    caller: str,
    filename: str | None = None,
    policy_master_id: str | None = None,
    content_type: str = "application/octet-stream",
    file_sha256: str | None = None,
) -> dict[str, Any]:
    """Create the upload job, or return the existing one for the same document."""
    applicant = str(applicant_id or "").strip()
    name = str(document_name or "").strip()
    path = Path(file_path)
    if not applicant or not name:
        raise ValueError("applicant id and document name are required")
    digest = str(file_sha256 or "").strip().lower() or sha256_hex(path.read_bytes())
    payload = {
        "worker": DOCUMENT_UPLOAD_WORKER,
        "resource_id": document_resource_id(applicant),
        "applicant_id": applicant,
        "document_name": name,
        "file_path": str(path),
        "file_sha256": digest,
        "filename": filename or path.name,
        "policy_master_id": str(policy_master_id or ""),
        "content_type": content_type,
        "caller": str(caller or "").strip(),
    }
    return store.create_job(
        DOCUMENT_UPLOAD,
        payload,
        idempotency_key=document_upload_key(applicant, digest, name),
    )


def ensure_note_append_job(
    store: Any,
    *,
    applicant_id: str,
    discussion_id: str,
    body: str,
    caller_job_id: str,
    note_type: str = "Note",
) -> dict[str, Any]:
    """Create the note job, or return the existing one for the same note."""
    applicant = str(applicant_id or "").strip()
    discussion = str(discussion_id or "").strip()
    caller = str(caller_job_id or "").strip()
    text = str(body or "").strip()
    if not applicant or not discussion or not caller or not text:
        raise ValueError("applicant id, discussion id, caller job id and body are required")
    digest = body_norm_sha256(text)
    payload = {
        "worker": NOTE_APPEND_WORKER,
        "resource_id": discussion_resource_id(discussion),
        "applicant_id": applicant,
        "discussion_id": discussion,
        "body": text,
        "body_norm_sha256": digest,
        "caller_job_id": caller,
        "note_type": note_type,
    }
    return store.create_job(
        NOTE_APPEND, payload, idempotency_key=note_append_key(discussion, digest, caller)
    )


# ------------------------------------------------------------- write intent


class IntentExists(Exception):
    """A write intent for this job is already recorded; never write again."""


def reserve_intent(store: Any, job_id: str, intent: dict[str, Any]) -> dict[str, Any]:
    """Insert the intent once. Raises IntentExists when any intent is recorded."""
    from .store import canonical_json

    with store.transaction() as conn:
        row = conn.execute(
            "SELECT data_json FROM checkpoints WHERE job_id=? AND kind=?",
            (job_id, INTENT_KIND),
        ).fetchone()
        if row is not None:
            raise IntentExists(json.loads(row[0]).get("state") or "recorded")
        conn.execute(
            "INSERT INTO checkpoints(job_id,kind,data_json,created_at) VALUES(?,?,?,?)",
            (job_id, INTENT_KIND, canonical_json(intent), utc_now()),
        )
    if store.get_checkpoint(job_id, INTENT_KIND) != intent:
        raise IntentExists("intent persistence could not be confirmed")
    return intent


def _scope_refusal(applicant_id: str) -> str | None:
    from .ezlynx_write_scope import EZLYNX_WRITE_SCOPE_REFUSED, applicant_is_write_allowed

    if applicant_is_write_allowed(applicant_id):
        return None
    return (
        f"{EZLYNX_WRITE_SCOPE_REFUSED}: applicant {applicant_id or '<missing>'} is not "
        "on the EZLynx business-write allowlist; nothing was written"
    )


def _refused(action: str, error: str) -> WorkerResult:
    return WorkerResult(False, action, {}, retryable=False, error=error)


def _destination(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get("payload") or {}
    out = {"resource_id": payload.get("resource_id"), "applicant_id": payload.get("applicant_id")}
    if payload.get("discussion_id"):
        out["discussion_id"] = payload["discussion_id"]
    if payload.get("document_name"):
        out["document_name"] = payload["document_name"]
    return out


# ------------------------------------------------------------ upload worker


class EzlynxDocumentUploadWorker:
    """Uploads one file through DocumentApi. Reports what it did, never done."""

    def __init__(self, store: Any, client: Any | None = None,
                 client_factory: Callable[[], Any] | None = None):
        self.store = store
        self._client = client
        self._factory = client_factory

    def _api(self) -> Any:
        if self._client is None:
            if self._factory is not None:
                self._client = self._factory()
            else:
                from .ezlynx_api import EzlynxApiClient, load_ezlynx_api_config

                self._client = EzlynxApiClient(load_ezlynx_api_config())
        return self._client

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        payload = job.get("payload") or {}
        applicant = str(payload.get("applicant_id") or "").strip()
        name = str(payload.get("document_name") or "").strip()
        want_sha = str(payload.get("file_sha256") or "").strip().lower()
        if idempotency_key != document_upload_key(applicant, want_sha, name):
            return _refused(DOCUMENT_UPLOAD, "job identity does not match applicant, file hash and name")
        refusal = _scope_refusal(applicant)
        if refusal:
            return _refused(DOCUMENT_UPLOAD, refusal)
        intent = self.store.get_checkpoint(job["id"], INTENT_KIND)
        if intent is not None:
            # A write may already have happened. Never upload again: the
            # verifier decides from its own read.
            return self._resumed(job, intent)
        try:
            data = Path(str(payload.get("file_path") or "")).read_bytes()
        except OSError as exc:
            return _refused(DOCUMENT_UPLOAD, f"file cannot be read: {type(exc).__name__}; nothing was uploaded")
        if not data or len(data) > MAX_UPLOAD_BYTES:
            return _refused(DOCUMENT_UPLOAD, "file is empty or too large; nothing was uploaded")
        if sha256_hex(data) != want_sha:
            return _refused(DOCUMENT_UPLOAD, "file bytes changed since the job was created; nothing was uploaded")
        api = self._api()
        try:
            listing = api.search_applicant_documents(applicant)
        except Exception as exc:  # noqa: BLE001
            return WorkerResult(False, DOCUMENT_UPLOAD, {}, retryable=True,
                                error=f"document list read failed before upload: {type(exc).__name__}")
        if not document_search_read_every_page(listing):
            return WorkerResult(
                False, DOCUMENT_UPLOAD, {}, retryable=True,
                error="the client's document list could not be read in full; nothing was uploaded",
            )
        rows = _document_rows(listing)
        prior = sorted({row["id"] for row in rows})
        same = []
        for row in rows:
            if row.get("name") == name:
                try:
                    if sha256_hex(_download(api, row["id"])) == want_sha:
                        same.append(row["id"])
                except Exception:  # noqa: BLE001 - unreadable is not "the same"
                    continue
        record = {
            "action": DOCUMENT_UPLOAD,
            "applicant_id": applicant,
            "document_name": name,
            "file_sha256": want_sha,
            "prior_document_ids": prior,
            "reserved_at": utc_now(),
            "document_id": "",
            "state": "reserved",
        }
        if len(same) == 1:
            # The exact document is already filed: adopt it, upload nothing.
            record.update({"document_id": same[0], "adopted_existing": True, "state": "adopted"})
        try:
            reserve_intent(self.store, job["id"], record)
        except IntentExists:
            return self._resumed(job, self.store.get_checkpoint(job["id"], INTENT_KIND) or {})
        if record["state"] == "adopted":
            return WorkerResult(True, DOCUMENT_UPLOAD, _destination(job),
                                detail={"document_id": same[0], "adopted_existing": True,
                                        "file_sha256": want_sha})
        from .ezlynx_write_scope import EzlynxWriteScopeError

        try:
            document_id = str(api.upload_applicant_document(
                applicant, name, data,
                filename=str(payload.get("filename") or "") or None,
                policy_master_id=str(payload.get("policy_master_id") or "") or None,
                file_content_type=str(payload.get("content_type") or "application/octet-stream"),
            ) or "").strip()
        except EzlynxWriteScopeError as exc:
            # The client refused before any request: provably not written.
            self.store.checkpoint(job["id"], INTENT_KIND, {**record, "state": "refused_before_send"})
            return _refused(DOCUMENT_UPLOAD, str(exc))
        # Any other exception propagates: the engine records the outcome as
        # unknown and the verifier decides from a fresh read.
        record.update({"document_id": document_id, "state": "posted", "posted_at": utc_now()})
        self.store.checkpoint(job["id"], INTENT_KIND, record)
        return WorkerResult(True, DOCUMENT_UPLOAD, _destination(job),
                            detail={"document_id": document_id, "file_sha256": want_sha})

    def _resumed(self, job: dict[str, Any], intent: dict[str, Any]) -> WorkerResult:
        if intent.get("state") == "refused_before_send":
            return _refused(DOCUMENT_UPLOAD, "the earlier attempt was refused before sending; nothing was uploaded")
        return WorkerResult(
            True, DOCUMENT_UPLOAD, _destination(job),
            detail={"document_id": str(intent.get("document_id") or ""),
                    "resumed_without_rewrite": True},
        )


# -------------------------------------------------------------- note worker


class EzlynxNoteAppendWorker:
    """Appends one note through DiscussionApi. Reports what it did, never done."""

    def __init__(self, store: Any, client: Any | None = None,
                 client_factory: Callable[[], Any] | None = None):
        self.store = store
        self._client = client
        self._factory = client_factory

    def _discussions(self) -> Any:
        if self._client is None:
            if self._factory is not None:
                self._client = self._factory()
            else:
                from .ezlynx_api_only_writes import load_discussion_api_config
                from .ezlynx_discussions import DiscussionApiClient

                self._client = DiscussionApiClient(load_discussion_api_config())
        return self._client

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        payload = job.get("payload") or {}
        applicant = str(payload.get("applicant_id") or "").strip()
        discussion = str(payload.get("discussion_id") or "").strip()
        body = str(payload.get("body") or "")
        digest = str(payload.get("body_norm_sha256") or "")
        caller = str(payload.get("caller_job_id") or "")
        if idempotency_key != note_append_key(discussion, digest, caller):
            return _refused(NOTE_APPEND, "job identity does not match discussion, text hash and caller")
        if not norm_note_text(body) or body_norm_sha256(body) != digest:
            return _refused(NOTE_APPEND, "note text does not match its recorded hash; nothing was sent")
        refusal = _scope_refusal(applicant)
        if refusal:
            return _refused(NOTE_APPEND, refusal)
        intent = self.store.get_checkpoint(job["id"], INTENT_KIND)
        if intent is not None:
            return self._resumed(job, intent)
        from .ezlynx_discussions import reject_phone_numbers

        try:
            reject_phone_numbers(body)
        except Exception as exc:  # noqa: BLE001
            return _refused(NOTE_APPEND, f"note refused before sending: {exc}")
        client = self._discussions()
        try:
            owned = [str(item).strip() for item in client.get_discussion_ids(applicant)]
        except Exception as exc:  # noqa: BLE001
            return WorkerResult(False, NOTE_APPEND, {}, retryable=True,
                                error=f"discussion ownership read failed: {type(exc).__name__}")
        if owned.count(discussion) != 1:
            return _refused(
                NOTE_APPEND,
                f"discussion {discussion} is not one of applicant {applicant}'s discussions; nothing was sent",
            )
        prior = complete_note_ids(client, discussion)
        if prior is None:
            return WorkerResult(
                False, NOTE_APPEND, {}, retryable=True,
                error="the discussion's notes could not be read in full, so the new note "
                      "could not be told apart afterwards; nothing was sent",
            )
        record = {
            "action": NOTE_APPEND,
            "applicant_id": applicant,
            "discussion_id": discussion,
            "body_norm_sha256": digest,
            "prior_note_ids": prior,
            "reserved_at": utc_now(),
            "note_id": "",
            "state": "reserved",
        }
        try:
            reserve_intent(self.store, job["id"], record)
        except IntentExists:
            return self._resumed(job, self.store.get_checkpoint(job["id"], INTENT_KIND) or {})
        from .ezlynx_write_scope import EzlynxWriteScopeError

        try:
            client.append_note(discussion, body, applicant_id=applicant)
        except EzlynxWriteScopeError as exc:
            self.store.checkpoint(job["id"], INTENT_KIND, {**record, "state": "refused_before_send"})
            return _refused(NOTE_APPEND, str(exc))
        # Other exceptions propagate (outcome unknown; verifier decides).
        record.update({"state": "posted", "posted_at": utc_now()})
        matches = new_note_ids_with_text(client, prior, discussion, body)
        if matches is not None and len(matches) == 1:
            record["note_id"] = matches[0]
        self.store.checkpoint(job["id"], INTENT_KIND, record)
        return WorkerResult(True, NOTE_APPEND, _destination(job),
                            detail={"note_id": record["note_id"], "body_norm_sha256": digest})

    def _resumed(self, job: dict[str, Any], intent: dict[str, Any]) -> WorkerResult:
        if intent.get("state") == "refused_before_send":
            return _refused(NOTE_APPEND, "the earlier attempt was refused before sending; nothing was sent")
        return WorkerResult(
            True, NOTE_APPEND, _destination(job),
            detail={"note_id": str(intent.get("note_id") or ""), "resumed_without_rewrite": True},
        )


# ---------------------------------------------------------------- verifiers


def _evidence(method: str, source: str, expected: dict[str, Any], observed: dict[str, Any],
              captured: str, locator: str | None) -> VerificationEvidence:
    return VerificationEvidence(
        method=method, source=source, expected=expected, observed=observed,
        authoritative=True, captured_at=captured, locator=locator,
    )


def _claimed_id(action: dict[str, Any], intent: dict[str, Any], key: str) -> str:
    detail = (action or {}).get("detail") or {}
    return str(detail.get(key) or intent.get(key) or "").strip()


def _bound_applicant_problem(job: dict[str, Any], action: dict[str, Any]) -> str | None:
    """The worker cannot point verification at a different client."""
    payload = job.get("payload") or {}
    claimed = str(((action or {}).get("destination") or {}).get("applicant_id") or "").strip()
    bound = str(payload.get("applicant_id") or "").strip()
    if claimed and claimed != bound:
        return f"action names applicant {claimed} but the job is bound to {bound}"
    return None


def _default_read_port() -> Any:
    from .ezlynx_api_read_port import EzlynxApiClientReadPort

    return EzlynxApiClientReadPort()


class EzlynxDocumentUploadVerifier:
    """Fresh DocumentApi search (every page) plus a fresh download hash."""

    METHOD = "ezlynx-documentapi-search-and-download"

    def __init__(self, store: Any, port: Any | None = None,
                 port_factory: Callable[[], Any] | None = None):
        self.store = store
        self._port = port
        self._factory = port_factory or _default_read_port

    def _read(self) -> Any:
        if self._port is None:
            self._port = self._factory()
        return self._port

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        payload = job.get("payload") or {}
        applicant = str(payload.get("applicant_id") or "").strip()
        name = str(payload.get("document_name") or "").strip()
        want_sha = str(payload.get("file_sha256") or "").strip().lower()
        resource = document_resource_id(applicant)
        intent = self.store.get_checkpoint(job["id"], INTENT_KIND) or {}
        claimed = _claimed_id(action, intent, "document_id")
        captured = utc_now()
        expected: dict[str, Any] = {
            "resource_id": resource, "applicant_id": applicant, "document_name": name,
            "sha256": want_sha, "search_complete": True,
        }

        def fail(observed: dict[str, Any], error: str, *, retryable: bool = False) -> VerificationResult:
            ev = _evidence(self.METHOD, "DocumentApi", {**expected, "document_id": claimed},
                           observed, captured, resource)
            return VerificationResult(False, ev, retryable=retryable, error=error)

        problem = _bound_applicant_problem(job, action)
        if problem:
            return fail({}, problem)
        if not intent:
            return fail({}, "no write intent was recorded, so nothing can be confirmed")
        if intent.get("state") == "refused_before_send":
            return fail({}, "the upload was refused before sending")
        try:
            listing = _search(self._read(), applicant)
        except Exception as exc:  # noqa: BLE001
            return fail({"error": f"document search failed: {type(exc).__name__}"},
                        "document search failed", retryable=True)
        observed: dict[str, Any] = {
            "resource_id": resource,
            "applicant_id": applicant,
            "search_complete": document_search_read_every_page(listing),
            "pages_read": listing.get("pages_read") if isinstance(listing, dict) else None,
            "total_on_file": listing.get("totalSize") if isinstance(listing, dict) else None,
        }
        if not observed["search_complete"]:
            return fail(observed, "UNVERIFIED: the document list was not read in full; "
                                  "do not upload again, check first")
        rows = _document_rows(listing)
        prior = {str(x) for x in intent.get("prior_document_ids") or []}
        if intent.get("adopted_existing"):
            candidates = [row for row in rows if row["id"] == str(intent.get("document_id") or "")]
        else:
            candidates = [row for row in rows if row["id"] not in prior]
        candidates = [row for row in candidates if row.get("name") == name]
        matches = []
        for row in candidates:
            try:
                if sha256_hex(_download(self._read(), row["id"])) == want_sha:
                    matches.append(row["id"])
            except Exception:  # noqa: BLE001 - unreadable bytes are not proof
                continue
        observed["matching_document_ids"] = matches
        if len(matches) != 1:
            what = "no" if not matches else f"{len(matches)}"
            return fail(observed, f"{what} new document with this name and these exact bytes is on "
                                  f"applicant {applicant}; not uploading again")
        found = matches[0]
        if claimed and claimed != found:
            return fail(observed, f"the upload reported document {claimed} but the matching document is {found}")
        observed.update({"document_id": found, "document_name": name, "sha256": want_sha})
        expected["document_id"] = found
        ev = _evidence(self.METHOD, "DocumentApi", expected, observed, captured, f"{resource}/{found}")
        return VerificationResult(True, ev)


class EzlynxNoteAppendVerifier:
    """Fresh full discussion read: exactly one new note with exactly this text."""

    METHOD = "ezlynx-discussion-new-note-readback"

    def __init__(self, store: Any, port: Any | None = None,
                 port_factory: Callable[[], Any] | None = None):
        self.store = store
        self._port = port
        self._factory = port_factory or _default_read_port

    def _read(self) -> Any:
        if self._port is None:
            self._port = self._factory()
        return self._port

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        payload = job.get("payload") or {}
        applicant = str(payload.get("applicant_id") or "").strip()
        discussion = str(payload.get("discussion_id") or "").strip()
        body = str(payload.get("body") or "")
        digest = str(payload.get("body_norm_sha256") or "")
        resource = discussion_resource_id(discussion)
        intent = self.store.get_checkpoint(job["id"], INTENT_KIND) or {}
        claimed = _claimed_id(action, intent, "note_id")
        captured = utc_now()
        expected: dict[str, Any] = {
            "resource_id": resource, "applicant_id": applicant, "discussion_id": discussion,
            "body_norm_sha256": digest, "read_complete": True, "new_matching_notes": 1,
        }

        def fail(observed: dict[str, Any], error: str, *, retryable: bool = False) -> VerificationResult:
            ev = _evidence(self.METHOD, "DiscussionApi", {**expected, "note_id": claimed},
                           observed, captured, resource)
            return VerificationResult(False, ev, retryable=retryable, error=error)

        problem = _bound_applicant_problem(job, action)
        if problem:
            return fail({}, problem)
        if body_norm_sha256(body) != digest:
            return fail({}, "note text does not match its recorded hash")
        if not isinstance(intent.get("prior_note_ids"), list):
            return fail({}, "no pre-post note snapshot was recorded, so a new note cannot be told apart")
        if intent.get("state") == "refused_before_send":
            return fail({}, "the note was refused before sending")
        port = self._read()
        lookup = getattr(port, "discussion_ids_for_applicant", None) or getattr(port, "get_discussion_ids", None)
        if not callable(lookup):
            return fail({}, "verifier cannot prove the discussion belongs to the applicant")
        try:
            owned = [str(item).strip() for item in lookup(applicant)]
        except Exception as exc:  # noqa: BLE001
            return fail({"error": f"ownership read failed: {type(exc).__name__}"},
                        "discussion ownership read failed", retryable=True)
        if owned.count(discussion) != 1:
            return fail({"discussion_owned": False},
                        f"discussion {discussion} is not one of applicant {applicant}'s discussions")
        matches = new_note_ids_with_text(port, intent["prior_note_ids"], discussion, body)
        observed: dict[str, Any] = {
            "resource_id": resource, "applicant_id": applicant, "discussion_id": discussion,
            "read_complete": matches is not None,
        }
        if matches is None:
            return fail(observed, "UNVERIFIED: the discussion's notes were not read in full; "
                                  "the note is not posted again")
        observed["new_matching_notes"] = len(matches)
        observed["matching_note_ids"] = matches
        if len(matches) != 1:
            return fail(observed, f"{len(matches)} new notes with exactly this text are in the "
                                  "discussion (one is required); the note is not posted again")
        found = matches[0]
        if claimed and claimed != found:
            return fail(observed, f"the worker reported note {claimed} but the new note is {found}")
        observed.update({"note_id": found, "body_norm_sha256": digest})
        expected["note_id"] = found
        ev = _evidence(self.METHOD, "DiscussionApi", expected, observed, captured, f"{resource}/note:{found}")
        return VerificationResult(True, ev)


# ------------------------------------------------------------ COMPLETE guard


def shared_write_evidence_missing(
    action_type: str | None,
    expected: dict[str, Any] | None,
    observed: dict[str, Any] | None,
) -> str | None:
    """Refuse COMPLETE for a shared write job without its exact proof."""
    required = REQUIRED_EVIDENCE.get(str(action_type or ""))
    if required is None:
        return None
    expected = dict(expected or {})
    observed = dict(observed or {})
    for key, value in required.items():
        for side, blob in (("expected", expected), ("observed", observed)):
            got = blob.get(key)
            if value is None:
                if got in (None, "") or isinstance(got, bool):
                    return f"COMPLETE prohibited: {action_type} evidence has no {side} {key}"
            elif got != value or isinstance(got, bool) != isinstance(value, bool):
                return f"COMPLETE prohibited: {action_type} evidence {side} {key}={got!r}, need {value!r}"
        if expected.get(key) != observed.get(key):
            return f"COMPLETE prohibited: {action_type} expected {key} differs from observed"
    if action_type == DOCUMENT_UPLOAD and not str(observed.get("document_id")).isdigit():
        return "COMPLETE prohibited: document_id is not a DocumentApi id"
    return None


# --------------------------------------------------------------- run helper


def run_shared_write_job(store: Any, job_id: str, engine: Any) -> dict[str, Any]:
    """Run (or resume) the job once and return it. Terminal jobs are not rerun."""
    from .models import TERMINAL_STATUSES

    job = store.get_job(job_id)
    if JobStatus(job["status"]) in TERMINAL_STATUSES:
        return job
    return engine.run(job_id)


def verified_destination_id(store: Any, job_id: str, key: str) -> str:
    """The id the verifier itself observed (never the worker's report)."""
    for item in reversed(store.list_evidence(job_id)):
        if item.get("verified") and item.get("authoritative"):
            observed = item.get("observed") or {}
            return str(observed.get(key) or "")
    return ""
