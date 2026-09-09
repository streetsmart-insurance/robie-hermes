"""Separate Progressive retrieval build, with bounded dates and read-only port.

Portal navigation must be implemented against a verified Test session; these
scope names come from the Hello/Mail Sorting SOP, not guessed selectors.
"""
from __future__ import annotations

from datetime import date
from typing import Protocol

from .intake_core import IntakeHold, IntakeWorker, ReadResult, SourceItem, require_test


SCOPES = frozenset({"fao_communications", "policies_need_service", "bop_pending_cancel_nonpayment"})


class ProgressivePort(Protocol):
    def list_documents(self, *, scope: str, start: date, end: date) -> ReadResult: ...
    def download_document(self, document_id: str) -> SourceItem: ...


class ProgressiveRetrieval(IntakeWorker):
    process = "progressive"
    source_system = "progressive"
    title = "Progressive document intake: review carrier item and upload source"

    def retrieve_selected(self, portal: ProgressivePort, *, scope: str, start: date, end: date, document_id: str) -> SourceItem:
        require_test()
        if scope not in SCOPES or not (0 <= (end - start).days <= 31):
            raise IntakeHold("An approved Progressive scope and at most 32 inclusive days are required")
        # Caller chooses explicit dates covering leave, weekends and holidays.
        # No checkpoint advances here and no portal item is marked processed.
        rows = portal.list_documents(scope=scope, start=start, end=end).checked()
        selected = [r for r in rows if r.get("document_id") == document_id]
        if len(selected) != 1:
            raise IntakeHold("Selected carrier document is missing or ambiguous")
        row = selected[0]
        if row.get("requires_action") is not True or row.get("already_delivered") is not False:
            raise IntakeHold("Actionability and prior delivery must be explicitly checked")
        if scope == "policies_need_service" and row.get("processed") is not False:
            raise IntakeHold("Policies Need Service selection must be an unticked item")
        if not start <= date.fromisoformat(str(row.get("processed_or_effective_date") or "")) <= end:
            raise IntakeHold("Carrier date is outside the requested retrieval window")
        source = portal.download_document(document_id)
        source.validate()
        if source.system != "progressive" or source.source_id != document_id or source.source_account != row.get("source_account"):
            raise IntakeHold("Downloaded source does not match the selected carrier document")
        return source
