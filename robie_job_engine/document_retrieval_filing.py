"""Shared Document Retrieval filing stage for carrier pull workers.

Pull workers download a portal document, then call :func:`file_carrier_batch`.
Progressive FAO memos use :func:`file_progressive_memos`.

Live EZLynx writes stay off unless ``ROBIE_ENV=TEST``, the hostname is
``hermes-test-01``, and ``ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX=1``. Production
is refused. No timer is installed here.

Documents go through :func:`robie_job_engine.ezlynx_api_only_writes.upload_document_via_api`.
Notes go through :func:`robie_job_engine.ezlynx_api_only_writes.add_note_to_discussion`
when the titled workflow already exists. Browser automation is not used for
either write. DiscussionApi has no create call. A missing workflow still
uploads the PDF, skips the note, and asks :func:`robie_job_engine.zapier_tasks.fire_task`
to open a Nicole review task. The payload comes from
:func:`robie_job_engine.zapier_tasks.document_retrieval_task_payload`, which
maps the display name Nicole Segovia to the EZLynx login ``SSNicole``.
The webhook URL stays in the vault (``custom.zapier-webhook``) and is loaded
by ``bin/zap-trigger``. This module does not resolve the script path.

The status-sheet writer uses the Sheets API when Application Default
Credentials are present. A missing library, credential, daily tab, or carrier
section holds the batch and does not invent a row.
"""

from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .ezlynx_discussions import (
    discussion_id_of,
    discussion_title_of,
    is_untitled_discussion,
    reject_phone_numbers,
)


EASTERN = ZoneInfo("America/New_York")
KILL_SWITCH_ENV = "ROBIE_DOCUMENT_RETRIEVAL_FILE_EZLYNX"
TEST_FILING_HOST = "hermes-test-01"
PRODUCTION_HOSTS = frozenset({"hermes-poc-01"})
PRODUCTION_ENVS = frozenset({"PRODUCTION", "PROD", "LIVE"})
STATUS_SHEET_ID = "1HL6Uw5nAJjZ3qtCleUzXUtOC_xmhFPmy0LPbz89v7vw"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
ROBIE_SIGNATURE = "ROBIE was here"
_GENERIC_NAME_TOKENS = frozenset({"progressive", "memo", "pdf", "document", "the"})
_POLICY_IN_NAME = re.compile(r"[^A-Za-z0-9]+")


class FilingHeld(RuntimeError):
    """This item or batch cannot be filed. Nothing was invented."""


class FilingUnavailable(RuntimeError):
    """EZLynx or the status sheet client is not configured. No write ran."""


@dataclass(frozen=True)
class FilingDecision:
    allowed: bool
    reason: str


NICOLE_ASSIGNEE = "Nicole Segovia"
NO_WORKFLOW_SHEET_COMMENT = (
    "Filed the document. There was no matching workflow, so Nicole has a review task in EZLynx."
)
# Test-only: file every document to one test applicant (Buster Brown) instead
# of the real client. Honored only with ROBIE_ENV=TEST on hermes-test-01.
TEST_APPLICANT_ENV = "ROBIE_DOCUMENT_RETRIEVAL_TEST_APPLICANT_ID"
TEST_DISCUSSION_ENV = "ROBIE_DOCUMENT_RETRIEVAL_TEST_DISCUSSION_TITLE"


@dataclass(frozen=True)
class FilingRule:
    """Mail Sorting destination for one carrier document type.

    ``workflow_title`` empty means this carrier has no confirmed discussion
    title yet. The stage then uploads and opens the Nicole review task.
    ``task_on_missing_workflow`` does the same when a title is set and the
    applicant does not already have that discussion. DiscussionApi is never
    asked to create one.
    """

    carrier_section: str
    document_type: str
    folder: str
    workflow_title: str
    note_label: str
    task_on_missing_workflow: bool = True
    carrier_label: str = ""


def sketch_carrier_rule(
    *,
    carrier_section: str,
    carrier_label: str,
    document_type: str,
    folder: str = "",
    workflow_title: str = "",
    note_label: str = "",
) -> FilingRule:
    """Hook for a pull worker that is not wired in this change.

    Geico, Progressive BOP, NatGen, and Travelers call ``file_carrier_batch``
    with this rule once their draft branch includes the module. Leave
    ``workflow_title`` and ``folder`` empty until that carrier's Mail Sorting
    row is known. An empty title skips Notes and uses the Nicole task.
    """

    return FilingRule(
        carrier_section=carrier_section,
        document_type=document_type,
        folder=folder,
        workflow_title=workflow_title,
        note_label=note_label or f"{carrier_label} {document_type}",
        task_on_missing_workflow=True,
        carrier_label=carrier_label,
    )


PROGRESSIVE_MEMO_RULE = FilingRule(
    carrier_section="Progressive",
    document_type="Memo",
    folder="Additional Information",
    workflow_title="Additional Information - Progressive Memo",
    note_label="Progressive memo",
    task_on_missing_workflow=True,
    carrier_label="Progressive",
)

# Sheet section and document type match the 9/26 status tab. Workflow titles
# for these four are not confirmed in this PR, so the hook files the PDF and
# creates the Nicole task instead of guessing a discussion title.
GEICO_NOC_RULE = sketch_carrier_rule(
    carrier_section="GEICO",
    carrier_label="Geico",
    document_type="Cancellation",
    note_label="Geico cancellation notice",
)
PROGRESSIVE_BOP_RULE = sketch_carrier_rule(
    carrier_section="Progressive BOP/CGL",
    carrier_label="Progressive BOP",
    document_type="Cancellation",
    note_label="Progressive business policy cancellation notice",
)
NATGEN_NOC_RULE = sketch_carrier_rule(
    carrier_section="NatGen",
    carrier_label="NatGen",
    document_type="NOC",
    note_label="NatGen cancellation notice",
)
TRAVELERS_ACTIVITY_RULE = sketch_carrier_rule(
    carrier_section="Travelers",
    carrier_label="Travelers",
    document_type="Policy Activity",
)


@dataclass(frozen=True)
class FilingDeps:
    """Injected clients. Tests pass fakes. Live code builds these after the gate."""

    policy_search: Callable[[str], Any]
    documents_search: Callable[[str], Any]
    list_discussions: Callable[[str], list]
    upload: Callable[..., dict]
    add_note: Callable[..., dict]
    sheets: Any
    activities: Callable[[str], list] | None = None
    fire_task: Callable[..., dict] | None = None


@dataclass(frozen=True)
class SheetEdit:
    kind: str
    row_index: int
    values: list[str]


def eastern_today(now: datetime | None = None) -> date:
    """Calendar day in America/New_York."""

    moment = now if now is not None else datetime.now(EASTERN)
    if moment.tzinfo is None:
        raise FilingHeld("retrieval clock requires a timezone")
    return moment.astimezone(EASTERN).date()


def retrieval_date_window(as_of: date) -> tuple[date, date]:
    """Inclusive processed dates: yesterday and today.

    Monday expands through the previous Friday so Friday, Saturday, and
    Sunday are included with Monday. The FAO memo pull has no Monday
    expansion of its own; this is Carlo's 2026-09-26 standing window.
    """

    if as_of.weekday() == 0:
        return as_of - timedelta(days=3), as_of
    return as_of - timedelta(days=1), as_of


def require_retrieval_window(start: date, end: date, *, as_of: date) -> None:
    """Hold when the requested dates leave the standing window."""

    window_start, window_end = retrieval_date_window(as_of)
    if start > end or start < window_start or end > window_end:
        raise FilingHeld(
            "Processed dates must stay inside the standing retrieval window "
            f"{window_start.isoformat()} through {window_end.isoformat()} "
            "(yesterday and today in America/New_York; Monday includes Friday through Monday)"
        )


def live_filing_decision(environ: Mapping[str, str], hostname: str) -> FilingDecision:
    """Kill switch. Default is off. Production never files."""

    env_name = str(environ.get("ROBIE_ENV") or "").strip().upper()
    host = str(hostname or "").split(".")[0].strip().lower()
    if env_name in PRODUCTION_ENVS or host in PRODUCTION_HOSTS:
        return FilingDecision(
            False,
            "Production document retrieval filing is disabled. No Production timer is installed.",
        )
    if env_name != "TEST":
        return FilingDecision(False, "Document retrieval filing requires ROBIE_ENV=TEST.")
    if host != TEST_FILING_HOST:
        return FilingDecision(False, "Live EZLynx filing runs only on hermes-test-01.")
    if str(environ.get(KILL_SWITCH_ENV) or "").strip() != "1":
        return FilingDecision(
            False,
            f"{KILL_SWITCH_ENV} is off. No EZLynx write was attempted.",
        )
    return FilingDecision(True, "Test filing is enabled on hermes-test-01.")


def status_tab_title(day: date) -> str:
    """Daily tab title, for example ``9/26.``."""

    return f"{day.month}/{day.day}."


def sheet_date_text(day: date) -> str:
    return f"{day.month}/{day.day}/{day.year}"


def nicole_status_comment(rule: FilingRule) -> str:
    return f"Added to the {rule.folder} folder and the {rule.workflow_title} workflow."


def review_task_payload(
    rule: FilingRule,
    *,
    applicant_id: str,
    insured_name: str,
    policy_number: str,
    due_on: date,
) -> dict[str, Any]:
    """Zapier catch-hook payload. ``due_on`` is the Eastern filing day, ISO YYYY-MM-DD.

    The display name ``Nicole Segovia`` is normalized to ``SSNicole`` inside
    :func:`robie_job_engine.zapier_tasks.document_retrieval_task_payload`.
    """

    from .zapier_tasks import document_retrieval_task_payload

    carrier = rule.carrier_label or rule.carrier_section
    return document_retrieval_task_payload(
        applicant_id=str(applicant_id).strip(),
        carrier=carrier,
        doc_type=rule.document_type,
        insured=insured_name,
        policy_number=policy_number,
        due_date=due_on.isoformat(),
        assignee=NICOLE_ASSIGNEE,
    )


def filing_note(rule: FilingRule, processed_on: date) -> str:
    """Short plain-English note. No policy digits, so a long policy cannot look like a phone number."""

    text = (
        f"{rule.note_label} dated {sheet_date_text(processed_on)} was added to the "
        f"{rule.folder} folder. {ROBIE_SIGNATURE}"
    )
    reject_phone_numbers(text)
    if not text.endswith(ROBIE_SIGNATURE):
        raise FilingHeld("note must end with ROBIE was here")
    return text


def _policy_key(value: object) -> str:
    return _POLICY_IN_NAME.sub("", str(value or "")).upper()


def _tokens(value: object) -> frozenset[str]:
    return frozenset(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


def document_is_duplicate(
    *,
    policy_number: str,
    filename: str,
    existing_name: str,
    existing_policy: str = "",
) -> bool:
    """True when an existing EZLynx document is the same policy and a similar file.

    A different memo reason on the same policy is not a duplicate. A second
    copy that adds a suffix still is. A declarations page is not.
    """

    policy = _policy_key(policy_number)
    have_policy = _policy_key(existing_policy)
    if not policy or (have_policy and have_policy != policy):
        return False
    want_tokens = _tokens(Path(str(filename or "")).stem)
    have_tokens = _tokens(existing_name)
    if not want_tokens or not have_tokens:
        return False
    policy_confirmed = have_policy == policy or policy.lower() in have_tokens
    if want_tokens == have_tokens:
        return True
    if not policy_confirmed or "memo" not in have_tokens:
        return False
    distinctive = {token for token in want_tokens if token not in _GENERIC_NAME_TOKENS}
    return bool(distinctive) and distinctive <= have_tokens


def _date_key(value: object) -> str:
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        return sheet_date_text(parsed)
    return raw


def _pad(row: Sequence[Any]) -> list[str]:
    values = [str(cell or "").strip() for cell in row]
    if len(values) < 7:
        values.extend([""] * (7 - len(values)))
    return values[:7]


def find_carrier_section(rows: Sequence[Sequence[Any]], carrier: str) -> int | None:
    """Return the 0-based row whose column A is the carrier section header."""

    want = carrier.casefold()
    for index, raw in enumerate(rows):
        row = _pad(raw)
        if index == 0 and row[1].casefold() == "insured name":
            continue
        if row[0].casefold() == want:
            return index
    return None


def _section_end(rows: Sequence[Sequence[Any]], header: int) -> int:
    end = len(rows)
    for index in range(header + 1, len(rows)):
        if _pad(rows[index])[0]:
            return index
    return end


def plan_status_sheet_edit(
    rows: Sequence[Sequence[Any]],
    *,
    carrier: str,
    insured_name: str,
    policy_number: str,
    department: str,
    document_type: str,
    memo_date: str,
    comment: str,
) -> SheetEdit:
    """Insert under the carrier header, or update the matching policy row.

    Does not create a carrier section or a daily tab.
    """

    header = find_carrier_section(rows, carrier)
    if header is None:
        raise FilingHeld(f"status sheet has no {carrier} section header in column A")
    end = _section_end(rows, header)
    policy = _policy_key(policy_number)
    memo = _date_key(memo_date)
    doc_type = document_type.casefold()
    for index in range(header, end):
        row = _pad(rows[index])
        if _policy_key(row[2]) != policy:
            continue
        if row[4].casefold() != doc_type or _date_key(row[5]) != memo:
            continue
        values = list(row)
        if insured_name and not values[1]:
            values[1] = insured_name
        if department and not values[3]:
            values[3] = department
        values[4] = document_type
        values[5] = memo
        values[6] = comment
        return SheetEdit("update", index, values)
    header_values = _pad(rows[header])
    if not _policy_key(header_values[2]):
        return SheetEdit(
            "update",
            header,
            [carrier, insured_name, policy_number, department, document_type, memo, comment],
        )
    return SheetEdit(
        "insert",
        end,
        ["", insured_name, policy_number, department, document_type, memo, comment],
    )


def _existing_documents(payload: Any) -> list[dict[str, str]]:
    from .ezlynx_api import document_display_fields, extract_document_api_results, extract_document_records

    found: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in extract_document_api_results(payload):
        item = {"id": row.get("id", ""), "name": row.get("name", ""), "policy_number": ""}
        key = (item["id"], item["name"], "")
        if key not in seen:
            seen.add(key)
            found.append(item)
    for row in extract_document_records(payload):
        display = document_display_fields(row)
        item = {
            "id": str(row.get("id") or row.get("Id") or "").strip(),
            "name": display.get("name", ""),
            "policy_number": display.get("policy_number", ""),
        }
        key = (item["id"], item["name"], item["policy_number"])
        if item["name"] and key not in seen:
            seen.add(key)
            found.append(item)
    return found


def _applicants_for_policy(payload: Any, policy_number: str) -> list[str]:
    from .ezlynx_writers import _POLICY_APPLICANT_KEYS, _POLICY_NUMBER_KEYS, _first_present, _policy_records

    want = str(policy_number or "").strip().lower()
    found: list[str] = []
    for record in _policy_records(payload):
        if not any(str(record.get(key) or "").strip().lower() == want for key in _POLICY_NUMBER_KEYS):
            continue
        applicant = _first_present(record, _POLICY_APPLICANT_KEYS)
        if applicant and applicant not in found:
            found.append(applicant)
    return found


def _matching_discussion(discussions: list, title: str) -> dict[str, Any] | None:
    want = " ".join(title.casefold().split())
    matches = []
    for row in discussions:
        if not isinstance(row, dict) or is_untitled_discussion(row):
            continue
        have = " ".join(discussion_title_of(row).casefold().split())
        if have == want:
            matches.append(row)
    if len(matches) > 1:
        raise FilingHeld(
            f"workflow {title!r} matched {len(matches)} discussions; refusing to guess"
        )
    if len(matches) == 1:
        return matches[0]
    return None


def _parse_processed_on(value: object) -> date:
    raw = str(value or "").strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw)
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", raw):
            return datetime.strptime(raw, "%m/%d/%Y").date()
    except ValueError as exc:
        raise FilingHeld("memo date is missing or ambiguous") from exc
    raise FilingHeld("memo date is missing or ambiguous")


def _pdf_bytes(item: Mapping[str, Any]) -> bytes:
    content = item.get("content")
    if isinstance(content, (bytes, bytearray)) and content:
        blob = bytes(content)
    else:
        path = str(item.get("path") or "").strip()
        if not path:
            raise FilingHeld("memo PDF path is missing")
        try:
            blob = Path(path).read_bytes()
        except OSError as exc:
            raise FilingHeld("memo PDF could not be read") from exc
    if not blob.startswith(b"%PDF"):
        raise FilingHeld("memo file is not a PDF")
    return blob


def _item_result(status: str, item: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    wrote = status in {
        "filed",
        "filed_no_workflow",
        "document_filed_note_held",
        "document_filed_task_held",
        "filed_sheet_held",
    }
    payload: dict[str, Any] = {
        "status": status,
        "policy_number": str(item.get("policy_number") or ""),
        "filename": str(item.get("filename") or ""),
        "attempted_writes": wrote,
    }
    payload.update(extra)
    return payload


def _duplicate_on_applicant(
    deps: FilingDeps,
    *,
    applicant_id: str,
    policy_number: str,
    filename: str,
) -> str | None:
    documents = _existing_documents(deps.documents_search(applicant_id))
    for row in documents:
        if document_is_duplicate(
            policy_number=policy_number,
            filename=filename,
            existing_name=row.get("name", ""),
            existing_policy=row.get("policy_number", ""),
        ):
            return "ezlynx_document"
    if deps.activities is None:
        return None
    for row in deps.activities(applicant_id):
        if not isinstance(row, dict):
            continue
        if document_is_duplicate(
            policy_number=policy_number,
            filename=filename,
            existing_name=" ".join(
                str(row.get(key) or "")
                for key in ("name", "title", "subject", "description", "body")
            ),
            existing_policy=str(row.get("policy_number") or row.get("policy") or ""),
        ):
            return "ezlynx_activity"
    return None


def _apply_sheet(deps: FilingDeps, tab: str, edit: SheetEdit) -> None:
    tabs = deps.sheets.list_tabs()
    if tab not in tabs:
        raise FilingHeld(f"status sheet tab {tab!r} does not exist; refusing to create one")
    if edit.kind == "insert":
        deps.sheets.insert_row(tabs[tab], edit.row_index)
        deps.sheets.write_row(tab, edit.row_index + 1, edit.values)
        return
    if edit.kind == "update":
        deps.sheets.write_row(tab, edit.row_index + 1, edit.values)
        return
    raise FilingHeld("status sheet edit is not update or insert")


def _resolve_workflow(
    deps: FilingDeps, applicant_id: str, rule: FilingRule
) -> tuple[str, dict[str, Any] | None, str]:
    """Choose a note on the existing workflow, or the Nicole task fallback.

    Returns ``(mode, discussion, detail)``. ``mode`` is ``note``, ``task``, or
    ``hold``. A missing or ambiguous title does not hold when the rule asks
    for the task fallback. Applicant matching and the document upload are
    separate and can still hold before a write.
    """

    title = str(rule.workflow_title or "").strip()
    if not title:
        return "task", None, "filing rule has no workflow title"
    try:
        matched = _matching_discussion(deps.list_discussions(applicant_id), title)
    except FilingHeld as exc:
        if rule.task_on_missing_workflow:
            return "task", None, str(exc)
        return "hold", None, str(exc)
    except Exception as exc:
        if rule.task_on_missing_workflow:
            return "task", None, f"discussion lookup failed ({type(exc).__name__})"
        return "hold", None, f"discussion lookup failed ({type(exc).__name__})"
    if matched is not None and discussion_id_of(matched):
        return "note", matched, ""
    detail = f"workflow {title!r} is not on the applicant"
    if rule.task_on_missing_workflow:
        return "task", None, detail
    return "hold", None, detail


def _read_back_document_id(uploaded: Mapping[str, Any] | None) -> str:
    document_id = str((uploaded or {}).get("document_id") or "").strip()
    if not document_id or not document_id.isdigit() or not (uploaded or {}).get("read_back"):
        raise FilingHeld("DocumentApi upload did not return a read-back document_id")
    return document_id


_NOTE_REASON_FIELD = re.compile(
    r"note_?id|notecount|mostrecentnoteid|discussionapi|document_id|read[-_ ]?back",
    re.IGNORECASE,
)


def _post_discussion_note(
    deps: FilingDeps,
    applicant_id: str,
    note_text: str,
    discussion_title: str,
    document_id: str,
) -> dict[str, Any]:
    return deps.add_note(
        applicant_id,
        note_text,
        discussion_title=discussion_title,
        document_id=document_id,
    )


def _note_is_confirmed(noted: Mapping[str, Any] | None) -> bool:
    return (
        isinstance(noted, Mapping)
        and noted.get("status") == "filed"
        and bool(str(noted.get("note_id") or "").strip())
        and bool(noted.get("read_back"))
    )


def _unconfirmed_note_reason(noted: Mapping[str, Any] | None) -> str:
    """People see this sentence. It must not name API fields."""

    reason = " ".join(str((noted or {}).get("reason") or "").split())
    if reason and _NOTE_REASON_FIELD.search(reason) is None:
        return reason
    return "The note was sent, but it could not be confirmed. It was not sent again."


def _write_status_comment(
    deps: FilingDeps,
    *,
    tab: str,
    rule: FilingRule,
    insured_name: str,
    policy_number: str,
    department: str,
    processed_on: date,
    comment: str,
) -> None:
    current = deps.sheets.read(tab)
    edit = plan_status_sheet_edit(
        current,
        carrier=rule.carrier_section,
        insured_name=insured_name,
        policy_number=policy_number,
        department=department,
        document_type=rule.document_type,
        memo_date=sheet_date_text(processed_on),
        comment=comment,
    )
    _apply_sheet(deps, tab, edit)


def _file_one(
    item: Mapping[str, Any],
    *,
    rule: FilingRule,
    deps: FilingDeps,
    sheet_day: date,
    zapier_dry_run: bool = False,
) -> dict[str, Any]:
    """File one document, then the next caller waits. Never parallel."""

    policy_number = str(item.get("policy_number") or "").strip()
    filename = str(item.get("filename") or "").strip()
    insured_name = str(item.get("insured_name") or "").strip()
    department = str(item.get("department") or "").strip()
    if not policy_number or not filename or not insured_name:
        return _item_result("held", item, reason="policy, insured name, or filename is missing")
    try:
        processed_on = _parse_processed_on(item.get("processed_on") or item.get("processed_date"))
        pdf = _pdf_bytes(item)
        applicants = _applicants_for_policy(deps.policy_search(policy_number), policy_number)
    except FilingHeld as exc:
        return _item_result("held", item, reason=str(exc))
    except Exception as exc:
        return _item_result("held", item, reason=f"policy lookup failed ({type(exc).__name__})")
    if len(applicants) != 1:
        return _item_result(
            "held",
            item,
            reason="policy search must return one applicant; filing stays one applicant at a time",
        )
    applicant_id = applicants[0]
    try:
        duplicate = _duplicate_on_applicant(
            deps, applicant_id=applicant_id, policy_number=policy_number, filename=filename
        )
    except Exception as exc:
        return _item_result("held", item, reason=f"document search failed ({type(exc).__name__})")
    if duplicate:
        return _item_result(
            "skipped_duplicate",
            item,
            applicant_id=applicant_id,
            reason=f"matching {duplicate} already exists; upload skipped",
        )
    mode, matched, workflow_detail = _resolve_workflow(deps, applicant_id, rule)
    if mode == "hold":
        return _item_result("held", item, applicant_id=applicant_id, reason=workflow_detail)
    tab = status_tab_title(sheet_day)
    try:
        uploaded = deps.upload(applicant_id, filename, pdf, filename=filename)
        document_id = _read_back_document_id(uploaded)
    except FilingHeld as exc:
        return _item_result("held", item, applicant_id=applicant_id, reason=str(exc))
    except Exception as exc:
        return _item_result(
            "held",
            item,
            applicant_id=applicant_id,
            reason=f"document upload failed ({type(exc).__name__})",
        )
    if mode == "note":
        try:
            note_text = filing_note(rule, processed_on)
            noted = _post_discussion_note(
                deps, applicant_id, note_text, rule.workflow_title, document_id
            )
        except Exception as exc:
            return _item_result(
                "document_filed_note_held",
                item,
                applicant_id=applicant_id,
                document_id=document_id,
                reason=f"note failed after upload ({type(exc).__name__})",
            )
        note_id = str((noted or {}).get("note_id") or "").strip()
        if not _note_is_confirmed(noted):
            return _item_result(
                "document_filed_note_held",
                item,
                applicant_id=applicant_id,
                document_id=document_id,
                reason=_unconfirmed_note_reason(noted),
            )
        comment = nicole_status_comment(rule)
        discussion_id = str((noted or {}).get("discussion_id") or "") or discussion_id_of(matched or {})
        try:
            _write_status_comment(
                deps,
                tab=tab,
                rule=rule,
                insured_name=insured_name,
                policy_number=policy_number,
                department=department,
                processed_on=processed_on,
                comment=comment,
            )
        except Exception as exc:
            return _item_result(
                "filed_sheet_held",
                item,
                applicant_id=applicant_id,
                document_id=document_id,
                note_id=note_id,
                discussion_id=discussion_id,
                reason=f"status sheet update failed ({type(exc).__name__})",
            )
        return _item_result(
            "filed",
            item,
            applicant_id=applicant_id,
            document_id=document_id,
            note_id=note_id,
            discussion_id=discussion_id,
            comment=comment,
            folder=rule.folder,
            workflow_title=rule.workflow_title,
            folder_field="not_in_proven_document_upload",
        )
    if deps.fire_task is None:
        return _item_result(
            "document_filed_task_held",
            item,
            applicant_id=applicant_id,
            document_id=document_id,
            reason="Zapier task client is not configured",
        )
    try:
        payload = review_task_payload(
            rule,
            applicant_id=applicant_id,
            insured_name=insured_name,
            policy_number=policy_number,
            due_on=sheet_day,
        )
        fired = deps.fire_task(payload, dry_run=zapier_dry_run)
    except Exception as exc:
        return _item_result(
            "document_filed_task_held",
            item,
            applicant_id=applicant_id,
            document_id=document_id,
            reason=f"review task failed after upload ({type(exc).__name__})",
        )
    if not isinstance(fired, dict) or fired.get("ok") is not True:
        return _item_result(
            "document_filed_task_held",
            item,
            applicant_id=applicant_id,
            document_id=document_id,
            reason="zap-trigger did not accept the review task",
        )
    try:
        _write_status_comment(
            deps,
            tab=tab,
            rule=rule,
            insured_name=insured_name,
            policy_number=policy_number,
            department=department,
            processed_on=processed_on,
            comment=NO_WORKFLOW_SHEET_COMMENT,
        )
    except Exception as exc:
        return _item_result(
            "filed_sheet_held",
            item,
            applicant_id=applicant_id,
            document_id=document_id,
            task_title=payload["task_title"],
            reason=f"status sheet update failed ({type(exc).__name__})",
        )
    return _item_result(
        "filed_no_workflow",
        item,
        applicant_id=applicant_id,
        document_id=document_id,
        comment=NO_WORKFLOW_SHEET_COMMENT,
        task_title=payload["task_title"],
        task_due_date=payload["due_date"],
        task_assignee=payload["assignee"],
        task_source=payload["source"],
        zapier_dry_run=zapier_dry_run,
        workflow_detail=workflow_detail,
        folder_field="not_in_proven_document_upload",
    )


def _overall_status(results: list[dict[str, Any]]) -> str:
    statuses = {row["status"] for row in results}
    if not results:
        return "empty"
    if statuses & {"held", "document_filed_note_held", "document_filed_task_held", "filed_sheet_held"}:
        return "held"
    if "filed" in statuses:
        return "filed"
    if statuses == {"filed_no_workflow"} or statuses == {"filed_no_workflow", "skipped_duplicate"}:
        return "filed_no_workflow"
    if statuses == {"skipped_duplicate"}:
        return "skipped_duplicate"
    return "held"


def _batch(
    status: str,
    reason: str,
    *,
    results: list[dict[str, Any]] | None = None,
    attempted_writes: bool = False,
    activities_check: str = "not_used",
) -> dict[str, Any]:
    rows = results or []
    return {
        "status": status,
        "reason": reason,
        "results": rows,
        "attempted_writes": attempted_writes or any(row.get("attempted_writes") for row in rows),
        "activities_check": activities_check,
        "count": len(rows),
    }


def test_applicant_override(environ: Mapping[str, str], hostname: str) -> str | None:
    """The Test-only applicant every document is filed to, or ``None``.

    Set outside Test (any other ROBIE_ENV or host) it refuses instead of
    being ignored, so a stray setting can never reach a real client.
    """

    raw = str(environ.get(TEST_APPLICANT_ENV) or "").strip()
    if not raw:
        return None
    env_name = str(environ.get("ROBIE_ENV") or "").strip().upper()
    host = str(hostname or "").split(".")[0].strip().lower()
    if env_name != "TEST" or host != TEST_FILING_HOST:
        raise FilingHeld(
            f"{TEST_APPLICANT_ENV} is only allowed with ROBIE_ENV=TEST on {TEST_FILING_HOST}; refusing"
        )
    if not raw.isdigit():
        raise FilingHeld(f"{TEST_APPLICANT_ENV} must be a numeric EZLynx applicant id")
    return raw


def _hint_matches(rows: list[dict[str, Any]], hint: str) -> list[dict[str, Any]]:
    # Same rule the Notes writer uses: case-insensitive substring.
    want = hint.strip().lower()
    return [row for row in rows if want and want in discussion_title_of(row).lower()]


def choose_test_discussion(
    discussions: list, *, preferred: Sequence[str]
) -> tuple[dict[str, Any] | None, list[str]]:
    """Pick an existing titled discussion on the test applicant. Never creates one.

    The first title in ``preferred`` that names exactly one titled discussion
    (by the Notes writer's own substring rule) wins. Returns the record, or
    ``None``, and every titled discussion seen, for the report.
    """

    rows = [
        row for row in (discussions or [])
        if isinstance(row, dict) and not is_untitled_discussion(row) and discussion_id_of(row)
    ]
    titles = [discussion_title_of(row) for row in rows]
    for title in preferred:
        title = str(title or "").strip()
        if not title:
            continue
        exact = [row for row in rows if " ".join(discussion_title_of(row).casefold().split()) == " ".join(title.casefold().split())]
        if len(exact) == 1 and len(_hint_matches(rows, title)) == 1:
            return exact[0], titles
    return None, titles


def _display_policy(policy_number: str) -> str:
    """Full policy number, or its last four when the digits would look dialable."""

    text = str(policy_number or "").strip()
    try:
        reject_phone_numbers(text)
        return text
    except Exception:
        runs = [run for run in re.findall(r"\d+", text) if len(run) >= 4]
        return f"ending in {runs[0][-4:]}" if runs else "on file"


def test_account_note(rule: FilingRule, *, insured_name: str, policy_number: str, processed_on: date) -> str:
    """Plain-English note naming the real client and policy, e.g.
    "Progressive memo dated 9/29/2026 for Groesbeck, Zachary, policy 876263535,
    saved to this test account. ROBIE was here"."""

    policy = _display_policy(policy_number)
    policy_text = f"policy {policy}"
    text = (
        f"{rule.note_label} dated {sheet_date_text(processed_on)} for {insured_name.strip()}, "
        f"{policy_text}, saved to this test account. {ROBIE_SIGNATURE}"
    )
    reject_phone_numbers(text)
    return text


def _file_one_test_account(
    item: Mapping[str, Any],
    *,
    rule: FilingRule,
    deps: FilingDeps,
    applicant_id: str,
    discussion: Mapping[str, Any],
) -> dict[str, Any]:
    """Upload to the test applicant and add the note. No real-client lookup,
    no status sheet, no review task."""

    policy_number = str(item.get("policy_number") or "").strip()
    filename = str(item.get("filename") or "").strip()
    insured_name = str(item.get("insured_name") or "").strip()
    base = {"applicant_id": applicant_id, "real_client": insured_name, "real_policy": policy_number}
    if not policy_number or not filename or not insured_name:
        return _item_result("held", item, reason="policy, insured name, or filename is missing", **base)
    if _policy_key(policy_number) not in _policy_key(filename):
        return _item_result("held", item, reason="upload file name must include the real policy number", **base)
    try:
        processed_on = _parse_processed_on(item.get("processed_on") or item.get("processed_date"))
        pdf = _pdf_bytes(item)
    except FilingHeld as exc:
        return _item_result("held", item, reason=str(exc), **base)
    try:
        duplicate = _duplicate_on_applicant(
            deps, applicant_id=applicant_id, policy_number=policy_number, filename=filename
        )
    except Exception as exc:
        return _item_result("held", item, reason=f"document search failed ({type(exc).__name__})", **base)
    if duplicate:
        return _item_result(
            "skipped_duplicate", item, reason=f"matching {duplicate} already exists; upload skipped", **base
        )
    title = discussion_title_of(dict(discussion))
    try:
        note_text = test_account_note(
            rule, insured_name=insured_name, policy_number=policy_number, processed_on=processed_on
        )
    except Exception as exc:
        return _item_result("held", item, reason=f"note text was refused ({type(exc).__name__})", **base)
    try:
        uploaded = deps.upload(applicant_id, filename, pdf, filename=filename)
        document_id = _read_back_document_id(uploaded)
    except FilingHeld as exc:
        return _item_result("held", item, reason=str(exc), **base)
    except Exception as exc:
        return _item_result("held", item, reason=f"document upload failed ({type(exc).__name__})", **base)
    try:
        noted = _post_discussion_note(deps, applicant_id, note_text, title, document_id)
    except Exception as exc:
        return _item_result(
            "document_filed_note_held", item, document_id=document_id,
            reason=f"note failed after upload ({type(exc).__name__})", **base,
        )
    note_id = str((noted or {}).get("note_id") or "").strip()
    discussion_id = str((noted or {}).get("discussion_id") or "") or discussion_id_of(dict(discussion))
    if not _note_is_confirmed(noted):
        return _item_result(
            "document_filed_note_held", item, document_id=document_id, discussion_id=discussion_id,
            reason=_unconfirmed_note_reason(noted), **base,
        )
    return _item_result(
        "filed", item, document_id=document_id, note_id=note_id, discussion_id=discussion_id,
        discussion_title=title, note_text=note_text, test_account=True, **base,
    )


def _file_test_account_batch(
    items: Sequence[Mapping[str, Any]],
    *,
    rule: FilingRule,
    applicant_id: str,
    environ: Mapping[str, str],
    factory: Callable[[], FilingDeps],
) -> dict[str, Any]:
    try:
        deps = factory()
    except Exception as exc:
        return _batch("held", f"EZLynx filing client is unavailable ({type(exc).__name__}: {exc})")
    try:
        discussions = deps.list_discussions(applicant_id)
    except Exception as exc:
        return _batch("held", f"discussion lookup failed ({type(exc).__name__})")
    preferred = [rule.workflow_title, str(environ.get(TEST_DISCUSSION_ENV) or "")]
    chosen, titles = choose_test_discussion(discussions, preferred=preferred)
    if chosen is None:
        batch = _batch(
            "held",
            "no existing discussion on the test account matches the configured title; nothing was filed",
        )
        batch["test_account_discussions"] = titles
        return batch
    results = [
        _file_one_test_account(item, rule=rule, deps=deps, applicant_id=applicant_id, discussion=chosen)
        for item in items
    ]
    status = _overall_status(results)
    reason = {
        "filed": "documents and notes were read back on the test account",
        "skipped_duplicate": "every document was already on the test account",
        "held": "one or more documents were not filed",
    }.get(status, status)
    batch = _batch(status, reason, results=results, activities_check="not_used")
    batch["test_account"] = applicant_id
    batch["test_account_discussion"] = {
        "id": discussion_id_of(chosen),
        "title": discussion_title_of(chosen),
    }
    return batch


def file_carrier_batch(
    items: Sequence[Mapping[str, Any]],
    *,
    rule: FilingRule,
    environ: Mapping[str, str] | None = None,
    hostname: str | None = None,
    client_factory: Callable[[], FilingDeps] | None = None,
    sheet_day: date | None = None,
    zapier_dry_run: bool = False,
) -> dict[str, Any]:
    """File each item only after the previous one returns. The kill switch is checked first."""

    env = os.environ if environ is None else environ
    host = socket.gethostname() if hostname is None else hostname
    decision = live_filing_decision(env, host)
    if not decision.allowed:
        return _batch("disabled", decision.reason)
    try:
        test_applicant = test_applicant_override(env, host)
    except FilingHeld as exc:
        return _batch("held", str(exc))
    if not items:
        return _batch("empty", "no local documents to file")
    day = sheet_day or eastern_today()
    if test_applicant is not None:
        return _file_test_account_batch(
            items,
            rule=rule,
            applicant_id=test_applicant,
            environ=env,
            factory=client_factory or (lambda: build_live_deps(test_account=True)),
        )
    factory = client_factory or build_live_deps
    try:
        deps = factory()
    except Exception as exc:
        return _batch(
            "held",
            f"EZLynx filing client is unavailable ({type(exc).__name__}: {exc})",
        )
    if deps.sheets is None:
        return _batch("held", "status sheet client is not configured; refusing to invent a row")
    tab = status_tab_title(day)
    try:
        tabs = deps.sheets.list_tabs()
        if tab not in tabs:
            raise FilingHeld(f"status sheet tab {tab!r} does not exist; refusing to create one")
        preview = deps.sheets.read(tab)
        if find_carrier_section(preview, rule.carrier_section) is None:
            raise FilingHeld(
                f"status sheet tab {tab!r} has no {rule.carrier_section} section header"
            )
    except Exception as exc:
        return _batch("held", str(exc) if isinstance(exc, FilingHeld) else f"status sheet is unavailable ({type(exc).__name__})")
    results: list[dict[str, Any]] = []
    for item in items:
        results.append(
            _file_one(item, rule=rule, deps=deps, sheet_day=day, zapier_dry_run=zapier_dry_run)
        )
    status = _overall_status(results)
    activities_check = "checked" if deps.activities is not None else "not_used"
    reason = {
        "filed": "documents and notes were read back",
        "filed_no_workflow": "documents were filed and a Nicole review task was created",
        "skipped_duplicate": "every document was already filed",
        "held": "one or more documents were not filed",
    }.get(status, status)
    return _batch(status, reason, results=results, activities_check=activities_check)


def file_progressive_memos(
    items: Sequence[Mapping[str, Any]],
    *,
    environ: Mapping[str, str] | None = None,
    hostname: str | None = None,
    client_factory: Callable[[], FilingDeps] | None = None,
    sheet_day: date | None = None,
    zapier_dry_run: bool = False,
) -> dict[str, Any]:
    """Progressive Communications memos use the Additional Information workflow."""

    return file_carrier_batch(
        items,
        rule=PROGRESSIVE_MEMO_RULE,
        environ=environ,
        hostname=hostname,
        client_factory=client_factory,
        sheet_day=sheet_day,
        zapier_dry_run=zapier_dry_run,
    )


def build_live_deps(*, test_account: bool = False) -> FilingDeps:
    """Real Documents, Notes, and Sheets clients. Raises when any client is missing.

    ``test_account`` (the Buster Brown override on hermes-test-01) builds only
    Documents and Notes. The test applicant lives in live EZLynx, so with
    ``ROBIE_EZLYNX_DISCUSSION_API=live`` DocumentApi uses the same live API
    secret the Notes client does, refusing any UAT host. Every write still
    passes the ``ROBIE_EZLYNX_WRITE_APPLICANT_IDS`` allowlist.
    """

    from .ezlynx_api import EzlynxApiClient, EzlynxApiConfigurationError, load_ezlynx_api_config
    from .ezlynx_api_only_writes import (
        _discussion_config_from_secret,
        add_note_to_discussion,
        upload_document_via_api,
    )
    from .ezlynx_discussions import DiscussionApiClient

    try:
        if test_account and str(os.environ.get("ROBIE_EZLYNX_DISCUSSION_API") or "").strip().lower() == "live":
            config = load_ezlynx_api_config(environment="PRODUCTION")
            hosts = f"{config.token_endpoint} {config.document_base_url}".casefold()
            if "uatezlynx" in hosts:
                raise FilingUnavailable("live EZLynx API secret points at UAT; refusing")
            api = EzlynxApiClient(config)
        else:
            api = EzlynxApiClient(load_ezlynx_api_config())
        discussions = DiscussionApiClient(_discussion_config_from_secret())
    except FilingUnavailable:
        raise
    except EzlynxApiConfigurationError as exc:
        raise FilingUnavailable(f"EZLynx API is not configured: {exc}") from exc
    except Exception as exc:
        raise FilingUnavailable(f"EZLynx API client could not be built ({type(exc).__name__})") from exc
    sheets = None if test_account else build_status_sheet_client()

    def upload(applicant_id: str, document_name: str, file_bytes: bytes, filename: str) -> dict:
        return upload_document_via_api(
            applicant_id,
            document_name,
            file_bytes,
            client=api,
            filename=filename,
            file_content_type="application/pdf",
        )

    def add_note(
        applicant_id: str,
        note_text: str,
        discussion_title: str,
        document_id: str | None = None,
    ) -> dict:
        return add_note_to_discussion(
            applicant_id,
            note_text,
            discussion_title=discussion_title,
            title_hint=discussion_title,
            discussion_client=discussions,
            document_id=document_id,
        )

    def fire(payload: dict, *, dry_run: bool = False) -> dict:
        from .zapier_tasks import fire_task

        return fire_task(payload, dry_run=dry_run)

    return FilingDeps(
        policy_search=api.search_policy_by_number,
        documents_search=api.search_applicant_documents,
        list_discussions=discussions.get_discussions,
        upload=upload,
        add_note=add_note,
        sheets=sheets,
        activities=None,
        fire_task=None if test_account else fire,
    )


def build_status_sheet_client() -> "GoogleStatusSheetClient":
    """ADC Sheets client. Missing credentials fail closed and do not fake a row."""

    try:
        import google.auth
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise FilingUnavailable(
            "Google Sheets library is not installed. TODO: install the Sheets "
            f"client on hermes-test-01 and share {STATUS_SHEET_ID} with the Test "
            "service account. Refusing to invent a status-sheet row."
        ) from exc
    try:
        credentials, _ = google.auth.default(scopes=[SHEETS_SCOPE])
        if getattr(credentials, "requires_scopes", False):
            credentials = credentials.with_scopes([SHEETS_SCOPE])
        refresh = getattr(credentials, "refresh", None)
        if callable(refresh) and not getattr(credentials, "valid", True):
            refresh(Request())
        service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    except Exception as exc:
        raise FilingUnavailable(
            "Google Sheets credentials are not available. TODO: grant "
            f"{SHEETS_SCOPE} on the hermes-test-01 service account and share "
            f"spreadsheet {STATUS_SHEET_ID}. Refusing to invent a status-sheet row."
        ) from exc
    return GoogleStatusSheetClient(service, STATUS_SHEET_ID)


class GoogleStatusSheetClient:
    """Narrow Sheets wrapper. Creates neither tabs nor carrier sections."""

    def __init__(self, service: Any, spreadsheet_id: str) -> None:
        self._service = service
        self._spreadsheet_id = spreadsheet_id

    def list_tabs(self) -> dict[str, int]:
        meta = self._service.spreadsheets().get(spreadsheetId=self._spreadsheet_id).execute()
        tabs: dict[str, int] = {}
        for sheet in meta.get("sheets") or []:
            props = sheet.get("properties") or {}
            title = str(props.get("title") or "")
            sheet_id = props.get("sheetId")
            if title and sheet_id is not None:
                tabs[title] = int(sheet_id)
        return tabs

    def read(self, tab: str) -> list[list[str]]:
        result = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=f"'{tab}'!A1:G500")
            .execute()
        )
        return result.get("values") or []

    def insert_row(self, sheet_id: int, before_index: int) -> None:
        self._service.spreadsheets().batchUpdate(
            spreadsheetId=self._spreadsheet_id,
            body={
                "requests": [
                    {
                        "insertDimension": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "ROWS",
                                "startIndex": before_index,
                                "endIndex": before_index + 1,
                            },
                            "inheritFromBefore": True,
                        }
                    }
                ]
            },
        ).execute()

    def write_row(self, tab: str, row_number: int, values: list[str]) -> None:
        (
            self._service.spreadsheets()
            .values()
            .update(
                spreadsheetId=self._spreadsheet_id,
                range=f"'{tab}'!A{row_number}:G{row_number}",
                valueInputOption="RAW",
                body={"values": [values]},
            )
            .execute()
        )


__all__ = [
    "GEICO_NOC_RULE",
    "KILL_SWITCH_ENV",
    "NATGEN_NOC_RULE",
    "NO_WORKFLOW_SHEET_COMMENT",
    "PROGRESSIVE_BOP_RULE",
    "PROGRESSIVE_MEMO_RULE",
    "STATUS_SHEET_ID",
    "TRAVELERS_ACTIVITY_RULE",
    "FilingDeps",
    "FilingHeld",
    "FilingRule",
    "FilingUnavailable",
    "build_live_deps",
    "document_is_duplicate",
    "eastern_today",
    "file_carrier_batch",
    "file_progressive_memos",
    "filing_note",
    "live_filing_decision",
    "nicole_status_comment",
    "plan_status_sheet_edit",
    "review_task_payload",
    "sketch_carrier_rule",
    "require_retrieval_window",
    "retrieval_date_window",
    "status_tab_title",
]


CARRIER_RULES = {
    "fao": PROGRESSIVE_MEMO_RULE,
    "bop": PROGRESSIVE_BOP_RULE,
    "geico": GEICO_NOC_RULE,
    "natgen": NATGEN_NOC_RULE,
}
_INSURED_KEYS = ("insured_name", "named_insured", "insured", "client")


def pack_filing_items(output_dir: str | Path) -> list[dict[str, Any]]:
    """Filing items from a pull's QA packs (``<output>/<YYYY-MM-DD>/manifest.json``).

    Only pulled PDFs that are on disk next to their manifest are returned.
    """

    root = Path(output_dir)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for manifest_path in sorted(root.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        processed = str(manifest.get("processed_date") or manifest.get("report_date") or manifest_path.parent.name)
        for value in manifest.values():
            if not isinstance(value, list):
                continue
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                filename = str(entry.get("filename") or "").strip()
                policy = str(entry.get("policy_number") or "").strip()
                insured = next((str(entry.get(k) or "").strip() for k in _INSURED_KEYS if entry.get(k)), "")
                disposition = str(entry.get("disposition") or "pulled").strip().lower()
                path = manifest_path.parent / filename
                if not filename or not policy or disposition != "pulled" or not path.is_file():
                    continue
                if str(path) in seen:
                    continue
                seen.add(str(path))
                items.append(
                    {
                        "policy_number": policy,
                        "insured_name": insured,
                        "filename": filename,
                        "path": str(path),
                        "processed_on": str(entry.get("processed_date") or processed),
                    }
                )
    return items


def main(argv: Sequence[str] | None = None) -> int:
    """File pulled PDFs from one carrier's QA packs through the shared API path.

    Every gate above still applies: the kill switch, the Test host, and (for
    the Buster Brown test) the Test-only applicant override.
    """

    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--carrier", required=True, choices=sorted(CARRIER_RULES))
    parser.add_argument("--output-dir", required=True, help="pull output folder holding <date>/manifest.json")
    parser.add_argument("--list-only", action="store_true", help="print the items and file nothing")
    args = parser.parse_args(list(argv) if argv is not None else None)
    items = pack_filing_items(args.output_dir)
    if args.list_only:
        print(json.dumps({"status": "listed", "count": len(items), "items": items}, indent=2))
        return 0
    result = file_carrier_batch(items, rule=CARRIER_RULES[args.carrier])
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") in {"filed", "skipped_duplicate", "empty", "filed_no_workflow"} else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
