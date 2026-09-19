#!/usr/bin/env python3
"""Hermes tool: ezlynx_document_upload.

Email and Chat jobs that need to push a document (dec page, renewal quote,
carrier letter, ...) into an EZLynx applicant file must call this tool — not
hand-rolled API calls and not playwright_exec. The handler invokes the Job
Engine's EzlynxApiClient.upload_applicant_document, which enforces the
engine's write-allowlist (require_allowed_ezlynx_write_applicant) and posts
through the DocumentApi OAuth path. No secrets in code: credentials load from
Secret Manager via load_ezlynx_api_config(). No bind, no money, no policy
deletes — this tool only uploads one document file.
"""
from __future__ import annotations

import os

from tools.registry import registry, tool_error, tool_result

DOCUMENT_UPLOAD_SCHEMA = {
    "name": "ezlynx_document_upload",
    "description": (
        "Upload a document file into an EZLynx applicant's document library "
        "via the EZLynx DocumentApi. USE THIS TOOL — not playwright_exec and "
        "not a hand-rolled API call — whenever the job needs to push a file "
        "(dec page, renewal packet, carrier correspondence, application PDF) "
        "into EZLynx. The engine enforces the write-allowlist: uploads are "
        "refused for any applicant not authorized by ROBIE_EZLYNX_WRITE_APPLICANT_IDS "
        "(unset/empty = agency-wide; comma list = restricted) or by a bound "
        "Production Chat job. Pass file_path as a local "
        "filesystem path to an already-downloaded file (e.g. an email "
        "attachment saved to the job evidence directory). Returns the new "
        "EZLynx document id on success."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "applicant_id": {
                "type": "string",
                "description": "EZLynx applicant/account id (e.g. 220250093).",
            },
            "file_path": {
                "type": "string",
                "description": (
                    "Local filesystem path to the file to upload. Must be an "
                    "existing readable file."
                ),
            },
            "document_name": {
                "type": "string",
                "description": "Display name for the document in EZLynx.",
            },
            "policy_master_id": {
                "type": "string",
                "description": (
                    "Optional policy master id to associate the document "
                    "with. Defaults to 0 (applicant-level document)."
                ),
            },
            "file_content_type": {
                "type": "string",
                "description": (
                    "Optional MIME type of the file (e.g. application/pdf). "
                    "Defaults to application/octet-stream."
                ),
            },
        },
        "required": ["applicant_id", "file_path", "document_name"],
    },
}


def _read_upload_file(file_path: str) -> bytes:
    """Read the upload file. Fail closed on anything unexpected."""
    path = os.path.expanduser(str(file_path or "").strip())
    if not path:
        raise ValueError("file_path is required")
    if not os.path.isfile(path):
        raise ValueError(f"file_path is not a readable file: {path}")
    with open(path, "rb") as handle:
        data = handle.read()
    if not data:
        raise ValueError(f"file_path is empty: {path}")
    return data


def _upload_document(args: dict) -> dict:
    from robie_job_engine.ezlynx_api import (
        EzlynxApiClient,
        load_ezlynx_api_config,
    )

    applicant_id = str(args.get("applicant_id") or "").strip()
    document_name = str(args.get("document_name") or "").strip()
    file_path = str(args.get("file_path") or "").strip()
    if not applicant_id:
        raise ValueError("applicant_id is required")
    if not document_name:
        raise ValueError("document_name is required")
    file_bytes = _read_upload_file(file_path)
    policy_master_id = args.get("policy_master_id")
    file_content_type = str(args.get("file_content_type") or "").strip() or None

    # Write-allowlist is enforced inside upload_applicant_document via
    # require_allowed_ezlynx_write_applicant: any applicant outside the
    # env allowlist / bound Production job raises EzlynxWriteScopeError.
    config = load_ezlynx_api_config()
    client = EzlynxApiClient(config)
    kwargs: dict = {}
    if policy_master_id not in (None, ""):
        kwargs["policy_master_id"] = policy_master_id
    if file_content_type:
        kwargs["file_content_type"] = file_content_type
    document_id = client.upload_applicant_document(
        applicant_id,
        document_name,
        file_bytes,
        filename=os.path.basename(os.path.expanduser(file_path)),
        **kwargs,
    )
    from robie_job_engine.ezlynx_api_only_writes import confirm_uploaded_document_id

    confirm_uploaded_document_id(client, applicant_id, document_id)
    return {
        "ok": True,
        "document_id": document_id,
        "applicant_id": applicant_id,
        "document_name": document_name,
        "read_back": True,
    }


def ezlynx_document_upload_handler(args: dict, **kwargs):
    try:
        report = _upload_document(args or {})
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return tool_error(f"{type(exc).__name__}: {exc}")
    return tool_result(report)


def _available() -> bool:
    try:
        from robie_job_engine import ezlynx_api  # noqa: F401
        return True
    except ImportError:
        return False


registry.register(
    name="ezlynx_document_upload",
    toolset="ezlynx",
    schema=DOCUMENT_UPLOAD_SCHEMA,
    handler=ezlynx_document_upload_handler,
    check_fn=_available,
    emoji="📄",
)
