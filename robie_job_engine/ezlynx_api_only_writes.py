"""EZLynx notes and documents are API-only (Carlo 2026-09-19).

If something would save a note or a document to EZLynx, it MUST use the
Notes/Discussion API and the Document API. Playwright and CDP may drive
forms and carrier portals. They must never file a note or upload a
document. COMPLETE is refused unless the write produced a DiscussionApi
``note_id`` / ``ezlynx_note_id`` or a DocumentApi ``document_id`` and a
fresh read-back confirmed that id.

This module is the fail-closed contract:

- Detect Playwright / CDP note and document write attempts.
- Route allowed writes through ``add_note_to_discussion`` and
  DocumentApi upload, then read the destination before success.
- Refuse COMPLETE when a note/doc write is claimed without those API ids,
  or when a Playwright path was used for the write.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

PLAYWRIGHT_BLOCKED = "PLAYWRIGHT_BLOCKED"
EZLYNX_NOTE_DOC_API_ONLY = "EZLYNX_NOTE_DOC_API_ONLY"

NOTE_API_ID_KEYS = ("note_id", "ezlynx_note_id", "discussion_note_id")
DOCUMENT_API_ID_KEYS = ("document_id", "document_ids")
NOTE_CLAIM_EXTRA_KEYS = ("note_body", "filed_note", "posted_note")
# Plural only: a chat/destination claim that files were uploaded.
# ``document_name`` is also used by apply_label / move jobs and is not a write claim.
DOCUMENT_CLAIM_EXTRA_KEYS = ("document_names",)
PLAYWRIGHT_WRITE_FLAGS = (
    "playwright_note_or_doc_write",
    "playwright_note_write",
    "playwright_document_write",
)

NOTE_ACTION_TYPES = frozenset(
    {
        "ezlynx.note",
        "ezlynx.discussion_note",
        "ezlynx.add_note",
        "ezlynx.file_note",
        "ezlynx.post_note",
    }
)
DOCUMENT_ACTION_TYPES = frozenset(
    {
        "ezlynx.document_upload",
        "ezlynx.upload_document",
    }
)

# EZLynx-specific note controls. These ids/labels do not appear on Ascend
# or carrier portals. Matching any of them is enough to refuse the write.
EZLYNX_NOTE_SELECTOR_MARKERS = (
    "add-note-btn",
    "add-note-header",
    "btnsavenote",
    "save-note-btn",
    "txtnote",
    "txtdiscussiontitle",
    "txtdiscussionbody",
    "discussion_body",
    "discussion_title",
    "discussionbody",
    "discussiontitle",
    "discussionnotes",
    "btnassociatetoapolicy",
    "data-action='add-note'",
    "data-action='save-note'",
    "data-testid='add-note",
    "data-testid='save-note",
    "#add-note",
    "add_discussion_note",
    "has-text('add note')",
    'has-text("add note")',
    "has-text('save note')",
    'has-text("save note")',
)
EZLYNX_NOTE_LABELS = ("add note", "save note", "note_add")

EZLYNX_HOST_MARKERS = ("ezlynx.com", "uatezlynx.com")
EZLYNX_DOCUMENT_PATH_MARKERS = (
    "/documents",
    "/document",
    "documentlibrary",
    "document-search",
)


class EzlynxPlaywrightNoteDocForbidden(RuntimeError):
    """Playwright/CDP attempted an EZLynx note or document write."""


class EzlynxNoteDocReadbackError(RuntimeError):
    """API write returned an id that a fresh read-back could not confirm."""


def refuse_playwright_note_or_doc(where: str) -> None:
    """Raise the fail-closed error for a Playwright note/doc helper."""

    raise EzlynxPlaywrightNoteDocForbidden(
        f"{PLAYWRIGHT_BLOCKED}: {EZLYNX_NOTE_DOC_API_ONLY}: "
        f"Playwright must never file EZLynx notes or documents ({where}). "
        "Use DiscussionApi add_note_to_discussion / file_note_to_existing_discussion "
        "or DocumentApi upload_applicant_document, then read back the API id. "
        "COMPLETE is refused without those ids."
    )


def is_ezlynx_host(url: str | None) -> bool:
    text = str(url or "").strip().casefold()
    if not text:
        return False
    if any(marker in text for marker in EZLYNX_HOST_MARKERS):
        return True
    try:
        host = (urlparse(text).hostname or "").casefold()
    except ValueError:
        return False
    return any(marker in host for marker in EZLYNX_HOST_MARKERS)


def is_ezlynx_documents_page(url: str | None) -> bool:
    text = str(url or "").strip().casefold()
    if not is_ezlynx_host(text):
        return False
    return any(marker in text for marker in EZLYNX_DOCUMENT_PATH_MARKERS)


def _combined_target_text(
    target: Any,
    *,
    selector: str | None = None,
) -> str:
    parts: list[str] = [str(selector or "")]
    for attr in ("_selector", "selector", "aria_label", "_aria_label", "_text"):
        value = getattr(target, attr, None)
        if value:
            parts.append(str(value))
    get_attribute = getattr(target, "get_attribute", None)
    if callable(get_attribute):
        for name in ("aria-label", "title", "name", "id", "data-action", "data-testid"):
            try:
                value = get_attribute(name)
            except Exception:
                value = None
            if value:
                parts.append(str(value))
    for method_name in ("inner_text", "text_content"):
        fn = getattr(target, method_name, None)
        if callable(fn):
            try:
                value = fn()
            except Exception:
                continue
            if value:
                parts.append(str(value))
    return " ".join(parts).casefold()


def playwright_note_doc_block_reason(
    target: Any,
    *,
    method_name: str,
    selector: str | None = None,
    page_url: str | None = None,
) -> str | None:
    """Hard refusal when Playwright would file an EZLynx note or document.

    Unlike unique-write blocks, this cannot be overridden by Gemini or HITL.
    Ascend / carrier-portal file inputs stay allowed: only EZLynx hosts and
    EZLynx note controls are refused.
    """

    method = str(method_name or "").strip().casefold()
    combined = _combined_target_text(target, selector=selector)
    url = str(page_url or "").strip()
    if not url:
        page = getattr(target, "page", None) or getattr(target, "_page", None)
        value = getattr(page, "url", None)
        if callable(value):
            try:
                value = value()
            except Exception:
                value = ""
        url = str(value or "")

    note_selector = any(marker in combined for marker in EZLYNX_NOTE_SELECTOR_MARKERS)
    note_label = any(label in combined for label in EZLYNX_NOTE_LABELS)
    if note_selector or (note_label and (not url or is_ezlynx_host(url))):
        return (
            f"{PLAYWRIGHT_BLOCKED}: {EZLYNX_NOTE_DOC_API_ONLY}: "
            "Playwright must never file EZLynx notes. "
            "Use DiscussionApi add_note_to_discussion "
            "(file_note_to_existing_discussion) and read back note_id. "
            "This block cannot be overridden by Gemini or HITL."
        )

    if method == "set_input_files" and is_ezlynx_host(url):
        return (
            f"{PLAYWRIGHT_BLOCKED}: {EZLYNX_NOTE_DOC_API_ONLY}: "
            "Playwright must never upload documents to EZLynx. "
            "Use DocumentApi upload_applicant_document and read back document_id. "
            "This block cannot be overridden by Gemini or HITL."
        )

    if (
        method in {"click", "set_input_files"}
        and is_ezlynx_documents_page(url)
        and "upload" in combined
    ):
        return (
            f"{PLAYWRIGHT_BLOCKED}: {EZLYNX_NOTE_DOC_API_ONLY}: "
            "Playwright must never upload documents to EZLynx. "
            "Use DocumentApi upload_applicant_document and read back document_id. "
            "This block cannot be overridden by Gemini or HITL."
        )
    return None


def _as_dict(blob: Any) -> dict[str, Any]:
    return dict(blob) if isinstance(blob, dict) else {}


def _iter_claim_blobs(
    expected: dict[str, Any] | None,
    observed: dict[str, Any] | None,
    action: dict[str, Any] | None,
    payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Evidence and destination claims only.

    Do not scan the raw Job payload: apply_label fixtures and email
    attachment lists carry ``document_name`` / ``document_names`` without
    having uploaded to EZLynx.
    """
    blobs = [
        _as_dict(expected),
        _as_dict(observed),
        _as_dict(action),
        _as_dict((_as_dict(action)).get("destination")),
    ]
    del payload
    return [blob for blob in blobs if blob]


def _action_type_of(*blobs: Any) -> str:
    for blob in blobs:
        mapping = _as_dict(blob)
        for key in ("action_type", "action", "kind"):
            value = str(mapping.get(key) or "").strip().casefold()
            if value:
                return value
    return ""


def _first_api_id(blobs: list[dict[str, Any]], keys: tuple[str, ...]) -> str:
    for blob in blobs:
        for key in keys:
            raw = blob.get(key)
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    text = str(item or "").strip()
                    if text and text.casefold() not in {"none", "null"}:
                        return text
                continue
            text = str(raw or "").strip()
            if text and text.casefold() not in {"none", "null"}:
                return text
    return ""


def _key_present(blobs: list[dict[str, Any]], keys: tuple[str, ...]) -> bool:
    return any(key in blob for blob in blobs for key in keys)


def _nonempty_extra(blobs: list[dict[str, Any]], keys: tuple[str, ...]) -> bool:
    for blob in blobs:
        for key in keys:
            raw = blob.get(key)
            if isinstance(raw, (list, tuple)):
                if any(str(item or "").strip() for item in raw):
                    return True
            elif str(raw or "").strip():
                return True
    return False


def playwright_note_or_doc_path_used(
    expected: dict[str, Any] | None = None,
    observed: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> bool:
    for blob in _iter_claim_blobs(expected, observed, action, payload):
        if any(blob.get(flag) for flag in PLAYWRIGHT_WRITE_FLAGS):
            return True
        method = str(blob.get("method") or blob.get("write_method") or "").casefold()
        if "playwright" in method and (
            "note" in method or "document" in method or "upload" in method
        ):
            return True
    return False


def claimed_ezlynx_note_write(
    expected: dict[str, Any] | None = None,
    observed: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> bool:
    """True when the job claimed it filed an EZLynx note.

    A bare ``discussion_title`` receipt is not a write claim. Chat
    destination verification records titles as receipts only.
    """

    blobs = _iter_claim_blobs(expected, observed, action, payload)
    if _key_present(blobs, NOTE_API_ID_KEYS):
        return True
    if _nonempty_extra(blobs, NOTE_CLAIM_EXTRA_KEYS):
        return True
    action_type = _action_type_of(action, payload)
    if action_type in NOTE_ACTION_TYPES:
        return True
    if action_type.endswith(".note") or "discussion_note" in action_type:
        return True
    return False


def claimed_ezlynx_document_write(
    expected: dict[str, Any] | None = None,
    observed: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> bool:
    blobs = _iter_claim_blobs(expected, observed, action, payload)
    if _key_present(blobs, DOCUMENT_API_ID_KEYS):
        return True
    if _nonempty_extra(blobs, DOCUMENT_CLAIM_EXTRA_KEYS):
        return True
    action_type = _action_type_of(action, payload)
    if action_type in DOCUMENT_ACTION_TYPES:
        return True
    if "document_upload" in action_type:
        return True
    return False


def note_or_document_write_missing_api_id(
    *,
    expected: dict[str, Any] | None = None,
    observed: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> str | None:
    """Refuse COMPLETE when a note/doc write lacks a confirmed API id."""

    if playwright_note_or_doc_path_used(expected, observed, action, payload):
        return (
            "COMPLETE prohibited: Playwright path used for an EZLynx "
            "note/document write. Notes require DiscussionApi note_id; "
            "documents require DocumentApi document_id. Playwright cannot "
            "authorize COMPLETE."
        )
    blobs = _iter_claim_blobs(expected, observed, action, payload)
    if claimed_ezlynx_note_write(expected, observed, action, payload):
        note_id = _first_api_id(blobs, NOTE_API_ID_KEYS)
        if not note_id:
            return (
                "COMPLETE prohibited: EZLynx note write requires a "
                "DiscussionApi note_id (or ezlynx_note_id). Playwright "
                "DOM/screenshots are not evidence."
            )
    if claimed_ezlynx_document_write(expected, observed, action, payload):
        document_id = _first_api_id(blobs, DOCUMENT_API_ID_KEYS)
        if not document_id:
            return (
                "COMPLETE prohibited: EZLynx document write requires a "
                "DocumentApi document_id. Playwright file-chooser uploads "
                "are not evidence."
            )
    return None


def note_id_in_discussion(record: Any, note_id: str) -> bool:
    """True when a discussion payload contains ``note_id``.

    A live discussion read has no note bodies. The latest note id on that
    metadata is enough when it is the id being confirmed.
    """

    want = str(note_id or "").strip()
    if not want or not isinstance(record, dict):
        return False
    for key in ("mostRecentNoteId", "MostRecentNoteId", "noteId", "NoteId", "id", "Id"):
        if str(record.get(key) or "").strip() == want:
            return True
    for key in ("notes", "Notes", "items", "Items", "data", "Data"):
        rows = record.get(key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and note_id_in_discussion(row, want):
                    return True
                if str(row).strip() == want:
                    return True
    return False


def confirm_discussion_note(
    client: Any,
    discussion_id: str,
    note_id: str,
) -> dict[str, Any]:
    """Fresh GET of the discussion. Missing note_id is not success."""

    discussion = str(discussion_id or "").strip()
    nid = str(note_id or "").strip()
    if not discussion:
        raise EzlynxNoteDocReadbackError(
            "DiscussionApi read-back failed: discussion id is missing"
        )
    if not nid:
        raise EzlynxNoteDocReadbackError(
            "DiscussionApi read-back failed: note_id is missing"
        )
    getter = getattr(client, "get_discussion", None)
    if not callable(getter):
        raise EzlynxNoteDocReadbackError(
            "DiscussionApi read-back failed: client cannot GET a discussion"
        )
    record = getter(discussion)
    if not note_id_in_discussion(record, nid):
        raise EzlynxNoteDocReadbackError(
            f"DiscussionApi read-back did not confirm note_id {nid}"
        )
    return {"note_id": nid, "discussion_id": discussion, "read_back": True}


def confirm_uploaded_document_id(
    client: Any,
    applicant_id: str,
    document_id: str,
) -> dict[str, Any]:
    """Fresh DocumentApi search. Missing document_id is not success."""

    applicant = str(applicant_id or "").strip()
    doc_id = str(document_id or "").strip()
    if not applicant:
        raise EzlynxNoteDocReadbackError(
            "DocumentApi read-back failed: applicant id is missing"
        )
    if not doc_id or not doc_id.isdigit():
        raise EzlynxNoteDocReadbackError(
            "DocumentApi read-back failed: document_id is missing or not numeric"
        )
    search = getattr(client, "search_applicant_documents", None)
    if not callable(search):
        raise EzlynxNoteDocReadbackError(
            "DocumentApi read-back failed: client cannot search documents"
        )
    payload = search(applicant)
    from .ezlynx_api import extract_document_api_results, extract_document_records

    ids = {row.get("id") for row in extract_document_api_results(payload)}
    if doc_id not in ids:
        for row in extract_document_records(payload):
            raw = row.get("id") or row.get("Id") or row.get("documentId")
            if str(raw or "").strip() == doc_id:
                ids.add(doc_id)
                break
    if doc_id not in ids:
        raise EzlynxNoteDocReadbackError(
            f"DocumentApi read-back did not confirm document_id {doc_id}"
        )
    return {"document_id": doc_id, "applicant_id": applicant, "read_back": True}


def add_note_to_discussion(
    applicant_id: str,
    note_text: str,
    *,
    discussion_title: str | None = None,
    title_hint: str | None = None,
    discussion_client: Any | None = None,
    note_type: str = "Note",
    dry_run: bool = False,
    document_id: str | None = None,
    ledger_path: Any = None,
    allow_repost: bool = False,
) -> dict[str, Any]:
    """File a note on an existing titled discussion and read it back.

    This is the only success path for EZLynx notes. Playwright helpers must
    raise :class:`EzlynxPlaywrightNoteDocForbidden` instead of calling this
    after a browser click.
    """

    from .ezlynx_discussions import (
        DiscussionApiClient,
        file_note_to_existing_discussion,
    )

    client = discussion_client
    if client is None:
        client = DiscussionApiClient(_discussion_config_from_secret())
    hint = title_hint or discussion_title
    filed = file_note_to_existing_discussion(
        client,
        applicant_id,
        note_text,
        title_hint=hint,
        note_type=note_type,
        dry_run=dry_run,
        document_id=document_id,
        ledger_path=ledger_path,
        allow_repost=allow_repost,
    )
    if filed.get("status") != "filed":
        return filed
    if filed.get("read_back") and str(filed.get("verified_by") or "") in {
        "text",
        "discussion",
        "ledger",
    }:
        return filed
    note_id = str(filed.get("note_id") or "").strip()
    discussion_id = str(filed.get("discussion_id") or "").strip()
    if not note_id:
        return filed
    confirm_discussion_note(client, discussion_id, note_id)
    filed["read_back"] = True
    return filed


def upload_document_via_api(
    applicant_id: str,
    document_name: str,
    file_bytes: bytes,
    *,
    client: Any | None = None,
    filename: str | None = None,
    policy_master_id: str | None = None,
    file_content_type: str = "application/octet-stream",
) -> dict[str, Any]:
    """Upload via DocumentApi and confirm the numeric id on a fresh search."""

    from .ezlynx_api import EzlynxApiClient, load_ezlynx_api_config

    live = client if client is not None else EzlynxApiClient(load_ezlynx_api_config())
    document_id = live.upload_applicant_document(
        applicant_id,
        document_name,
        file_bytes,
        filename=filename,
        policy_master_id=policy_master_id,
        file_content_type=file_content_type,
    )
    confirm_uploaded_document_id(live, applicant_id, document_id)
    return {
        "document_id": document_id,
        "applicant_id": str(applicant_id).strip(),
        "document_name": str(document_name).strip(),
        "read_back": True,
    }


DISCUSSION_API_ENV = "ROBIE_EZLYNX_DISCUSSION_API"
LIVE_DISCUSSION_API = "live"


def discussion_api_target(environ: dict[str, str] | None = None) -> str:
    """``live`` uses live EZLynx. Anything else follows ROBIE_ENV (Test stays UAT)."""
    env = os.environ if environ is None else environ
    raw = str(env.get(DISCUSSION_API_ENV) or "").strip().lower()
    if raw in {"", "env", "uat", "default"}:
        return "env"
    if raw == LIVE_DISCUSSION_API:
        return LIVE_DISCUSSION_API
    raise RuntimeError(
        f"{DISCUSSION_API_ENV} must be 'live' or unset. Refusing '{raw}'."
    )


def _login_secret_resource(ref: str, accessor: Any) -> str:
    """Newest ENABLED version. A numeric env pin is not the password to use.

    The browser login lists ENABLED versions and ignores a stale pin such as
    ``versions/7``. This does the same. If the list call is unavailable, the
    ``versions/latest`` alias is used instead of the pin. The value is never
    logged.
    """
    from .secret_manager import _newest_enabled_resource

    resource = str(ref or "").strip()
    if "/versions/" not in resource:
        return resource
    newest = _newest_enabled_resource(resource, accessor)
    pinned = resource.rsplit("/", 1)[-1]
    chosen = str(newest or resource).rsplit("/", 1)[-1]
    if newest and newest != resource:
        logger.info(
            "note tool ignoring stale login secret pin env versions/%s; using versions/%s",
            pinned,
            chosen,
        )
        return str(newest)
    if pinned == "latest":
        return resource
    parent = resource.rsplit("/versions/", 1)[0]
    logger.info(
        "note tool ignoring stale login secret pin env versions/%s; using versions/latest",
        pinned,
    )
    return parent + "/versions/latest"


def _required_secret_value(
    env_name: str,
    *,
    accessor: Any,
    environ: dict[str, str],
) -> str:
    ref = str(environ.get(env_name) or "").strip()
    if not ref:
        raise RuntimeError(
            f"{env_name} must be set when {DISCUSSION_API_ENV}=live. "
            "Refusing to fall back to UAT."
        )
    ref = _login_secret_resource(ref, accessor)
    try:
        value = str(accessor.access(ref) or "").strip()
    except Exception as exc:
        raise RuntimeError(
            f"{env_name} could not be read for {DISCUSSION_API_ENV}=live. "
            "Refusing to fall back to UAT."
        ) from exc
    if not value:
        raise RuntimeError(
            f"{env_name} is empty for {DISCUSSION_API_ENV}=live. "
            "Refusing to fall back to UAT."
        )
    return value


@contextmanager
def _secret_env(environ: dict[str, str] | None) -> Iterator[None]:
    """Apply a caller-supplied env for secret lookups, then restore it."""
    if environ is None:
        yield
        return
    keys = (
        "ROBIE_ENV",
        "ROBIE_EZLYNX_API_UAT_SECRET",
        "ROBIE_EZLYNX_API_PROD_SECRET",
        "ROBIE_EZLYNX_USERNAME_SECRET",
        "ROBIE_EZLYNX_PASSWORD_SECRET",
        DISCUSSION_API_ENV,
    )
    previous: dict[str, str | None] = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            if key in environ:
                os.environ[key] = str(environ[key])
            elif key in os.environ and environ is not os.environ:
                os.environ.pop(key, None)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load_discussion_api_config(
    *,
    accessor: Any | None = None,
    environ: dict[str, str] | None = None,
) -> Any:
    """Discussion API settings for the note tool.

    ``ROBIE_EZLYNX_DISCUSSION_API=live`` uses the live EZLynx host from
    ``ROBIE_EZLYNX_API_PROD_SECRET`` and the same SSRobie login the Test
    browser uses (``ezlynx-username`` / ``ezlynx-password``). A missing
    secret raises. It does not silently use UAT.
    """
    from .ezlynx_api import EzlynxApiConfigurationError, load_ezlynx_api_config
    from .ezlynx_discussions import DiscussionApiConfig

    env = dict(os.environ if environ is None else environ)
    target = discussion_api_target(env)

    def _from_api(api_config: Any, *, username: str, password: str) -> Any:
        parsed = urlparse(str(api_config.document_base_url or api_config.token_endpoint))
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return DiscussionApiConfig(
            discussion_base_url=origin + "/DiscussionApi/",
            token_endpoint=str(api_config.token_endpoint),
            client_id=str(api_config.client_id),
            client_secret=str(api_config.client_secret),
            username=username,
            integration_group_id=str(api_config.integration_group_id),
            scope="DiscussionApi openid",
            password=password,
        )

    if target != LIVE_DISCUSSION_API:
        with _secret_env(environ):
            api_config = load_ezlynx_api_config(accessor=accessor)
        return _from_api(api_config, username=str(api_config.username), password="")

    secret_reader = accessor
    if secret_reader is None:
        from .secret_manager import GoogleSecretManagerAccessor

        secret_reader = GoogleSecretManagerAccessor()
    try:
        with _secret_env(environ):
            api_config = load_ezlynx_api_config(
                environment="PRODUCTION",
                accessor=secret_reader,
            )
    except EzlynxApiConfigurationError as exc:
        raise RuntimeError(
            f"{DISCUSSION_API_ENV}=live could not load the live EZLynx API secret. "
            "Refusing to fall back to UAT."
        ) from exc
    host = urlparse(str(api_config.token_endpoint or "")).hostname or ""
    base = str(api_config.document_base_url or "")
    if "uatezlynx" in host.casefold() or "uatezlynx" in base.casefold():
        raise RuntimeError(
            f"{DISCUSSION_API_ENV}=live but the production API secret points at UAT. "
            "Refusing."
        )
    username = _required_secret_value(
        "ROBIE_EZLYNX_USERNAME_SECRET",
        accessor=secret_reader,
        environ=env,
    )
    password = _required_secret_value(
        "ROBIE_EZLYNX_PASSWORD_SECRET",
        accessor=secret_reader,
        environ=env,
    )
    return _from_api(api_config, username=username, password=password)


def _discussion_config_from_secret() -> Any:
    return load_discussion_api_config()


__all__ = [
    "DOCUMENT_ACTION_TYPES",
    "EZLYNX_NOTE_DOC_API_ONLY",
    "EzlynxNoteDocReadbackError",
    "EzlynxPlaywrightNoteDocForbidden",
    "NOTE_ACTION_TYPES",
    "PLAYWRIGHT_BLOCKED",
    "add_note_to_discussion",
    "claimed_ezlynx_document_write",
    "claimed_ezlynx_note_write",
    "confirm_discussion_note",
    "confirm_uploaded_document_id",
    "is_ezlynx_documents_page",
    "is_ezlynx_host",
    "note_id_in_discussion",
    "note_or_document_write_missing_api_id",
    "playwright_note_doc_block_reason",
    "playwright_note_or_doc_path_used",
    "refuse_playwright_note_or_doc",
    "upload_document_via_api",
]
