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

    def __init__(self, client: Any = None, *, discussion_client: Any = None):
        self._client = client
        if discussion_client is not None:
            self._discussion_client = discussion_client

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

    def document_listing(self, applicant_id: str) -> dict[str, Any]:
        """Every document row, and whether the list is known to be complete."""
        from .ezlynx_api import document_search_is_complete, extract_document_api_results

        payload = self._require_client().search_applicant_documents(applicant_id)
        rows = [{"id": row["id"], "name": row.get("name") or ""} for row in extract_document_api_results(payload)]
        total = payload.get("totalSize") if isinstance(payload, dict) else None
        return {"rows": rows, "complete": document_search_is_complete(payload), "total": total}

    def document_search(self, applicant_id: str) -> dict[str, Any]:
        """The raw merged DocumentApi search: ``results``, ``complete``, ``pages_read``."""
        payload = self._require_client().search_applicant_documents(applicant_id)
        return payload if isinstance(payload, dict) else {"results": payload}

    def download_document(self, document_id: str) -> bytes:
        downloaded = self._require_client().download_document(document_id)
        return downloaded.body

    def discussions_for_applicant(self, applicant_id: str) -> list[dict[str, Any]]:
        rows = self._require_client().get_applicant_discussions(applicant_id, page_size=50) or []
        return [
            {"title": str(r.get("Title") or r.get("title") or r.get("Subject") or "")}
            for r in rows
            if isinstance(r, dict)
        ]

    def get_discussion(self, discussion_id: str) -> dict[str, Any]:
        """One discussion, notes included. Used to re-read a note write.

        A policy search is the wrong key for a discussion note. This GET is
        the readback for that write.
        """
        from .ezlynx_api_only_writes import load_discussion_api_config
        from .ezlynx_discussions import DiscussionApiClient

        client = getattr(self, "_discussion_client", None)
        if client is None:
            client = DiscussionApiClient(load_discussion_api_config())
            self._discussion_client = client
        record = client.get_discussion(discussion_id)
        return record if isinstance(record, dict) else {}

    def _discussions(self) -> Any:
        if getattr(self, "_discussion_client", None) is None:
            from .ezlynx_api_only_writes import load_discussion_api_config
            from .ezlynx_discussions import DiscussionApiClient

            self._discussion_client = DiscussionApiClient(load_discussion_api_config())
        return self._discussion_client

    def get_discussion_with_notes(self, discussion_id: str) -> dict[str, Any]:
        """One discussion with every note body (read-only)."""
        record = self._discussions().get_discussion_with_notes(discussion_id)
        return record if isinstance(record, dict) else {}

    def discussion_ids_for_applicant(self, applicant_id: str) -> list[str]:
        """Discussion ids on the applicant (read-only ownership check)."""
        return [str(item) for item in self._discussions().get_discussion_ids(applicant_id) or []]
