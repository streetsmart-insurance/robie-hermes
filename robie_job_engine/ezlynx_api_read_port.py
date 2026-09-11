"""Adapter binding the real EZLynxApiClient to the Chat destination verifier.

Split out of chat_ezlynx_destination_verifier.py so the verifier stays pure
logic and the IO lives on its own. Read-only: search, list, read. It never
posts and never mutates.

Normalization happens HERE, not in the verifier. api_client's own docstring
warns the document envelope varies by tenant ("do not assume
Records/DocumentList or DocumentName/PolicyNumber"), which is why
extract_document_records / document_display_fields exist.

Documents go through the CLASSIC endpoint
    GET /ezlynxapi/api/documentlibrary/list/{applicant}/{page}/{size}/{policyId}
because the OAuth DocumentApi scope returns 403 for this integration group.
"""

from __future__ import annotations

from typing import Any


class EzlynxApiClientReadPort:
    """Bind the real ``EzlynxApiClient`` to the verifier's read port.

    Read-only: search, list, read. Never posts, never mutates. Normalizes
    each shape here so the verifier stays free of tenant-specific field
    names. Constructible without renewal-automation; ``client`` may be
    omitted and is loaded from Secret Manager on first use.
    """

    def __init__(self, client: Any = None):
        self._client = client

    def _require_client(self) -> Any:
        if self._client is None:
            from .ezlynx_api import EzlynxApiClient, load_ezlynx_api_config

            self._client = EzlynxApiClient(load_ezlynx_api_config())
        return self._client

    def policy_by_number(self, policy_number: str) -> dict[str, Any]:
        return self._require_client().search_policy_by_number(policy_number)

    def documents_for_applicant(
        self, applicant_id: str, policy_id: int = 0
    ) -> list[dict[str, Any]]:
        from .ezlynx_api import document_display_fields, extract_document_records

        payload = self._require_client().list_applicant_documents(
            applicant_id, page_index=1, page_size=200, policy_id=policy_id
        )
        rows = extract_document_records(payload)
        out: list[dict[str, Any]] = []
        for row in rows:
            fields = document_display_fields(row) or {}
            out.append(
                {
                    "name": fields.get("name")
                    or fields.get("description")
                    or row.get("Description")
                    or "",
                    "policy_id": row.get("PolicyId"),
                }
            )
        return out

    def discussions_for_applicant(self, applicant_id: str) -> list[dict[str, Any]]:
        rows = self._require_client().get_applicant_discussions(applicant_id, page_size=50) or []
        return [
            {"title": str(r.get("Title") or r.get("title") or r.get("Subject") or "")}
            for r in rows
            if isinstance(r, dict)
        ]
