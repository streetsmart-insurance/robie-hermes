"""Test-only NatGen retrieval for Pending Cancellation NOC.

Wave A navigates ``pending_cancellation_noc`` only. Policy To Dos Additional
Information is an approved scope name that refuses before any portal click.
This module does not upload, note, task, or label in EZLynx.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Mapping, Protocol

from .intake_core import IntakeHold, IntakeWorker, ReadResult, SourceItem, require_test


NOC_SCOPE = "pending_cancellation_noc"
ADDITIONAL_INFO_SCOPE = "policy_todos_additional_info"
SCOPES = frozenset({NOC_SCOPE, ADDITIONAL_INFO_SCOPE})
ADDITIONAL_INFO_TODO = (
    "Policy To Dos Additional Information is TODO; "
    "Wave A navigates Pending Cancellations NOC only"
)


class NocDateHold(IntakeHold):
    """The opened NOC is not the list row's document. Do not file it."""


class NatGenPort(Protocol):
    def list_documents(self, *, scope: str, start: date, end: date) -> ReadResult: ...
    def download_document(self, document_id: str) -> SourceItem: ...


class NatGenRetrieval(IntakeWorker):
    process = "natgen"
    source_system = "natgen"
    title = "NatGen document intake: review pending cancellation NOC"

    def retrieve_selected(self, portal: NatGenPort, *, scope: str, start: date, end: date, document_id: str) -> SourceItem:
        require_bounded_scope(scope, start, end)
        rows = portal.list_documents(scope=scope, start=start, end=end).checked()
        selected = [row for row in rows if row.get("document_id") == document_id]
        if len(selected) != 1:
            raise IntakeHold("Selected carrier document is missing or ambiguous")
        require_selected_row(scope, start, end, selected[0])
        source = portal.download_document(document_id)
        source.validate()
        if source.system != "natgen" or source.source_id != document_id or source.source_account != selected[0].get("source_account"):
            raise IntakeHold("Downloaded source does not match the selected carrier document")
        return source

    def pull_pending_cancellation_noc(self, portal: NatGenPort, *, start: date, end: date) -> tuple[SourceItem, ...]:
        """Download each in-window Pending Cancellation NOC once.

        The portal list is not day-filtered. ``start`` and ``end`` are the
        requested window; a Monday in that window also keeps the preceding
        Saturday and Sunday. Local archive only. No EZLynx task, note, upload,
        or label, and no carrier processed-state change.
        """
        scope = NOC_SCOPE
        require_bounded_scope(scope, start, end)
        scrub_start, scrub_end = scrub_window(start, end)
        note = getattr(portal, "note_requested_window", None)
        if callable(note):
            note(start, end)
        rows = portal.list_documents(scope=scope, start=scrub_start, end=scrub_end).checked()
        require_unique_noc_rows(rows)
        pending: list[str] = []
        for row in rows:
            if row.get("already_delivered") is True and row.get("requires_action") is True:
                require_carrier_date(scrub_start, scrub_end, row)
                continue
            require_selected_row(scope, scrub_start, scrub_end, row)
            pending.append(str(row["document_id"]))
        downloaded: list[SourceItem] = []
        for document_id in pending:
            try:
                source = self.retrieve_selected(
                    portal, scope=scope, start=scrub_start, end=scrub_end, document_id=document_id,
                )
            except NocDateHold:
                continue
            self.archive.preserve(source)
            publish = getattr(portal, "publish_source", None)
            if callable(publish):
                publish(source)
            downloaded.append(source)
        finish = getattr(portal, "finish_natgen_pull", None)
        if callable(finish):
            finish(scrub_start, scrub_end)
        return tuple(downloaded)

    def pull_policy_todos_additional_info(self, portal: NatGenPort, *, start: date, end: date) -> tuple[SourceItem, ...]:
        """Wave A stub. Does not navigate Policy To Dos Additional Information."""
        require_bounded_scope(ADDITIONAL_INFO_SCOPE, start, end)
        del portal
        raise IntakeHold(ADDITIONAL_INFO_TODO)


def scrub_window(start: date, end: date) -> tuple[date, date]:
    """Include the weekend before every Monday in the requested window.

    NatGen Pending Cancellations is not day-filtered. A Monday pull must keep
    Saturday and Sunday process dates that were not pulled on Friday. Other
    weekdays stay on the requested dates. Calling this twice is the same window.
    """
    if end < start:
        raise IntakeHold("Process date window is missing or ambiguous")
    scrub_start = start
    day = start
    while day <= end:
        if day.weekday() == 0:
            saturday = day - timedelta(days=2)
            if saturday < scrub_start:
                scrub_start = saturday
        day += timedelta(days=1)
    return scrub_start, end


def require_bounded_scope(scope: str, start: date, end: date) -> None:
    if scope == NOC_SCOPE:
        from .document_retrieval_filing import require_carrier_pull

        require_carrier_pull("natgen")
    else:
        require_test()
    if scope not in SCOPES or not (0 <= (end - start).days <= 31):
        raise IntakeHold("An approved NatGen scope and at most 32 inclusive days are required")


def require_selected_row(scope: str, start: date, end: date, row: Mapping[str, object]) -> None:
    del scope
    if row.get("requires_action") is not True or row.get("already_delivered") is not False:
        raise IntakeHold("Actionability and prior delivery must be explicitly checked")
    require_carrier_date(start, end, row)


def require_carrier_date(start: date, end: date, row: Mapping[str, object]) -> None:
    if not start <= date.fromisoformat(str(row.get("processed_or_effective_date") or "")) <= end:
        raise IntakeHold("Carrier date is outside the requested retrieval window")


def require_unique_noc_rows(rows: tuple[Mapping[str, object], ...] | list[Mapping[str, object]]) -> None:
    ids: list[str] = []
    identities: list[tuple[str, str, str, str]] = []
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
                str(row.get("cancel_effective_date") or ""),
            ))
        else:
            saw_plain = True
    if saw_identity and saw_plain:
        raise IntakeHold("Selected carrier document is missing or ambiguous")
    if len(ids) != len(set(ids)) or (identities and len(identities) != len(set(identities))):
        raise IntakeHold("Selected carrier document is missing or ambiguous")
