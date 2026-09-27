"""Verified EZLynx API writers: discussion notes and document uploads.

Every writer runs :func:`ezlynx_write_verify.verify_write_target` BEFORE any
HTTP write. A refused write raises ``EzlynxWriteVerifyError`` and the
network is never touched.

Reads reuse the merged #295 OAuth client (:mod:`ezlynx_api`)::

    policy search    GET  /PolicyApi/policy/v1/search?PolicyNumber=        (proven 200)
    document search  GET  /documentapi/documents/v1/account/{id}/document-search (proven)
    document upload  POST /DocumentApi/documents/v1/account/{id}/document  (proven)

The note-post path below is UNVERIFIED until the first live 200 pins it::

    note post        POST /DiscussionApi/discussions/v1/notes

The 2026-09-12 work order also cited ``POST /api/note/v1`` as the
classic-REST alternative. If the live call 404s, the constant -- not the
call sites -- gets corrected. No fallback guessing: a second guessed
endpoint risks double-posting the note.

CREDENTIALS: writers take an ``EzlynxApiClient``. When none is passed they
build one via ``load_ezlynx_api_config()``, which reads Secret Manager and
raises a clear configuration error when it is not wired. There is no
silent fallback and no simulated success.

FIELD SHAPES: the policy-search record's applicant-id and insured-name
keys are extracted by candidate-key lists below. Those keys are
UNVERIFIED until the first live response pins them; when none match, the
write is refused (fail closed) instead of guessed.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .ezlynx_api import (
    EzlynxApiClient,
    EzlynxApiConfigurationError,
    document_display_fields,
    extract_document_records,
    load_ezlynx_api_config,
)
from .ezlynx_write_scope import normalize_applicant_id
from .ezlynx_write_verify import (
    EzlynxWriteVerifyError,
    _document_corroborates,
    normalize_name,
    verify_write_target,
)

# UNVERIFIED until the first live 200. See module docstring.
DISCUSSION_NOTE_POST_PATH = "/DiscussionApi/discussions/v1/notes"

# Candidate keys in a PolicyApi search record. UNVERIFIED until live.
_POLICY_LIST_KEYS = ("Policies", "policies", "Data", "data", "Items", "items", "results", "Results")
_POLICY_NUMBER_KEYS = ("PolicyNumber", "policyNumber", "policy_number", "PolicyNo")
_POLICY_APPLICANT_KEYS = (
    "ApplicantId",
    "ApplicantID",
    "applicantId",
    "applicant_id",
    "Applicant_Id",
)
_POLICY_NAME_KEYS = (
    "InsuredName",
    "insuredName",
    "NamedInsured",
    "namedInsured",
    "Insured",
    "insured",
    "ApplicantName",
    "applicantName",
    "Name",
    "name",
)


def _first_present(mapping: Any, keys: tuple[str, ...]) -> Optional[str]:
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            text = str(value).strip()
            if text:
                return text
    return None


def _policy_records(search_result: Any) -> list[dict]:
    data = search_result.get("data", search_result) if isinstance(search_result, dict) else search_result
    if isinstance(data, dict):
        for key in _POLICY_LIST_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return [data] if data else []
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def find_policy_record(search_result: Any, policy_number: str) -> Optional[dict]:
    """Return the policy record matching ``policy_number``, or None."""

    want = str(policy_number or "").strip().lower()
    if not want:
        return None
    for record in _policy_records(search_result):
        for key in _POLICY_NUMBER_KEYS:
            if str(record.get(key) or "").strip().lower() == want:
                return record
    return None


def make_live_verifier_fns(
    client: EzlynxApiClient,
    *,
    expected_policy_number: object = None,
    expected_name: object = None,
) -> tuple[Callable, Callable, Callable]:
    """Build the (policy_search, applicant_fetch, documents) callables for the verifier."""

    policy_number = str(expected_policy_number or "").strip() or None

    def policy_search_fn(pn: str) -> Optional[dict]:
        result = client.search_policy_by_number(pn)
        record = find_policy_record(result, pn)
        if record is None:
            return None
        return {
            "applicant_id": _first_present(record, _POLICY_APPLICANT_KEYS),
            "policy_number": str(pn).strip(),
        }

    def documents_fn(applicant_id: str) -> list[dict]:
        payload = client.search_applicant_documents(applicant_id)
        return [document_display_fields(row) for row in extract_document_records(payload)]

    def applicant_fetch_fn(applicant_id: str) -> dict:
        if policy_number:
            record = find_policy_record(client.search_policy_by_number(policy_number), policy_number)
            name = _first_present(record or {}, _POLICY_NAME_KEYS)
            return {
                "applicant_id": applicant_id,
                "name": name,
                "name_source": "policy_record" if name else "policy_record_name_not_found",
            }
        # No policy number (COI request, hello-inbox item): corroborate the
        # expected name against the applicant's own documents tab. v1
        # limitation: checks 3 and 4 then share this one signal; an applicant
        # read endpoint would make them independent.
        docs = documents_fn(applicant_id)
        if any(_document_corroborates(doc, "", expected_name) for doc in docs):
            return {
                "applicant_id": applicant_id,
                "name": str(expected_name or ""),
                "name_source": "document_corroboration",
            }
        return {
            "applicant_id": applicant_id,
            "name": None,
            "name_source": "no_document_corroboration",
        }

    return policy_search_fn, applicant_fetch_fn, documents_fn


def _require_client(client: Optional[EzlynxApiClient]) -> EzlynxApiClient:
    if client is not None:
        return client
    try:
        return EzlynxApiClient(load_ezlynx_api_config())
    except EzlynxApiConfigurationError as exc:
        raise EzlynxApiConfigurationError(
            "EZLynx API is not configured (Secret Manager not wired): "
            f"{exc}. Refusing to write instead of guessing."
        ) from exc


def _verified(
    applicant_id: object,
    client: EzlynxApiClient,
    *,
    expected_policy_number: object = None,
    expected_name: object = None,
) -> tuple[str, dict]:
    """Run the verifier with live fns. Returns (applicant_id, evidence) or raises."""

    target = normalize_applicant_id(applicant_id)
    policy_search_fn, applicant_fetch_fn, documents_fn = make_live_verifier_fns(
        client,
        expected_policy_number=expected_policy_number,
        expected_name=expected_name,
    )
    evidence = verify_write_target(
        target,
        expected_policy_number=expected_policy_number,
        expected_name=expected_name,
        policy_search_fn=policy_search_fn,
        applicant_fetch_fn=applicant_fetch_fn,
        documents_fetch_fn=documents_fn,
    )
    return target, evidence


def post_note(
    applicant_id: object,
    title: str,
    body: str,
    *,
    expected_policy_number: object = None,
    expected_name: object = None,
    client: Optional[EzlynxApiClient] = None,
) -> dict:
    """Verify the write target, then post a discussion note via the API.

    ``title`` is the Discussion title (e.g. the renewal SOP's
    ``"Renewal Manual / Submission Center"``). Raises
    ``EzlynxWriteVerifyError`` before any HTTP call when verification
    fails; raises ``EzlynxApiError`` when the live POST fails.
    """

    if not str(title or "").strip():
        raise ValueError("note title is required")
    if not str(body or "").strip():
        raise ValueError("note body is required")
    live = _require_client(client)
    target, evidence = _verified(
        applicant_id,
        live,
        expected_policy_number=expected_policy_number,
        expected_name=expected_name,
    )
    payload: dict[str, Any] = {
        "applicantId": target,
        "title": str(title).strip(),
        "text": str(body),
    }
    if str(expected_policy_number or "").strip():
        payload["policyNumber"] = str(expected_policy_number).strip()
    # NOTE POST PATH UNVERIFIED -- see DISCUSSION_NOTE_POST_PATH.
    response = live.post_json(DISCUSSION_NOTE_POST_PATH, payload)
    return {"verification": evidence, "note_response": response}


def upload_document(
    applicant_id: object,
    document_name: str,
    file_bytes: bytes,
    *,
    expected_policy_number: object = None,
    expected_name: object = None,
    filename: Optional[str] = None,
    policy_master_id: Optional[str] = None,
    file_content_type: str = "application/pdf",
    client: Optional[EzlynxApiClient] = None,
) -> dict:
    """Verify the write target, then upload a document via the proven DocumentApi path.

    Delegates the HTTP upload to the merged #295 client
    (``POST /DocumentApi/documents/v1/account/{id}/document``), which
    additionally enforces the write allowlist itself. Returns
    ``{"verification": evidence, "document_id": ...}``.
    """

    if not str(document_name or "").strip():
        raise ValueError("document name is required")
    if not file_bytes:
        raise ValueError("document bytes are required")
    live = _require_client(client)
    target, evidence = _verified(
        applicant_id,
        live,
        expected_policy_number=expected_policy_number,
        expected_name=expected_name,
    )
    document_id = live.upload_applicant_document(
        target,
        str(document_name).strip(),
        file_bytes,
        filename=filename,
        policy_master_id=policy_master_id,
        file_content_type=file_content_type,
    )
    return {"verification": evidence, "document_id": document_id}


__all__ = [
    "DISCUSSION_NOTE_POST_PATH",
    "EzlynxWriteVerifyError",
    "find_policy_record",
    "make_live_verifier_fns",
    "post_note",
    "upload_document",
]
