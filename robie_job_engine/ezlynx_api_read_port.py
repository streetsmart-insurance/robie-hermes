"""Adapter binding the real EZLynxApiClient to the Chat destination verifier.

Split out of chat_ezlynx_destination_verifier.py so the verifier stays pure
logic and the IO lives on its own. Read-only: search, list, read. It never
posts and never mutates.

Documents go through DocumentApi (proven 2026-09-11 as SSRobie):

    GET {host}/documentapi/documents/v1/account/{ApplicantID}/document-search
    GET {host}/documentapi/documents/v1/{DocumentID}/download

Use ``results[].id``. Never ``documentUrl``. Classic
``documentlibrary/list`` and classic download are not the dest-evidence path.
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
        from .ezlynx_api import extract_document_api_results

        del policy_id
        payload = self._require_client().search_applicant_documents(applicant_id)
        out: list[dict[str, Any]] = []
        for row in extract_document_api_results(payload):
            out.append(
                {
                    "id": row["id"],
                    "name": row.get("name") or "",
                }
            )
        return out

    def download_document(self, document_id: str) -> bytes:
        downloaded = self._require_client().download_document(document_id)
        return downloaded.body

    def discussions_for_applicant(self, applicant_id: str) -> list[dict[str, Any]]:
        # v8 by-applicant endpoint (DiscussionApiClient). The OAuth
        # DiscussionApi/discussion/v1/applicant/{id} endpoint 404s and the
        # old path silently returned [], which made every discussion read
        # look empty. Use the working v8 client instead.
        rows = self._discussion_client().get_discussions(applicant_id) or []
        return [
            {"title": str(r.get("title") or r.get("Title") or r.get("subject") or r.get("Subject") or "")}
            for r in rows
            if isinstance(r, dict)
        ]

    def note_in_discussion(self, discussion_id: str, note_id: str) -> bool:
        """Fresh GET of one discussion; True iff ``note_id`` is present."""
        from .ezlynx_api_only_writes import note_id_in_discussion

        record = self._discussion_client().get_discussion(discussion_id)
        return note_id_in_discussion(record, note_id)

    def _discussion_client(self) -> Any:
        if getattr(self, "_discussion_client_cached", None) is None:
            from .ezlynx_api_only_writes import _discussion_config_from_secret
            from .ezlynx_discussions import DiscussionApiClient

            self._discussion_client_cached = DiscussionApiClient(
                _discussion_config_from_secret()
            )
        return self._discussion_client_cached
