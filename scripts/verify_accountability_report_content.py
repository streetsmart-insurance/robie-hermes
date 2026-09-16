#!/usr/bin/env python3
"""Verify the delivered accountability Google Doc contains usable source evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from typing import Any, Iterable, Mapping


DOCS_READONLY_SCOPE = "https://www.googleapis.com/auth/documents.readonly"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
SUBMISSION_UNVERIFIED_MARKERS = (
    "submission center was not verified",
    "server submission center audit unavailable",
    "submission center audit unavailable",
)
MAGELLAN_UNVERIFIED_MARKERS = (
    "no verified records for this report date",
    "magellan was not verified",
    "magellan unavailable",
)


def _structural_text(elements: Iterable[Mapping[str, Any]]) -> str:
    chunks: list[str] = []
    for element in elements:
        paragraph = element.get("paragraph", {})
        for item in paragraph.get("elements", []):
            text = item.get("textRun", {}).get("content")
            if text:
                chunks.append(str(text))
        table = element.get("table", {})
        for row in table.get("tableRows", []):
            for cell in row.get("tableCells", []):
                chunks.append(_structural_text(cell.get("content", [])))
        toc = element.get("tableOfContents", {})
        chunks.append(_structural_text(toc.get("content", [])))
    return "".join(chunks)


def _tab_text(tab: Mapping[str, Any]) -> str:
    chunks: list[str] = []
    document_tab = tab.get("documentTab", {})
    chunks.append(_structural_text(document_tab.get("body", {}).get("content", [])))
    for child in tab.get("childTabs", []):
        chunks.append(_tab_text(child))
    return "".join(chunks)


def document_text(document: Mapping[str, Any]) -> str:
    tabs = document.get("tabs", [])
    if tabs:
        return "".join(_tab_text(tab) for tab in tabs)
    return _structural_text(document.get("body", {}).get("content", []))


def validate_submission_content(text: str) -> dict[str, Any]:
    normalized = " ".join(text.split()).casefold()
    if "submission center" not in normalized:
        raise RuntimeError("Submission Center section is missing")
    submission_markers = [
        marker for marker in SUBMISSION_UNVERIFIED_MARKERS if marker in normalized
    ]
    if submission_markers:
        raise RuntimeError("Submission Center section contains an unverified-source marker")
    if "magellan" not in normalized:
        raise RuntimeError("Magellan section is missing")
    magellan_markers = [
        marker for marker in MAGELLAN_UNVERIFIED_MARKERS if marker in normalized
    ]
    if magellan_markers:
        raise RuntimeError("Magellan section contains an unverified-source marker")
    return {
        "verified": True,
        "submission_center_section_present": True,
        "submission_center_unverified_marker_present": False,
        "magellan_section_present": True,
        "magellan_unverified_marker_present": False,
        "document_content_sha256": hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest(),
    }


def delegated_credentials(*, mailbox: str, service_account_email: str):
    import google.auth
    from google.auth import iam
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    source, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
    return service_account.Credentials(
        signer=iam.Signer(Request(), source, service_account_email),
        service_account_email=service_account_email,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[DOCS_READONLY_SCOPE],
        subject=mailbox,
    )


def fetch_document(*, document_id: str, mailbox: str, service_account_email: str):
    from googleapiclient.discovery import build

    credentials = delegated_credentials(
        mailbox=mailbox,
        service_account_email=service_account_email,
    )
    docs = build("docs", "v1", credentials=credentials, cache_discovery=False)
    return (
        docs.documents()
        .get(documentId=document_id, includeTabsContent=True)
        .execute()
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--mailbox", required=True)
    parser.add_argument("--service-account-email", required=True)
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", args.document_id):
        print("report content verification failed: invalid document id", file=sys.stderr)
        return 1
    try:
        document = fetch_document(
            document_id=args.document_id,
            mailbox=args.mailbox,
            service_account_email=args.service_account_email,
        )
        evidence = validate_submission_content(document_text(document))
    except Exception as exc:
        print(
            f"report content verification failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
