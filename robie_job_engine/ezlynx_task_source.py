#!/usr/bin/env python3
"""Where the intake gets Robie's tasks: the emailed report, or the EZLynx API.

``ROBIE_TASK_SOURCE`` picks the source.

* ``report`` (the default, and anything not exactly ``api``): the emailed
  "Robie AI - Task Check-In" report, as before.
* ``api``: ask EZLynx for the tasks assigned to Robie AI (SSRobie, id 438318)
  and treat the answer as the report. When the API cannot give a complete,
  well-formed answer for ANY reason, the intake falls back to the emailed
  report for that run. The fallback is the same code path as today.

Everything after the source is unchanged: the seen-task store and baseline,
the one-time hold notes, the batch cap, the daily call cap, the dedupe, the
calling window, the kill switch, the write-scope guard and the lease. The
source only produces ``IngestedReport``/``AssignedTask`` objects.

STATUS: the plumbing is proven offline with a stub client. The live listing
call is UNVERIFIED. EZLynx told us on 2026-10-02 that tasks are notes
(``TaskCreationNote``), that the Task endpoints are create/update by id, and
that notes are read back by id or per discussion. The 2026-10-03 probe found
no label fields on discussions. No endpoint that lists tasks by assignee, and
no call label on a note, has been confirmed. Until
``ROBIE_TASK_API_LIST_PATH`` names a verified path, ``LiveTaskListClient``
raises ``TaskSourceUnavailable`` and the intake uses the report. A task with
no label is never dialed (an unlabeled task is left untouched), so an API
that cannot show labels cannot start a call.

Auth is the existing Task API login (``ezlynx_task_api``): vendor_data_access
acting as ``ROBIE_EZLYNX_TASK_API_ACT_AS_USERNAME`` (carlo1), the Production
EZLynx API secret, the token cache, and the one-failed-login 60 minute stop
shared with task creation. ``ROBIE_EZLYNX_DIRECT_TASK_API_ENABLED=0`` turns
the API source off too. This module only reads. It never writes to EZLynx.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Protocol
from urllib import parse, request

from .ezlynx_task_inbox import IngestedReport
from .ezlynx_task_report import AssignedTask, SOURCE_TASK
from .report_clock import NEW_YORK, report_created_et

logger = logging.getLogger("ezlynx_task_source")

SOURCE_ENV = "ROBIE_TASK_SOURCE"
MODE_REPORT = "report"
MODE_API = "api"
ASSIGNEE_ID_ENV = "ROBIE_TASK_SOURCE_ASSIGNEE_ID"
LIST_PATH_ENV = "ROBIE_TASK_API_LIST_PATH"
# SSRobie, from ezlynx_user_ids. Robie AI in the report is this login.
ROBIE_ASSIGNEE_ID = 438318
ROBIE_ASSIGNEE_NAME = "Robie AI"
API_FILENAME = "ezlynx-task-api"
MESSAGE_PREFIX = "api:"
# Statuses that are not open work. Anything else (including blank) is open.
CLOSED_STATUSES = frozenset({
    "complete", "completed", "closed", "done", "cancelled", "canceled", "deleted",
})


class TaskSourceUnavailable(RuntimeError):
    """The API could not give a complete, well-formed task list. Use the report."""


@dataclass(frozen=True)
class TaskListing:
    """What a client returns. ``complete`` is True only when every row was read."""

    records: tuple[dict[str, Any], ...]
    complete: bool


class TaskListClient(Protocol):
    def list_assigned_tasks(self, assignee_id: int) -> TaskListing: ...


def task_source_mode(env: Any = None) -> str:
    source = os.environ if env is None else env
    value = str(source.get(SOURCE_ENV) or "").strip().casefold()
    return MODE_API if value == MODE_API else MODE_REPORT


def assignee_id(env: Any = None) -> int:
    source = os.environ if env is None else env
    raw = str(source.get(ASSIGNEE_ID_ENV) or "").strip()
    if not raw:
        return ROBIE_ASSIGNEE_ID
    if not raw.isdigit():
        raise TaskSourceUnavailable(f"{ASSIGNEE_ID_ENV} is not a number")
    return int(raw)


# --------------------------------------------------------------- normalizing


def _first(record: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = record.get(key)
        if value in (None, "") or isinstance(value, (dict, list, bool)):
            continue
        return str(value).strip()
    return ""


def _digits(value: str, what: str) -> str:
    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    if not text.isdigit():
        raise TaskSourceUnavailable(f"task record has no usable {what}")
    return text


def _labels(record: dict[str, Any]) -> str:
    raw: Any = None
    for key in ("labels", "Labels", "noteLabels", "activity_labels", "activityLabels"):
        if record.get(key) not in (None, ""):
            raw = record[key]
            break
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    names: list[str] = []
    for item in raw if isinstance(raw, list) else [raw]:
        if isinstance(item, dict):
            name = _first(item, ("labelName", "name", "label", "title"))
        else:
            name = str(item or "").strip()
        if name:
            names.append(name)
    return ", ".join(names)


def record_to_task(record: dict[str, Any], expected_assignee: int) -> AssignedTask | None:
    """One API record as an AssignedTask, or None when it is not open Robie work.

    Accepts the flat shape documented here or a ``TaskCreationNote`` shaped
    like the notes the Discussion API returns (``task`` holds the assignee,
    due date and task id). A record missing the task id, applicant id,
    discussion id or a readable creation time raises: a listing with a
    row we cannot read is not a complete listing.
    """
    if not isinstance(record, dict):
        raise TaskSourceUnavailable("task record is not an object")
    task_part = record.get("task") if isinstance(record.get("task"), dict) else {}
    flat = {**record, **task_part}
    assignee = _first(flat, ("assigned_user_id", "assignedUserId", "AssignedUserId"))
    if not assignee.isdigit():
        raise TaskSourceUnavailable("task record has no assignee id")
    if int(assignee) != int(expected_assignee):
        return None
    status = _first(flat, ("status", "Status", "taskStatus", "TaskStatus"))
    if status.casefold() in CLOSED_STATUSES:
        return None
    task_id = _digits(_first(flat, ("task_id", "taskId", "TaskId")), "task id")
    applicant_id = _digits(_first(record, ("applicant_id", "applicantId", "ApplicantId")), "applicant id")
    discussion_id = _digits(
        _first(record, ("discussion_id", "discussionId", "DiscussionId")), "discussion id",
    )
    created_raw = _first(record, ("created_at", "created", "createdAt", "Created"))
    created_et = report_created_et(created_raw)
    if created_et is None or not _has_offset(created_raw):
        # Report timestamps are naive Central. An API time must say its zone.
        raise TaskSourceUnavailable("task record has no zoned creation time")
    due = _first(flat, ("due_date", "dueDate", "due", "Due"))
    return AssignedTask(
        task_id=task_id,
        title=_first(record, ("title", "activity_type", "type")) or "Task Note",
        description=_first(record, ("text", "body", "Body", "note", "description")),
        applicant_id=applicant_id,
        applicant_name=_first(record, ("applicant_name", "applicantName", "account_name")),
        assigned_to=ROBIE_ASSIGNEE_NAME,
        due_date=due,
        priority=_first(flat, ("priority", "Priority")),
        created_date=created_et.date().isoformat(),
        status=status or "Open",
        discussion_id=discussion_id,
        last_modified=_first(record, ("last_modified", "lastModified", "modified")) or created_raw,
        created_by=_first(record, ("created_by", "createdBy", "createdByName")),
        assigned_producer=_first(record, ("assigned_producer", "assignedProducer")),
        csr=_first(record, ("csr", "CSR")),
        activity_labels=_labels(record),
        created_at=created_et.isoformat(),
        created_at_et=created_et.isoformat(),
        source=SOURCE_TASK,
    )


def _has_offset(text: str) -> bool:
    value = str(text or "").strip()
    if value.endswith("Z"):
        return True
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _digest(tasks: list[AssignedTask]) -> str:
    rows = sorted(
        (t.task_id, t.applicant_id, t.discussion_id, t.last_modified,
         t.activity_labels, t.status, t.description)
        for t in tasks
    )
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def fetch_api_report(
    client: TaskListClient,
    *,
    assignee: int | None = None,
    now: datetime | None = None,
) -> IngestedReport:
    """Read the listing and return it as a report. Raises TaskSourceUnavailable."""
    who = ROBIE_ASSIGNEE_ID if assignee is None else int(assignee)
    try:
        listing = client.list_assigned_tasks(who)
    except TaskSourceUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - any client failure means use the report
        raise TaskSourceUnavailable(f"listing failed ({type(exc).__name__})") from exc
    if not isinstance(listing, TaskListing) or listing.complete is not True:
        raise TaskSourceUnavailable("listing is not provably complete")
    tasks: list[AssignedTask] = []
    seen: set[str] = set()
    for record in listing.records:
        task = record_to_task(record, who)
        if task is None:
            continue
        if task.task_id in seen:
            raise TaskSourceUnavailable(f"task {task.task_id} listed twice")
        seen.add(task.task_id)
        tasks.append(task)
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    digest = _digest(tasks)
    return IngestedReport(
        message_id=f"{MESSAGE_PREFIX}{digest[:16]}",
        filename=API_FILENAME,
        digest=digest,
        received_at=str(int(moment.timestamp() * 1000)),
        tasks=tuple(tasks),
        row_count=len(listing.records),
        # The health probe reads this as "how fresh is the source". For the API
        # it is the time of this read, not a task's creation time.
        newest_created_et=moment.astimezone(NEW_YORK).isoformat(),
    )


# ------------------------------------------------------------------ live client


class LiveTaskListClient:
    """Read-only listing through the existing Task API login. UNVERIFIED endpoint.

    ``ROBIE_TASK_API_LIST_PATH`` is a path under the Discussion API (for
    example ``v8/...``) that answers ``GET <path>?assignedUserId=<id>`` with a
    JSON list, or an object holding the list under ``tasks``/``notes``/
    ``items``, and ``"complete": false`` or ``"hasMore": true`` when it did not
    return everything. It is unset until EZLynx or a read-only probe
    (``scripts/probe_task_list_endpoint.py``) confirms one, and then this
    client refuses.
    """

    def __init__(self, *, accessor: Any = None, urlopen: Callable[..., Any] | None = None):
        self._accessor = accessor
        self._urlopen = urlopen

    def list_assigned_tasks(self, assignee_id: int) -> TaskListing:
        from . import ezlynx_task_api as api

        path = str(os.environ.get(LIST_PATH_ENV) or "").strip().strip("/")
        if not path:
            raise TaskSourceUnavailable(
                "no verified EZLynx endpoint lists tasks by assignee "
                f"({LIST_PATH_ENV} is not set)"
            )
        if not api.direct_task_api_enabled():
            raise TaskSourceUnavailable("direct Task API is switched off")
        username = api.act_as_username()
        if not username or api.is_vendor_integration_username(username):
            raise TaskSourceUnavailable("Task API act-as user is not set")
        urlopen = self._urlopen or api._default_urlopen
        token, app, reason = api._ensure_token(username, self._accessor, urlopen)
        if not token or app is None:
            raise TaskSourceUnavailable(f"Task API login unavailable ({reason})")
        base = app["discussion_base"].rstrip("/")
        query = parse.urlencode({"assignedUserId": int(assignee_id)})
        req = request.Request(
            f"{base}/{path}?{query}", headers=api._headers(token), method="GET",
        )
        status, body, transport = api._send(req, urlopen, [token])
        if transport:
            raise TaskSourceUnavailable(f"listing request failed ({transport})")
        if status is None or status < 200 or status >= 300:
            raise TaskSourceUnavailable(f"listing request returned HTTP {status}")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise TaskSourceUnavailable("listing was not JSON") from exc
        return _listing_from_response(parsed)


def _listing_from_response(parsed: Any) -> TaskListing:
    if isinstance(parsed, list):
        rows, complete = parsed, True
    elif isinstance(parsed, dict):
        rows = None
        for key in ("tasks", "Tasks", "notes", "Notes", "items", "Items"):
            if isinstance(parsed.get(key), list):
                rows = parsed[key]
                break
        if rows is None:
            raise TaskSourceUnavailable("listing has no task list")
        complete = parsed.get("complete") is not False and parsed.get("hasMore") is not True
        if parsed.get("nextPage") or parsed.get("nextPageToken"):
            complete = False
    else:
        raise TaskSourceUnavailable("listing has an unexpected shape")
    if not all(isinstance(row, dict) for row in rows):
        raise TaskSourceUnavailable("listing holds a row that is not an object")
    return TaskListing(records=tuple(rows), complete=complete)


def fetch_from_api(client: TaskListClient | None = None) -> IngestedReport | None:
    """The API report, or None (after logging why) when the report must be used."""
    try:
        who = assignee_id()
        return fetch_api_report(client or LiveTaskListClient(), assignee=who)
    except TaskSourceUnavailable as exc:
        logger.warning("Task API source unavailable: %s; using the emailed report.", exc)
        return None
