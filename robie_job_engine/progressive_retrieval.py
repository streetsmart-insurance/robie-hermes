"""Separate Progressive retrieval build, with bounded dates and read-only port.

Portal navigation must be implemented against a verified Test session; these
scope names come from the Hello/Mail Sorting SOP, not guessed selectors.

``fao_communications`` list and download for an explicit processed-date window
is ``progressive_fao_memo``. Policies Need Service and BOP pending-cancel still
have no portal adapter. This module does not upload, note, task, or label.
"""
from __future__ import annotations

from datetime import date
from typing import Mapping, Protocol

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
        require_bounded_scope(scope, start, end)
        # Caller chooses explicit dates covering leave, weekends and holidays.
        # No checkpoint advances here and no portal item is marked processed.
        rows = portal.list_documents(scope=scope, start=start, end=end).checked()
        selected = [r for r in rows if r.get("document_id") == document_id]
        if len(selected) != 1:
            raise IntakeHold("Selected carrier document is missing or ambiguous")
        require_selected_row(scope, start, end, selected[0])
        source = portal.download_document(document_id)
        source.validate()
        if source.system != "progressive" or source.source_id != document_id or source.source_account != selected[0].get("source_account"):
            raise IntakeHold("Downloaded source does not match the selected carrier document")
        return source

    def pull_fao_communications(self, portal: ProgressivePort, *, start: date, end: date) -> tuple[SourceItem, ...]:
        """Download each in-window FAO Communications memo once.

        Local archive only. No EZLynx task, note, upload, or label, and no
        carrier processed-state change.
        """
        scope = "fao_communications"
        require_bounded_scope(scope, start, end)
        rows = portal.list_documents(scope=scope, start=start, end=end).checked()
        require_unique_memo_rows(rows)
        pending: list[str] = []
        for row in rows:
            if row.get("already_delivered") is True and row.get("requires_action") is True:
                require_carrier_date(start, end, row)
                continue
            require_selected_row(scope, start, end, row)
            pending.append(str(row["document_id"]))
        downloaded: list[SourceItem] = []
        for document_id in pending:
            source = self.retrieve_selected(
                portal, scope=scope, start=start, end=end, document_id=document_id,
            )
            self.archive.preserve(source)
            publish = getattr(portal, "publish_source", None)
            if callable(publish):
                publish(source)
            downloaded.append(source)
        finish = getattr(portal, "finish_fao_pull", None)
        if callable(finish):
            # FAO portal writes one QA pack per date, then holds if the counts disagree.
            finish(start, end)
        return tuple(downloaded)


def require_bounded_scope(scope: str, start: date, end: date) -> None:
    if scope == "fao_communications":
        from .document_retrieval_filing import require_carrier_pull

        require_carrier_pull("fao")
    else:
        require_test()
    if scope not in SCOPES or not (0 <= (end - start).days <= 31):
        raise IntakeHold("An approved Progressive scope and at most 32 inclusive days are required")


def require_selected_row(scope: str, start: date, end: date, row: Mapping[str, object]) -> None:
    if row.get("requires_action") is not True or row.get("already_delivered") is not False:
        raise IntakeHold("Actionability and prior delivery must be explicitly checked")
    if scope == "policies_need_service" and row.get("processed") is not False:
        raise IntakeHold("Policies Need Service selection must be an unticked item")
    require_carrier_date(start, end, row)


def require_carrier_date(start: date, end: date, row: Mapping[str, object]) -> None:
    if not start <= date.fromisoformat(str(row.get("processed_or_effective_date") or "")) <= end:
        raise IntakeHold("Carrier date is outside the requested retrieval window")


def require_unique_memo_rows(rows: tuple[Mapping[str, object], ...] | list[Mapping[str, object]]) -> None:
    ids: list[str] = []
    identities: list[tuple[str, str, str]] = []
    saw_identity = False
    saw_plain = False
    for row in rows:
        document_id = str(row.get("document_id") or "")
        if not document_id:
            raise IntakeHold("Selected carrier document is missing or ambiguous")
        ids.append(document_id)
        if "policy_number" in row or "reason" in row:
            saw_identity = True
            identities.append((
                str(row.get("policy_number") or ""),
                str(row.get("reason") or "").strip().casefold().rstrip("."),
                str(row.get("processed_or_effective_date") or ""),
            ))
        else:
            saw_plain = True
    if saw_identity and saw_plain:
        raise IntakeHold("Selected carrier document is missing or ambiguous")
    if len(ids) != len(set(ids)) or (identities and len(identities) != len(set(identities))):
        raise IntakeHold("Selected carrier document is missing or ambiguous")
