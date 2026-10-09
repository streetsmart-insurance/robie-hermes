"""Freeform "Robie Call" task handler.

When agency staff assign an EZLynx task to "Robie AI" with a freeform call
instruction (e.g. "Please call John about his renewal"), the 5-minute
"Robie AI - Task Check-In" intake reads the Looker CSV of Robie's open
tasks to robie@streetsmart.insurance. The task worker (PR #738) parses that
CSV, acknowledges each task, and routes call tasks here.

INTEGRATION (for the task worker / Job Engine):
    from robie_job_engine.robie_call_handler import (
        RobieCallConfig,
        handle_robie_call_task,
        is_call_task,
    )

    # 1. Route: only call handle_robie_call_task when is_call_task(row) is True.
    # 2. Build ports once (production wiring):
    #      - ApplicantPhonePort: Classic API applicant read
    #        (CellPhone/BusinessPhone/HomePhone priority). See
    #        bland-dispatcher/ezlynx_client.py extract_contact for the
    #        proven field list. May return a plain number string, or a
    #        dict {"phone": str|None, "ambiguous": bool, "candidates":
    #        [{"label": "Cell"}, ...]} when the applicant has several
    #        numbers and no clear best — the handler fails closed.
    #      - BlandCallPort: the Bland transport posts /v1/calls with the
    #        Jake-spec payload (see robie_job_engine.bland_config).
    #        Required get_call_status(call_id) lets the handler VERIFY the
    #        outcome instead of trusting the placement ack; without a
    #        terminal status the handler refuses to mark the task ok —
    #        there is no opt-out.
    #      - CallJobCheckpointPort: durable per-task checkpoint, implemented
    #        by the Job Engine adapter against the engine's job row.
    #        INTERFACE COORDINATION: the Job Engine owns ONE durable job per
    #        EZLynx task (idempotency key "ezlynx-task:<task_id>"). This
    #        handler never creates a job, never runs a scheduler, and never
    #        marks a task complete from an acknowledgment alone. It
    #        checkpoints inside the engine's job under "robie-call:<task_id>"
    #        so a worker restart reconciles (polls Bland for the known
    #        call_ids) instead of dialing again. Without this port the
    #        handler falls back to in-process memory guards (single-process
    #        only) — fine for tests, not for production.
    #      - TaskReassignmentPort: reassign the EZLynx task back to the
    #        original assigner. Optional read_task_assignee(task_id) lets
    #        the handler READ BACK the assignee; reassigned=True is only
    #        ever set on a matching read-back, never on the port's ok alone.
    #      - RecordingUploadPort (optional): upload the Bland MP3 via the
    #        Document API (robie_job_engine.ezlynx_api.upload_applicant_document).
    # 3. DiscussionApiClient: pass a configured
    #    robie_job_engine.ezlynx_discussions.DiscussionApiClient for the
    #    outcome-note writeback (API-only writes per repo rule).
    # 4. Call:
    #        result = handle_robie_call_task(task_dict, config, ports)
    #    The result dict always has "ok" (bool). It NEVER raises.
    #    "ok" is True only when the call outcome was VERIFIED via Bland
    #    status (or dry-run) AND the outcome note was filed in EZLynx.
    #    Reassignment is reported separately via "reassigned" (verified
    #    read-back only).
    #
    # Expected task_dict keys (flexible — CSV headers vary; first match wins):
    #   task_id:       "Task ID" | "TaskID" | "task_id" | "ID"
    #   subject:       "Task Subject" | "Subject" | "subject" | "Title"
    #   description:   "Task Description" | "Description" | "description" | "Notes"
    #   applicant_id:  "Applicant ID" | "ApplicantID" | "applicant_id"
    #   applicant_name:"Account Name" | "Applicant Name" | "applicant_name"
    #   assigned_by:   "Task Created By" | "Assigned By" | "Created By" | "assigned_by"
    #   due_date:      "Task Due Date" | "Due Date" | "due_date"
    #
    # Result dict keys:
    #   ok, task_id, applicant_id, call (Bland result), writeback (note result),
    #   reassigned (bool), recording (upload result or None), error (str|None),
    #   chat_alerted (bool)

RELIABILITY CONTRACT (rock solid):
- Fail-closed everywhere: missing phone, ambiguous phones, kill switch,
  bad input -> no call, task left OPEN, error surfaced in the result dict.
- Idempotent: a task_id processed within IDEMPOTENCY_WINDOW_S returns the
  cached result; a retry can never place a second call. An in-flight claim
  also suppresses concurrent duplicate processing within one process.
- Crash recovery: the Bland call_ids are checkpointed durably (via
  CallJobCheckpointPort, key "robie-call:<task_id>") IMMEDIATELY after the
  dial. A restart finds the call_ids and reconciles (polls Bland for the
  outcome) instead of dialing again.
- Outcome verification: "ok" requires a VERIFIED terminal call status from
  Bland (or dry-run), never the placement ack alone. Unverifiable outcomes
  fail closed: note filed honestly, task left OPEN, no reassignment.
- Reassignment read-back: "reassigned" is True only when the task's
  assignee reads back as the intended user. A port ok without read-back
  (or a mismatch) never counts as reassigned; mismatches fire a chat alert.
- Kill switch: ROBIE_CALL_HALT=1 (env) halts new dials immediately.
- Retries with exponential backoff on transient EZLynx/phone-lookup failures.
  The Bland POST itself is single-attempt (no idempotency key on /v1/calls;
  a retried timeout could double-dial the client). Failed tasks stay OPEN
  and are redelivered on the next intake run.
- Circuit breaker on the Bland port: 5 consecutive failures -> 120s cooldown.
- Call failure -> task stays OPEN, chat alert, NO reassignment (human sees it).
- Writeback failure with a successful call -> IMMEDIATE chat alert
  (silent data loss is the worst outcome).
- Every step logs applicant_id + task_id for traceability.
- DRY_RUN defaults True: nothing dials unless the worker explicitly opts in.
- No scheduler lives here: the Job Engine (or task worker) drives this
  handler per task. One durable job per EZLynx task; this handler
  checkpoints inside it.
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Protocol
from zoneinfo import ZoneInfo

from .business_calendar import us_federal_holidays
from .call_opt_out import pressed_opt_out
from . import call_contact as cc
from .bland_config import (
    CALLBACK_NUMBER,
    CALLBACK_NUMBER_SPOKEN,
    CALLER_ID,
    KAREN_VOICE_ID,
    BlandCallConfig,
    BlandRedialPolicy,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (Jake's specs + Carlo's corrections)
# ---------------------------------------------------------------------------

# Calling window. Outbound dials only, weekdays, America/New_York by default.
# Env overrides the built-in hours when RobieCallConfig leaves them unset.
CALL_WINDOW_START_ENV = "ROBIE_CALL_WINDOW_START"
CALL_WINDOW_END_ENV = "ROBIE_CALL_WINDOW_END"
CALL_WINDOW_TZ_ENV = "ROBIE_CALL_WINDOW_TZ"
DEFAULT_CALL_WINDOW_START = 9   # 9:00 inclusive
DEFAULT_CALL_WINDOW_END = 18    # 18:00 exclusive (6:00 PM)
DEFAULT_CALL_WINDOW_TZ = "America/New_York"


def _resolve_transfer_number(ports: RobieCallPorts,
                             assigned_by: str) -> Optional[str]:
    """The direct-dial number Eva may transfer to, or None.

    Only a wired transfer lookup can supply a number. With no lookup, a
    failed lookup, or an unknown person, there is no transfer target and
    Eva takes a message instead. There is no placeholder number.
    """
    if ports.transfer_lookup is None or not assigned_by:
        return None
    try:
        candidate = ports.transfer_lookup.get_transfer_number(assigned_by)
        normalized = _normalize_phone(candidate) if candidate else None
        if normalized:
            return normalized
    except Exception as exc:  # noqa: BLE001
        logger.warning("transfer lookup failed for %r: %s", assigned_by, exc)
    return None

CALL_KEYWORDS = ("call", "phone", "dial", "ring", "callback", "call back")

PHONE_KEY_PRIORITY = ("CellPhone", "BusinessPhone", "HomePhone", "Phone", "PrimaryPhone")

MAX_INSTRUCTION_CHARS = 2000
MAX_NOTE_CHARS = 4000

RETRYABLE_EXCEPTIONS = (TimeoutError, ConnectionError)

BLAND_CIRCUIT_THRESHOLD = 5
BLAND_CIRCUIT_COOLDOWN_S = 120

IDEMPOTENCY_WINDOW_S = 30 * 60  # 30 minutes, matches the report cadence

KILL_SWITCH_ENV = "ROBIE_CALL_HALT"

# Durable checkpoint key scheme: "robie-call:<task_id>". Lives inside the
# Job Engine's one-durable-job-per-task ("ezlynx-task:<task_id>").
CALL_CHECKPOINT_KEY_PREFIX = "robie-call:"

# Bland statuses that end a call. Anything else (ringing, in-progress,
# queued, ...) is non-terminal and keeps the poll loop waiting.
TERMINAL_CALL_STATUSES = frozenset({
    "completed", "failed", "busy", "no-answer", "canceled", "cancelled",
})

# Terminal statuses that count as a SUCCESSFUL call outcome. A call that
# ended as "failed", "busy", "no-answer", or "canceled" is terminal (it
# ended) but NOT successful — it must not count as task completion.
# "completed" alone is not enough; the caller must verify actual connection
# (answered_by human/voicemail, or positive duration) via _call_was_connected.
SUCCESSFUL_TERMINAL_STATUSES = frozenset({
    "completed",
})


# ---------------------------------------------------------------------------
# Ports (production wiring supplied by the task worker)
# ---------------------------------------------------------------------------

class ApplicantPhonePort(Protocol):
    """Resolve an applicant's dialable phone number."""

    def get_phone(self, applicant_id: str) -> Optional[Any]:
        """Return the best phone number as a string, or None when unknown.

        May also return a dict for ambiguous results:
          {"phone": str|None, "ambiguous": bool,
           "candidates": [{"label": "Cell"}, ...]}
        When "ambiguous" is true the handler fails closed and files a
        clarification note (candidate labels carry no digits — notes must
        never contain phone numbers).
        """
        ...


class CallJobCheckpointPort(Protocol):
    """Durable per-task checkpoint, implemented by the Job Engine adapter.

    The Job Engine owns ONE durable job per EZLynx task (idempotency key
    "ezlynx-task:<task_id>"). The call handler checkpoints inside that job
    under "robie-call:<task_id>" so a worker restart reconciles instead of
    redialing. Stored value shape:
      {"bland_call_ids": [...], "phone": "+1...",
       "completed_at": "<iso>" | None}
    """

    def get_checkpoint(self, key: str) -> Dict[str, Any]:
        """Return the stored checkpoint dict, or {} when none exists."""
        ...

    def set_checkpoint(self, key: str, value: Dict[str, Any]) -> None:
        """Persist the checkpoint dict (overwrite)."""
        ...


class BlandCallPort(Protocol):
    """Place a Bland AI call with Jake's double-dial policy."""

    def place_call_with_double_dial(
        self,
        phone: str,
        task_text: str,
        first_sentence: str,
        voicemail_message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Place the call; apply double-dial internally.

        Returns a dict with at least:
          success (bool), call_ids (list[str]), attempts (list),
          voicemail_hit (bool), redialed (bool), error (str|None),
          recording_url (str|None).
        """
        ...

    def recent_calls(
        self, phone: str, since_seconds: int = 1800
    ) -> Dict[str, Any]:
        """List calls to this phone within the window.

        Used BEFORE dialing: if our system already placed a call to this
        number recently (e.g. a previous attempt timed out after Bland
        accepted it), do NOT dial again. Returns
        {"ok": True, "calls": [{call_id, created_at, metadata, ...}]}
        or {"ok": False, ...}. Implementations may return ok=False when
        the history API is unavailable — the handler then proceeds with
        the normal idempotency guards.
        """
        ...

    # Optional: outcome verification. When present the handler polls it
    # for each placed call_id and only marks the task ok on a VERIFIED
    # terminal status — the placement ack alone is never enough.
    #
    # def get_call_status(self, call_id: str) -> Dict[str, Any]:
    #     """Return {"ok": bool, "status": str, "answered_by": str|None,
    #     "duration_s": float|None, "ended_at": str|None}.
    #     status is terminal ("completed", "failed", "busy", "no-answer",
    #     "canceled") or non-terminal ("in-progress", "ringing", ...).
    #     ok=False when the status is unknown."""


class TaskStatusPort(Protocol):
    """Check whether an EZLynx task is still open (optional)."""

    def is_task_open(self, task_id: str) -> Optional[bool]:
        """True when the task is still open, False when closed/done,
        None when the status can't be determined (handler proceeds)."""
        ...


class TaskReassignmentPort(Protocol):
    """Reassign an EZLynx task back to its original assigner."""

    def reassign_task(
        self, task_id: str, to_user: str, note: str
    ) -> Dict[str, Any]:
        """Return {"ok": bool, "error": str|None}."""
        ...

    # Optional: read-back. When present the handler sets reassigned=True
    # ONLY when the assignee reads back as the intended user — the port's
    # ok alone is never enough.
    #
    # def read_task_assignee(self, task_id: str) -> Optional[str]:
    #     """Return the task's current assignee name, or None when unknown."""


class RecordingUploadPort(Protocol):
    """Upload a call recording via the Document API (optional)."""

    def upload_recording(
        self, applicant_id: str, name: str, recording_url: str
    ) -> Dict[str, Any]:
        """Return {"ok": bool, "document_id": str|None, "error": str|None}."""
        ...


class TransferLookupPort(Protocol):
    """Resolve a staff member's direct-dial number for live call transfers.

    Implemented against the RingCentral account (staff extensions/DIDs).
    When Roby hits trouble on a call — e.g. the other party is getting
    frustrated — Eva transfers the live call to the person who assigned
    the task, so a human takes over instead of the call dying.
    """

    def get_transfer_number(self, assignee_name: str) -> Optional[str]:
        """Return the assignee's direct dial number, or None when unknown."""
        ...


class CheckpointError(Exception):
    """A durable checkpoint read or write failed.

    Never swallowed: a lost or unreadable checkpoint means a restart could
    redial the client or re-post a note, so every checkpoint failure is a
    real task failure (fail closed), never a silent success.
    """


@dataclass
class RobieCallPorts:
    """All external dependencies, injected by the worker."""

    phone_lookup: ApplicantPhonePort
    bland: BlandCallPort
    discussion_client: Any  # robie_job_engine.ezlynx_discussions.DiscussionApiClient
    task_reassign: Optional[TaskReassignmentPort] = None
    recording_upload: Optional[RecordingUploadPort] = None
    chat_alert: Optional[Callable[[str], bool]] = None  # posts to ROBIE health Chat
    task_status: Optional[TaskStatusPort] = None  # skip already-closed tasks
    job_checkpoint: Optional[CallJobCheckpointPort] = None  # durable restart recovery
    transfer_lookup: Optional[TransferLookupPort] = None  # assignee DID for live transfers
    opt_out_store: Optional[Any] = None  # press 6 stops later automated calls
    opt_in_store: Optional[Any] = None  # marketing workflows require a recorded opt-in
    call_dedupe: Optional[Any] = None  # one automated call per note per day
    daily_cap: Optional[Any] = None  # call_pickup.DailyCallCapStore


@dataclass
class RobieCallConfig:
    """Configuration for the freeform call handler."""

    dry_run: bool = True  # Fail-closed default: nothing dials unless opted in
    health_chat_webhook_url: Optional[str] = None
    redial_delay_seconds: int = 10
    max_attempts: int = 2
    request_timeout_s: int = 30
    # Outcome verification: the task is ok only on a VERIFIED terminal Bland
    # status, never on the placement ack alone. There is no opt-out: an
    # HTTP 200 from Bland (or Zapier) proves the request was accepted, not
    # that the call happened. When Bland cannot confirm a terminal status,
    # the task fails closed (open, alerted, honest note).
    outcome_poll_tries: int = 6
    outcome_poll_interval_s: int = 10
    # None means "use the env override, else the built-in default".
    calling_window_start_hour: Optional[int] = None
    calling_window_end_hour: Optional[int] = None
    calling_window_tz: Optional[str] = None
    # Test seam. Production leaves this unset and uses the real clock.
    now: Optional[datetime] = None
    # Press 2 is offered only when this is true and the number is mobile.
    sms_configured: bool = False


# ---------------------------------------------------------------------------
# Module state (per worker process; alerts are best-effort signals)
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_processed_tasks: Dict[str, float] = {}  # task_id -> completed_at epoch
# Content dedup: (applicant_id, normalized_instruction) -> completed_at.
# Catches the sloppy-human case of the same instruction assigned twice as
# two different tasks — task_id idempotency alone would dial twice.
_processed_content: Dict[tuple, float] = {}
# In-flight claims: task_ids currently being processed in this process.
# Two overlapping runs (5-minute intake + manual trigger) must not dial twice.
_inflight_tasks: set = set()
_bland_failures: int = 0
_bland_circuit_open_until: float = 0.0


def _reset_module_state_for_tests() -> None:
    """Test seam only: clear idempotency + circuit state."""
    global _processed_tasks, _processed_content
    global _bland_failures, _bland_circuit_open_until
    global _STREETSMART_NUMBERS, _STREETSMART_NUMBERS_LOADED
    _STREETSMART_NUMBERS = None
    _STREETSMART_NUMBERS_LOADED = False
    with _state_lock:
        _processed_tasks = {}
        _processed_content = {}
        _inflight_tasks.clear()
        _bland_failures = 0
        _bland_circuit_open_until = 0.0


def _claim_inflight(task_id: str) -> bool:
    """Claim a task for processing. False when already in flight."""
    with _state_lock:
        if task_id in _inflight_tasks:
            return False
        _inflight_tasks.add(task_id)
        return True


def _release_inflight(task_id: str) -> None:
    with _state_lock:
        _inflight_tasks.discard(task_id)


# ---------------------------------------------------------------------------
# Task parsing
# ---------------------------------------------------------------------------

_TASK_FIELD_ALIASES: Dict[str, List[str]] = {
    "task_id": ["Task ID", "TaskID", "task_id", "ID", "Id"],
    "subject": ["Task Subject", "Subject", "subject", "Title", "title"],
    "description": ["Task Description", "Description", "description", "Notes", "notes", "Task Notes"],
    "applicant_id": ["Applicant ID", "ApplicantID", "applicant_id", "Applicant Id"],
    "applicant_name": ["Account Name", "Applicant Name", "applicant_name", "Client Name"],
    "assigned_by": ["Task Created By", "Assigned By", "Created By", "assigned_by", "CreatedBy"],
    "assigned_to": ["Assigned To", "AssignedTo", "assigned_to", "Task Assigned To"],
    "assigned_producer": [
        "Assigned Producer", "assigned_producer",
        "Applicant Data Assigned Producer",
    ],
    "due_date": ["Task Due Date", "Due Date", "due_date"],
    "activity_labels": ["Activity Labels", "activity_labels"],
    "workflow": ["Workflow", "workflow"],
    "discussion_id": ["Discussion ID", "discussion_id", "DiscussionID"],
    "phone_is_mobile": ["Phone Is Mobile", "phone_is_mobile"],
    "created_date": [
        "Created Date", "created_at", "created_at_et", "created_date",
        "Task Created Date",
    ],
}


def _pick(task: Dict[str, Any], logical: str) -> str:
    for key in _TASK_FIELD_ALIASES[logical]:
        if key in task and task[key] not in (None, ""):
            return str(task[key]).strip()
    return ""


def _is_label_request(task: Dict[str, Any]) -> bool:
    """True for a labeled note (not a task assigned to Robie AI)."""
    return str(task.get("Source") or task.get("source") or "").strip() == "label"


def _request_word(task: Dict[str, Any]) -> str:
    return "note" if _is_label_request(task) else "task"


def _redo_hint(task: Dict[str, Any], *, lower: bool = False) -> str:
    """Plain-English 'how to ask again' for this kind of request."""
    labels = _pick(task, "activity_labels") or "Robie"
    label = labels.split(",")[0].strip() or "Robie"
    if _is_label_request(task):
        text = f"Add a new note with the {label} label"
    else:
        text = f"Make a new {label} task"
    return (text[0].lower() + text[1:]) if lower else text


def is_call_task(task: Dict[str, Any]) -> bool:
    """True when an Activity Label is a live call label or an enabled Splice label.

    Title and description text never start a call. "Do not call" and
    "[CALLBACK REQUIRED]" are not call tasks. A short name such as
    "Robie audit" is not a label. The nine full Splice labels are call
    tasks only while splice_workflows_enabled is true.
    """
    from .call_pickup import classify_call_request

    decision = classify_call_request(_pick(task, "activity_labels"), "")
    return decision.action in ("workflow", "freeform")


def _extract_instruction(task: Dict[str, Any]) -> str:
    """The freeform instruction Eva follows: the task description, verbatim."""
    instruction = _pick(task, "description") or _pick(task, "subject")
    return instruction[:MAX_INSTRUCTION_CHARS].strip()


# Words that signal the instruction says WHAT the call is about.
_TOPIC_SIGNALS = re.compile(
    r"\b(about|regarding|regards|re:|for|to discuss|discuss|follow.?up on|"
    r"concerning|renewal|payment|claim|policy|policies|document|documents|"
    r"billing|quote|audit|signature|esign|appointment|reminder)\b",
    re.IGNORECASE,
)

# Bare pronouns/references with no topic — never enough to place a call on.
_VAGUE_ONLY_RE = re.compile(
    r"^(please\s+|kindly\s+|can you\s+|could you\s+)?"
    r"(call|phone|dial|ring|contact|reach)(\s+(him|her|them|it|the client|"
    r"the customer|the insured))?[\s.!]*$",
    re.IGNORECASE,
)

_CALL_VERBS_RE = re.compile(
    r"\b(please\s+)?(call|phone|dial|ring|contact|reach)\b", re.IGNORECASE)


def _instruction_ambiguity(instruction: str) -> Optional[str]:
    """Return a plain-English reason when the instruction is too vague to act on.

    Carlo's rule: if the instruction is ambiguous ("call him"), do NOT guess —
    leave the task open with a note asking for clarification. Returns None
    when the instruction is actionable.
    """
    text = (instruction or "").strip()
    if not text:
        return "the task has no instruction text"
    # Strip call verbs to see what's left.
    remainder = _CALL_VERBS_RE.sub("", text).strip(" .!,:;")
    if not remainder:
        return "the task only says to call, with no reason given"
    if _VAGUE_ONLY_RE.match(text):
        return (f"the task only says {text.strip()!r} — it doesn't say "
                "what the call is about or who to ask for")
    # Remainder exists but names no topic: "call John" (no reason),
    # "call him ASAP" (pronoun, no topic).
    if len(remainder) < 12 and not _TOPIC_SIGNALS.search(text):
        return (f"the instruction {text.strip()!r} doesn't say what "
                "the call is about")
    return None


def _test_intake_skips_applicant_lookup(ports: RobieCallPorts) -> bool:
    """True only for the Test intake wiring that has no applicant phone.

    The regression battery sets ROBIE_ENV=TEST for synthetic runs. A fake
    phone port in those tests must still be called. The skip is the no-op
    reader installed by build_call_dependencies on Test.
    """
    from .bland_prod_wiring import _no_applicant_phone
    from .call_pickup import is_test_server

    if not is_test_server():
        return False
    lookup = getattr(ports, "phone_lookup", None)
    return getattr(lookup, "_fetch", None) is _no_applicant_phone


def _phone_is_mobile(ports: RobieCallPorts, task: Dict[str, Any], applicant_id: str) -> bool:
    flag = _pick(task, "phone_is_mobile").lower()
    if flag in ("1", "true", "yes"):
        return True
    from .call_pickup import is_test_server

    if is_test_server():
        return False
    method = getattr(ports.phone_lookup, "is_mobile", None)
    if method is None:
        return False
    try:
        return bool(method(applicant_id))
    except Exception:  # noqa: BLE001 — no text offer when the lookup fails
        return False


def _normalize_spoken(text: str) -> str:
    """Normalize text that will be SPOKEN by TTS (first_sentence, voicemail).

    ALL CAPS input makes TTS shout or spell words out. Collapse whitespace;
    if the text is mostly uppercase, convert to sentence case. The Eva task
    prompt keeps the original (she's an LLM and handles it fine).
    """
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return collapsed
    letters = [c for c in collapsed if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.8:
        return collapsed.capitalize()
    return collapsed


# Broad scrubber: DiscussionApi rejects digit runs that look like phones,
# including a bare 10-digit policy number. Dialing uses a stricter rule.
_PHONE_LIKE_RE = re.compile(
    r"(\+?1?[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4})")

# A phone is formatted when the groups are separated (732-555-0142,
# (732) 555-0142, 1-800-776-4737). A bare digit run is not.
_PHONE_FORMATTED_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s\-.]?)?(?:\(\d{3}\)[\s\-.]?\d{3}[\s\-.]\d{4}"
    r"|\d{3}[\s\-.]\d{3}[\s\-.]\d{4})(?!\d)"
)
_BARE_DIGIT_RUN_RE = re.compile(r"(?<!\d)\d{6,15}(?!\d)")
# "policy 7685786571", "pol #123", "claim: 999", "quote number 1234567890".
_POLICY_CONTEXT_RE = re.compile(
    r"(?i)(?:\b(?:policy|pol|claim|quote)\b"
    r"(?:\s*(?:number|no|num|#))?\s*[:#.\-]?\s*)$"
)
# Clear phone wording. "number" alone does not win when a policy/claim/quote
# word already claimed the same digits.
_PHONE_WORDING_RE = re.compile(
    r"(?i)(?:\b(?:phone|cell|cellphone|mobile|telephone)\b|\bnumber\b"
    r"|\bcall\b(?:\s+\S+){0,6}\s+\bat\b)\s*[:#.\-]?\s*$"
)
# Alphanumeric policy tokens (WC5-33S-B276B9-026). Never a phone.
_POLICY_ALNUM_RE = re.compile(
    r"\b(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z][A-Za-z0-9-]{4,30}\b"
)
_NAMED_CALLEE_RE = re.compile(
    r"\b[Cc]all\s+([A-Z][A-Za-z][A-Za-z'&.-]*"
    r"(?:\s+[A-Z][A-Za-z][A-Za-z'&.-]*)*)"
)


# Wording that introduces a number we ask the OTHER person to call, not the
# number Robie should dial: "If voicemail: Ask him to call <office line>."
# A number after this wording in the same line or sentence is never dialed
# (task 63558413, Oct 8 2026: Robie dialed Jake's office line from the
# voicemail line of his own test task). A plain dial request that says
# "call back" ("Call back Mrs. Smith at ...", "Please call back <number>")
# is NOT callback wording and still dials.
_CALLBACK_CONTEXT_RE = re.compile(
    r"(?i)(?:"
    # "If voicemail: ...", "if you get his voicemail, ...", "if no answer ..."
    r"\bif\s+(?:you\s+(?:get|reach|hit)\s+)?(?:(?:a|the|his|her|their)\s+)?"
    r"(?:voice\s*-?\s*mail|vm|no\s+answer|no\s+one\s+answers)\b"
    # "leave a message ...", "leave a voicemail ..."
    r"|\bleave\s+(?:(?:a|the)\s+)?(?:message|msg|voice\s*-?\s*mail|vm)\b"
    # "ask/tell/have him|her|them to call (us/back) ..."
    r"|\b(?:ask|tell|have)\s+(?:him|her|them|the\s+client|the\s+customer|"
    r"the\s+insured)\s+(?:to\s+)?(?:call|ring|phone|reach)\b"
    # "call us back at ..."
    r"|\bcall\s+us\s+back\b"
    # "callback number ...", "call-back #", "call back no."
    r"|\bcall\s*-?\s*back\s*(?:number|num|no\b|#|line|phone)"
    r")"
)
# Where a clause starts: a new line, or the end of a sentence.
_CLAUSE_BREAK_RE = re.compile(r"\n|[.!?;](?=\s|$)")

# The agency main line. Robie never dials a StreetSmart number.
STREETSMART_MAIN_LINE = CALLBACK_NUMBER
_STREETSMART_NUMBERS: Optional[frozenset] = None
_STREETSMART_NUMBERS_LOADED = False


def _last10(raw: Any) -> str:
    return "".join(c for c in str(raw or "") if c.isdigit())[-10:]


def _streetsmart_numbers() -> Optional[frozenset]:
    """Last-10-digit StreetSmart numbers: every staff DID, the main line, and
    the outbound caller ID. None when the staff directory cannot be read."""
    global _STREETSMART_NUMBERS, _STREETSMART_NUMBERS_LOADED
    if _STREETSMART_NUMBERS_LOADED:
        return _STREETSMART_NUMBERS
    numbers = {_last10(STREETSMART_MAIN_LINE), _last10(CALLER_ID)}
    try:
        from .ringcentral_transfer_lookup import load_staff_directory

        for row in load_staff_directory().values():
            did = _last10(row.get("did"))
            if len(did) == 10:
                numbers.add(did)
        _STREETSMART_NUMBERS = frozenset(n for n in numbers if len(n) == 10)
    except Exception as exc:  # noqa: BLE001 — fail closed below
        logger.error("staff phone directory unreadable; refusing every typed "
                     "number: %s", exc)
        _STREETSMART_NUMBERS = None
    _STREETSMART_NUMBERS_LOADED = True
    return _STREETSMART_NUMBERS


def _is_streetsmart_number(raw: Any) -> bool:
    """True for a StreetSmart number. Fails closed: an unreadable staff
    directory treats every number as StreetSmart, so nothing is dialed."""
    numbers = _streetsmart_numbers()
    if numbers is None:
        return True
    return _last10(raw) in numbers


def _in_callback_context(text: str, start: int) -> bool:
    """True when callback/voicemail wording comes before ``start`` in the
    same line or sentence."""
    before = text[:start]
    breaks = list(_CLAUSE_BREAK_RE.finditer(before))
    clause = before[breaks[-1].end():] if breaks else before
    return bool(_CALLBACK_CONTEXT_RE.search(clause))


def _never_dial(text: str, start: int, raw: str) -> bool:
    """A number Robie must not dial: a callback/voicemail number, or any
    StreetSmart number."""
    return _in_callback_context(text, start) or _is_streetsmart_number(raw)


def _phones_in_text(text: str) -> List[str]:
    """Digit runs that are clearly phones, not policy/claim/quote numbers."""
    return ["".join(c for c in raw if c.isdigit())
            for raw in _clear_phone_texts(text or "")]


def _context_before(text: str, start: int, window: int = 80) -> str:
    return text[max(0, start - window):start]


def _overlaps(start: int, end: int, spans: List[tuple]) -> bool:
    return any(not (end <= left or start >= right) for left, right in spans)


def _is_known_policy_shape(digits: str) -> bool:
    """True for shapes this agency treats as policy numbers, not phones.

    Bare 6–15 digit runs and 11-digit runs that are not a leading-1
    national number. A formatted phone (separators) is not this shape.
    A bare 10-digit run, and a bare 11-digit run starting with 1, match
    both a policy number and a phone — those stay ambiguous unless the
    request clearly introduces them as a phone.
    """
    if not digits.isdigit():
        return True
    if len(digits) == 10:
        return False
    if len(digits) == 11 and digits.startswith("1"):
        return False
    return True


def _policy_token_spans(text: str) -> List[tuple]:
    """Spans of alphanumeric policy tokens. Digits inside them are not phones."""
    return [(m.start(), m.end()) for m in _POLICY_ALNUM_RE.finditer(text or "")]


def _clear_phone_matches(text: str) -> List[tuple]:
    """(start, raw) for spans the request clearly gives as a phone number,
    in text order, after #814's exclusions (callback/voicemail wording and
    StreetSmart numbers)."""
    found: List[tuple] = []
    policy_spans = _policy_token_spans(text or "")
    formatted = list(_PHONE_FORMATTED_RE.finditer(text or ""))
    spans = [(m.start(), m.end()) for m in formatted]
    for match in formatted:
        if _overlaps(match.start(), match.end(), policy_spans):
            continue
        if _POLICY_CONTEXT_RE.search(_context_before(text, match.start())):
            continue
        if _never_dial(text, match.start(), match.group(0)):
            continue
        found.append((match.start(), match.group(0)))
    for match in _BARE_DIGIT_RUN_RE.finditer(text or ""):
        if _overlaps(match.start(), match.end(), spans):
            continue
        if _overlaps(match.start(), match.end(), policy_spans):
            continue
        if _POLICY_CONTEXT_RE.search(_context_before(text, match.start())):
            continue
        digits = match.group(0)
        if _is_known_policy_shape(digits):
            continue
        if _never_dial(text, match.start(), digits):
            continue
        if _PHONE_WORDING_RE.search(_context_before(text, match.start())):
            found.append((match.start(), digits))
    found.sort(key=lambda item: item[0])
    return found


def _clear_phone_texts(text: str) -> List[str]:
    """Raw spans the request clearly gives as a phone number."""
    return [raw for _, raw in _clear_phone_matches(text)]


# A number explicitly labeled as the one to call: "phone", "cell", "mobile",
# "number", "at", "call <someone> at", "home"/"work"/"office" (a label too,
# so two labeled numbers still ask). Only the text right before the number
# on the same line counts.
_DIAL_LABEL_RE = re.compile(
    r"(?i)(?:\b(?:phone|cell|cellphone|cell\s+phone|mobile|telephone|tel|"
    r"home|work|office|landline|number)\b\.?(?:\s+(?:is|number|#|no\.?))?"
    r"|\bat\b|\bcall\b(?:\s+\S+){0,6}\s+\bat\b)"
    r"\s*[:#.\-]?\s*(?:\+?1[\s.\-]?)?\(?$"
)

# Why a Robie Call did not get one number to dial.
AMBIGUOUS_DIGITS = "ambiguous_digits"
MULTIPLE_NUMBERS = "multiple_numbers"


def _line_before(text: str, start: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    return text[line_start:start]


def _first_line_span(text: str) -> tuple:
    """(start, end) of the first line that has any text."""
    pos = 0
    for line in text.split("\n"):
        if line.strip():
            return pos, pos + len(line)
        pos += len(line) + 1
    return 0, 0


# A first line that is only a phone number: "7326688161", "+1 7326688161",
# "17326688161". Area code and exchange must start 2-9 (a dialable NANP
# number).
_BARE_FIRST_LINE_RE = re.compile(r"^\s*(\+?1[\s.\-]?)?([2-9]\d{2}[2-9]\d{6})\s*[.,;]?\s*$")


def _bare_first_line_phone(text: str) -> Optional[tuple]:
    """(start, digits) when the first line of the description is a bare
    phone number and nothing else. Robie Call treats that as the number to
    dial (Jake writes his test tasks this way, task 63558413). A bare
    10-digit run anywhere else still asks: it could be a policy number.
    #814's exclusions still apply (a StreetSmart number is never dialed)."""
    first_start, first_end = _first_line_span(text or "")
    if first_end <= first_start:
        return None
    match = _BARE_FIRST_LINE_RE.match(text[first_start:first_end])
    if not match:
        return None
    digits = "".join(c for c in match.group(0) if c.isdigit())
    if _never_dial(text, first_start + match.start(2), digits):
        return None
    return first_start + match.start(2), digits


def _typed_phone_choice(instruction: str) -> tuple:
    """(normalized phone or None, why-not or None, how it was chosen).

    One distinct typed number: dial it. Two or more distinct typed numbers
    (after #814's callback/voicemail and StreetSmart exclusions): dial only
    the one that clearly wins, the one on the first line or the one
    explicitly labeled ("phone", "cell", "at", "call X at"). A first line
    that is only a phone number counts as typed. If none wins,
    or more than one does, do not guess: why-not is MULTIPLE_NUMBERS (the
    same rule as the Cloud Run Robie Call hard block, #811). A bare 10-digit
    run with no phone wording anywhere but alone on the first line stays
    AMBIGUOUS_DIGITS: it could be a policy number.
    """
    text = instruction or ""
    matches = _clear_phone_matches(text)
    bare_first = _bare_first_line_phone(text)
    if bare_first is not None and all(start != bare_first[0] for start, _ in matches):
        matches = sorted(matches + [bare_first], key=lambda item: item[0])
    distinct: List[str] = []
    for _, raw in matches:
        phone = _normalize_phone("".join(c for c in raw if c.isdigit()))
        if phone and phone not in distinct:
            distinct.append(phone)
    if len(distinct) == 1:
        return distinct[0], None, "only"
    if len(distinct) > 1:
        first_start, first_end = _first_line_span(text)
        first_line: List[str] = []
        labeled: List[str] = []
        for start, raw in matches:
            phone = _normalize_phone("".join(c for c in raw if c.isdigit()))
            if not phone:
                continue
            if first_start <= start < first_end and phone not in first_line:
                first_line.append(phone)
            if _DIAL_LABEL_RE.search(_line_before(text, start)) and phone not in labeled:
                labeled.append(phone)
        winners = list(dict.fromkeys(first_line + labeled))
        if len(winners) == 1:
            how = "first_line" if winners[0] in first_line else "labeled"
            return winners[0], None, how
        return None, MULTIPLE_NUMBERS, ""
    formatted = list(_PHONE_FORMATTED_RE.finditer(text))
    spans = [(m.start(), m.end()) for m in formatted] + _policy_token_spans(text)
    for match in _BARE_DIGIT_RUN_RE.finditer(text):
        if _overlaps(match.start(), match.end(), spans):
            continue
        if _POLICY_CONTEXT_RE.search(_context_before(text, match.start())):
            continue
        digits = match.group(0)
        if _is_known_policy_shape(digits):
            continue
        if _never_dial(text, match.start(), digits):
            continue
        # Bare 10-digit (or leading-1) run, no phone wording: could be
        # a policy number. Ask. Do not dial it.
        return None, AMBIGUOUS_DIGITS, ""
    return None, None, ""


def _phone_directive(instruction: str) -> tuple:
    """(normalized phone or None, ambiguous).

    Digits are a phone only when the request clearly gives one: phone,
    cell, number, or "call at" wording, or a phone-formatted number.
    A number we ask the other person to call ("if voicemail ...", "ask
    him to call us back ...", "call us back at", "callback number",
    "leave a message ...") is not a dial target and is skipped. A plain
    "call back <person> at <number>" request still dials. A StreetSmart
    number (staff direct dials, main line, caller ID) is never dialed,
    however it is worded.
    Digits after policy/pol/claim/quote are never a phone. A known policy
    shape is never a phone. A bare 10-digit run with none of those signals
    is ambiguous — the caller must ask, not dial. Two or more typed
    numbers with no clear winner (first line or labeled) are ambiguous
    too; see _typed_phone_choice.
    """
    phone, why_not, _ = _typed_phone_choice(instruction)
    return phone, why_not is not None


def _extract_explicit_phone(instruction: str) -> Optional[str]:
    """The task's phone number when the request clearly gives one.

    Policy, claim, and quote numbers are not phones. An ambiguous digit
    run returns None; callers must check `_phone_directive` and ask
    instead of dialing.
    """
    phone, ambiguous = _phone_directive(instruction)
    if ambiguous:
        return None
    return phone


def _scrub_phones(text: str) -> str:
    """Replace phone-like values with [phone number] (DiscussionApi rejects digits)."""
    return _PHONE_LIKE_RE.sub("[phone number]", text or "")


def _instruction_phone_mismatch(instruction: str, dialed_phone: str) -> Optional[str]:
    """Detect when the task text names a different phone than the one dialed.

    Only numbers the request clearly gives as a phone count. A policy,
    claim, or quote number is not a mismatch.
    """
    dialed_digits = "".join(c for c in str(dialed_phone or "") if c.isdigit())
    for found in _phones_in_text(instruction):
        # Compare on last 10 digits (formatting/country-code differences).
        if (found and dialed_digits and len(found) >= 10
                and len(dialed_digits) >= 10
                and found[-10:] != dialed_digits[-10:]):
            return found
    return None


def _named_callee(instruction: str) -> str:
    """The capitalized party after "call", if the instruction names one."""
    match = _NAMED_CALLEE_RE.search(instruction or "")
    return match.group(1).strip() if match else ""


def _name_overlaps(left: str, right: str) -> bool:
    left_tokens = {part for part in left.lower().split() if part}
    right_tokens = {part for part in right.lower().split() if part}
    return bool(left_tokens and right_tokens and (left_tokens & right_tokens))


def _instruction_name_mismatch(instruction: str,
                               applicant_name: str) -> Optional[str]:
    """Detect when the task names a person who isn't the applicant.

    Looks for "call <Name>" patterns and compares against the applicant
    name. Returns the mismatched name, or None. This is best-effort —
    only flags clear two-word capitalized names that don't overlap with
    the applicant's name tokens.
    """
    if not applicant_name or applicant_name == "the client":
        return None
    # Case-sensitive on the name itself: "Call Mary Smith" matches, but
    # "call about renewal" does not (lowercase words aren't names).
    m = re.search(r"\b[Cc][Aa][Ll][Ll]\s+([A-Z][A-Za-z]*\s+[A-Z][A-Za-z]*)\b",
                  instruction or "")
    if not m:
        return None
    mentioned = m.group(1)
    applicant_tokens = set(applicant_name.lower().split())
    mentioned_tokens = set(mentioned.lower().split())
    if mentioned_tokens & applicant_tokens:
        return None  # overlaps (e.g. "call John" for "John Smith")
    return mentioned

# ---------------------------------------------------------------------------
# Helpers: phone, retries, circuit breaker, chat
# ---------------------------------------------------------------------------

def _normalize_phone(raw: Any) -> Optional[str]:
    """Best-effort E.164-ish normalization. None when undialable."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) < 7:
        return None
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) > 7:
        return "+" + digits
    return None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _format_hour(hour: int) -> str:
    """9 -> '9:00 AM', 18 -> '6:00 PM'."""
    shown = hour % 12 or 12
    suffix = "AM" if hour < 12 else "PM"
    return f"{shown}:00 {suffix}"


def _calling_window(config: RobieCallConfig) -> tuple:
    """(start_hour, end_hour, ZoneInfo or None, error).

    start is inclusive, end is exclusive. error is set when the window
    cannot be applied safely — the caller must not dial.
    """
    start = (config.calling_window_start_hour
             if config.calling_window_start_hour is not None
             else _env_int(CALL_WINDOW_START_ENV, DEFAULT_CALL_WINDOW_START))
    end = (config.calling_window_end_hour
           if config.calling_window_end_hour is not None
           else _env_int(CALL_WINDOW_END_ENV, DEFAULT_CALL_WINDOW_END))
    tz_name = (config.calling_window_tz
               or os.environ.get(CALL_WINDOW_TZ_ENV, "").strip()
               or DEFAULT_CALL_WINDOW_TZ)
    if end <= start or not (0 <= start <= 23) or not (1 <= end <= 24):
        return start, end, None, "calling window hours are not usable"
    try:
        zone = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - bad tz must not dial
        return start, end, None, f"calling window timezone {tz_name!r} is not valid"
    return start, end, zone, None


def _calling_now(config: Optional[RobieCallConfig] = None) -> datetime:
    """Clock for the calling window. Tests pass config.now."""
    if config is not None and config.now is not None:
        return config.now
    _start, _end, zone, _err = _calling_window(config or RobieCallConfig())
    if zone is None:
        return datetime.now(ZoneInfo(DEFAULT_CALL_WINDOW_TZ))
    return datetime.now(zone)


def _outside_calling_window(config: RobieCallConfig) -> Optional[str]:
    """Plain-English reason when an outbound call must not be dialed.

    Weekdays 9:00 AM–6:00 PM America/New_York by default. Configurable
    via RobieCallConfig or ROBIE_CALL_WINDOW_START / _END / _TZ.
    """
    start, end, zone, error = _calling_window(config)
    if error or zone is None:
        return error or "calling window is not configured"
    current = _calling_now(config)
    if current.tzinfo is None:
        current = current.replace(tzinfo=zone)
    else:
        current = current.astimezone(zone)
    if current.weekday() >= 5:
        return (
            f"weekend; outbound calls run weekdays "
            f"{_format_hour(start)}–{_format_hour(end)} {zone.key}"
        )
    if current.date() in us_federal_holidays(current.year):
        return (
            f"federal holiday; outbound calls run weekdays "
            f"{_format_hour(start)}–{_format_hour(end)} {zone.key}"
        )
    minute_of_day = current.hour * 60 + current.minute
    if minute_of_day < start * 60 or minute_of_day >= end * 60:
        return (
            f"outside {_format_hour(start)}–{_format_hour(end)} {zone.key}"
        )
    return None


def _calling_window_label(config: RobieCallConfig) -> str:
    start, end, zone, error = _calling_window(config)
    tz = zone.key if zone is not None else DEFAULT_CALL_WINDOW_TZ
    if error:
        return error
    return f"weekdays {_format_hour(start)}–{_format_hour(end)} {tz}"


def _retryable_call(fn: Callable[[], Any], *, what: str, tries: int = 3) -> Any:
    """Retry transient failures with exponential backoff. Raises the last error."""
    last: Optional[BaseException] = None
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except RETRYABLE_EXCEPTIONS as exc:
            last = exc
            logger.warning("%s transient failure (attempt %d/%d): %s", what, attempt, tries, exc)
            time.sleep(min(2 ** (attempt - 1), 8))
        except Exception:
            raise
    assert last is not None
    raise last


def _bland_circuit_allows() -> bool:
    with _state_lock:
        return time.time() >= _bland_circuit_open_until


def _bland_record(success: bool) -> Optional[bool]:
    """Track Bland outcomes. Returns True when this failure TRIPPED the breaker."""
    global _bland_failures, _bland_circuit_open_until
    tripped = False
    with _state_lock:
        if success:
            _bland_failures = 0
        else:
            _bland_failures += 1
            if _bland_failures >= BLAND_CIRCUIT_THRESHOLD:
                _bland_circuit_open_until = time.time() + BLAND_CIRCUIT_COOLDOWN_S
                _bland_failures = 0
                tripped = True
    return tripped


def _chat_alert(ports: RobieCallPorts, config: RobieCallConfig, text: str) -> bool:
    """Best-effort chat alert. Never raises."""
    try:
        if ports.chat_alert is not None:
            return bool(ports.chat_alert(text))
        if config.health_chat_webhook_url:
            from .staff_jobs_common import post_chat_webhook

            return bool(post_chat_webhook(text, webhook_url=config.health_chat_webhook_url))
    except Exception as exc:  # noqa: BLE001 - alerting must never break the flow
        logger.warning("chat alert failed: %s", exc)
    return False


def _kill_switch_active() -> bool:
    import os

    return os.environ.get(KILL_SWITCH_ENV, "").strip().lower() in ("1", "true", "yes")


def _normalize_instruction_key(instruction: str) -> str:
    """Canonical form for content dedup: lowercase, no punctuation/extra space."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", (instruction or "").lower())).strip()


def _content_already_processed(applicant_id: str, instruction: str) -> bool:
    key = (applicant_id, _normalize_instruction_key(instruction))
    with _state_lock:
        ts = _processed_content.get(key)
        if ts is None:
            return False
        if time.time() - ts > IDEMPOTENCY_WINDOW_S:
            del _processed_content[key]
            return False
        return True


def _mark_content_processed(applicant_id: str, instruction: str) -> None:
    key = (applicant_id, _normalize_instruction_key(instruction))
    with _state_lock:
        now = time.time()
        expired = [k for k, v in _processed_content.items()
                   if now - v > IDEMPOTENCY_WINDOW_S]
        for k in expired:
            del _processed_content[k]
        _processed_content[key] = now


def _mark_processed(task_id: str) -> None:
    with _state_lock:
        now = time.time()
        # prune expired entries
        expired = [k for k, v in _processed_tasks.items() if now - v > IDEMPOTENCY_WINDOW_S]
        for k in expired:
            del _processed_tasks[k]
        _processed_tasks[task_id] = now


def _already_processed(task_id: str) -> bool:
    with _state_lock:
        ts = _processed_tasks.get(task_id)
        if ts is None:
            return False
        if time.time() - ts > IDEMPOTENCY_WINDOW_S:
            del _processed_tasks[task_id]
            return False
        return True


# ---------------------------------------------------------------------------
# Durable checkpoint (crash recovery without redialing)
# ---------------------------------------------------------------------------

def _checkpoint_key(task_id: str) -> str:
    return f"{CALL_CHECKPOINT_KEY_PREFIX}{task_id}"


def _load_checkpoint(ports: RobieCallPorts, task_id: str,
                     *, strict: bool = False) -> Dict[str, Any]:
    """Read the durable checkpoint.

    Returns {} when the port is absent. When the port is present but the
    read fails: strict=False logs and returns {} (legacy best-effort);
    strict=True raises CheckpointError. The pre-dial recovery check uses
    strict=True — an unreadable checkpoint must fail closed, because the
    guards against redialing (completed_at, call_ids, dial_intent) would
    otherwise be blind.
    """
    if ports.job_checkpoint is None:
        return {}
    try:
        data = ports.job_checkpoint.get_checkpoint(_checkpoint_key(task_id))
        return dict(data or {})
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise CheckpointError(
                f"checkpoint read failed for task {task_id}: {exc}") from exc
        logger.warning("checkpoint read failed for %s: %s", task_id, exc)
        return {}


def _save_checkpoint(ports: RobieCallPorts, task_id: str, value: Dict[str, Any]) -> None:
    """Persist the checkpoint. Raises CheckpointError on failure — never
    swallows. A failed write must fail the task, never silently continue:
    the next run would see no intent/call_ids and could dial the client
    a second time.
    """
    if ports.job_checkpoint is None:
        return
    try:
        ports.job_checkpoint.set_checkpoint(_checkpoint_key(task_id), dict(value))
    except Exception as exc:  # noqa: BLE001
        logger.error("checkpoint WRITE failed for %s: %s", task_id, exc)
        raise CheckpointError(
            f"checkpoint write failed for task {task_id}: {exc}") from exc


def _save_checkpoint_retrying(ports: RobieCallPorts, task_id: str,
                              value: Dict[str, Any], tries: int = 3,
                              backoff_s: float = 0.5) -> None:
    """Retry transient checkpoint write failures, then raise CheckpointError.

    "Fail or retry, never success": a checkpoint write that fails after
    retries is a real task failure, surfaced to the caller.
    """
    last: Optional[Exception] = None
    for attempt in range(1, tries + 1):
        try:
            _save_checkpoint(ports, task_id, value)
            return
        except CheckpointError as exc:
            last = exc
            if attempt < tries:
                time.sleep(backoff_s * attempt)
    raise CheckpointError(
        f"checkpoint write failed {tries}x for task {task_id}: {last}")


def _resolve_phone(raw: Any) -> tuple:
    """Normalize the phone port's answer.

    Returns (phone, ambiguity). phone is the dialable string or None;
    ambiguity is None when clear, otherwise {"candidates": [...]}.
    """
    if isinstance(raw, dict):
        if raw.get("ambiguous"):
            return None, {"candidates": list(raw.get("candidates") or [])}
        return _normalize_phone(raw.get("phone")), None
    return _normalize_phone(raw), None


# ---------------------------------------------------------------------------
# Outcome verification (never trust the placement ack alone)
# ---------------------------------------------------------------------------

def _poll_call_status(
    bland_port: Any, call_id: str, config: RobieCallConfig
) -> Dict[str, Any]:
    """Poll get_call_status until terminal or tries are exhausted.

    Returns {"available": bool, "terminal": bool, ...status fields}.
    available=False when the port has no status API.
    """
    get_status = getattr(bland_port, "get_call_status", None)
    if not callable(get_status):
        return {"available": False, "terminal": False}
    tries = max(1, int(config.outcome_poll_tries))
    interval = max(0, config.outcome_poll_interval_s)
    last: Dict[str, Any] = {"ok": False}
    for _ in range(tries):
        try:
            st = get_status(call_id) or {}
        except Exception as exc:  # noqa: BLE001 - transient; keep polling
            last = {"ok": False, "error": str(exc)[:200]}
            time.sleep(interval)
            continue
        last = dict(st)
        if str(st.get("status") or "").lower() in TERMINAL_CALL_STATUSES:
            last["available"] = True
            last["terminal"] = True
            return last
        time.sleep(interval)
    last["available"] = True
    last["terminal"] = False
    return last


def _call_was_connected(status_result: Dict[str, Any], *,
                        message_left: bool = False) -> bool:
    """Did this terminal call reach a live person (or leave the voicemail)?

    A call that ended as "failed", "busy", "no-answer", or "canceled" is
    terminal but NOT successful. A "completed" call is successful only when
    the transcript (or Bland's answered_by when there is no transcript)
    shows a live person, or Robie left the voicemail message. A recording,
    robocall, phone menu, hold message, call screener or voicemail greeting
    with no message is NOT successful (call d25f46d6, Oct 8 2026). See
    call_contact.classify_contact.
    """
    if not status_result.get("terminal"):
        return False
    status = str(status_result.get("status") or "").lower()
    if status not in SUCCESSFUL_TERMINAL_STATUSES:
        return False
    contact = status_result.get("contact")
    if not contact:
        contact, _ = cc.classify_contact(status_result, message_left=message_left)
    return contact in cc.SUCCESSFUL_CONTACTS


def _was_transferred(status_result: Dict[str, Any]) -> bool:
    """Did this call end via a live transfer to a human?

    Eva transfers when the other party gets frustrated or asks for a human.
    Bland reports this in the status payload (explicit flag or a transfer
    status); either counts. A transferred call is verified (we know what
    happened) but NOT task success — the assigner took over.
    """
    if status_result.get("transferred"):
        return True
    return "transfer" in str(status_result.get("status") or "").lower()


def _outcome_was_transferred(outcome: Dict[str, Any]) -> bool:
    """True when any verified call in the outcome was transferred."""
    return any(
        _was_transferred(st)
        for st in (outcome.get("statuses") or {}).values()
    )


def _identity_line(producer_name: str) -> str:
    """Who Eva said she was calling for. The assigned producer, never a placeholder."""
    who = (producer_name or "").strip() or "the assigned producer"
    return (
        f"Eva identified herself as an AI assistant calling on behalf of {who} "
        f"from StreetSmart Insurance."
    )


def _format_transfer_note(applicant_name: str, instruction: str,
                          call_result: Dict[str, Any],
                          transfer_to: str,
                          producer_name: str = "") -> str:
    """Structured note for a frustration transfer. Plain English, no digits.

    Covers what Carlo requires: why Roby transferred, the call outcome,
    and the notes of the call.
    """
    who = applicant_name or "the client"
    topic = _scrub_phones((instruction or "")[:120].strip())
    attempts = call_result.get("attempts") or []
    call_ids = ", ".join(call_result.get("call_ids") or []) or "n/a"
    # Call notes: transcript/summary excerpt when Bland provided one.
    notes_bit = ""
    for a in reversed(attempts):
        summary = (a.get("summary") or a.get("transcript_summary") or "").strip()
        if summary:
            notes_bit = f" Call notes: {_scrub_phones(summary[:500])}"
            break
    lines = [
        f"Roby transferred this call to {transfer_to or 'the task assigner'}.",
        f"Why: the person on the line was getting frustrated, so Roby handed "
        f"the live call to a human instead of continuing.",
        f"Call outcome: the call connected and was transferred "
        f"(call IDs {call_ids}).{notes_bit}",
        f"This was about {topic} for {who}.",
        _identity_line(producer_name),
    ]
    return "\n".join(lines)


def _verify_call_outcome(
    bland_port: Any, call_ids: List[str], config: RobieCallConfig,
    *, redialed: bool = False,
) -> Dict[str, Any]:
    """Verify every placed call_id against Bland.

    Returns {"verified": bool, "successful": bool, "available": bool,
             "contact": str, "statuses": {call_id: poll-result}}.
    verified=True when at least one call reached a terminal status.
    successful=True only when at least one call reached a live person or
    left the voicemail message (call_contact.SUCCESSFUL_CONTACTS). A call
    that ended as failed/busy/no-answer/canceled, or reached only a
    recording, phone menu, hold message, call screener or voicemail
    greeting, is verified (we know it ended) but NOT successful.
    The second dial (Jake's double dial) is the one that leaves the
    voicemail message, so a voicemail there counts as message left.
    """
    statuses: Dict[str, Dict[str, Any]] = {}
    available = False
    contacts: List[str] = []
    for index, cid in enumerate(call_ids):
        res = _poll_call_status(bland_port, cid, config)
        if res.get("terminal"):
            contact, reason = cc.classify_contact(
                res, message_left=bool(redialed or index >= 1))
            res["contact"] = contact
            res["contact_reason"] = reason
            contacts.append(contact)
        statuses[cid] = res
        if res.get("available"):
            available = True
    verified = available and any(s.get("terminal") for s in statuses.values())
    successful = available and any(
        _call_was_connected(s) for s in statuses.values()
    )
    return {
        "verified": verified,
        "successful": successful,
        "available": available,
        "contact": cc.best_contact(contacts) if contacts else None,
        "statuses": statuses,
    }


def _attach_verified_status(
    call_result: Dict[str, Any], outcome: Dict[str, Any]
) -> Dict[str, Any]:
    """Merge verified terminal statuses into the attempts list so the
    outcome note renders from Bland's own status, not the port's claim."""
    attempts = list(call_result.get("attempts") or [])
    call_ids = list(call_result.get("call_ids") or [])
    statuses = outcome.get("statuses") or {}
    for i, cid in enumerate(call_ids):
        st = statuses.get(cid) or {}
        if not st.get("terminal"):
            continue
        final_status = {
            "status": st.get("status"),
            "answered_by": st.get("answered_by"),
            "duration": st.get("duration_s"),
            "outcome_verified": True,
            "contact": st.get("contact"),
            "contact_reason": st.get("contact_reason"),
            "recording_kind": (
                cc.recording_kind(st) if st.get("contact") == cc.RECORDING else None
            ),
        }
        if i < len(attempts) and isinstance(attempts[i], dict):
            attempts[i] = {**attempts[i], "final_status": final_status}
        else:
            attempts.append({"attempt": i + 1, "success": True,
                             "final_status": final_status})
    call_result["attempts"] = attempts
    return call_result


def _format_recovery_note(
    applicant_name: str,
    instruction: str,
    call_result: Dict[str, Any],
    producer_name: str = "",
    called_party: str = "",
) -> str:
    """Outcome note for a call recovered after an interruption.

    States plainly that Robie is reconciling a previous attempt, then the
    verified outcome. Never contains a phone number.
    """
    verdict = _format_outcome_note(
        applicant_name, instruction, call_result, "skipped",
        producer_name=producer_name, called_party=called_party)
    return (
        f"Robie recovered this call after an interruption — a previous "
        f"attempt had already dialed, so Robie did NOT call again and "
        f"instead confirmed the outcome with the phone system.\n"
        f"{verdict}"
    )


# ---------------------------------------------------------------------------
# Bland payload contract (what the production BlandCallPort must send)
# ---------------------------------------------------------------------------

def bland_payload_spec(
    phone: str,
    task_text: str,
    first_sentence: str,
    voicemail_message: str,
    attempt: int,
    metadata: Optional[Dict[str, Any]] = None,
    transfer_phone_number: Optional[str] = None,
) -> Dict[str, Any]:
    """The exact POST /v1/calls body the BlandCallPort must send per attempt.

    This is the contract the worker's production Bland wiring implements.
    Attempt 1: voicemail_action=hangup (silent). Attempt 2: leave_message
    with the full slow voicemail_message. transfer_phone_number is included
    only when a transfer lookup resolved a real direct dial. With no lookup
    the payload has no transfer target.
    """
    payload: Dict[str, Any] = {
        "phone_number": phone,
        "task": task_text,
        "voice": KAREN_VOICE_ID,
        "model": "enhanced",
        "record": False,  # Recording OFF per Carlo's design requirement
        "answered_by_enabled": True,
        "wait_for_greeting": True,
        "first_sentence": first_sentence,
        "from": CALLER_ID,
        "max_duration": 12,  # minutes
    }
    if transfer_phone_number:
        payload["transfer_phone_number"] = transfer_phone_number
    if attempt <= 1:
        payload["voicemail_action"] = "hangup"
    else:
        payload["voicemail_action"] = "leave_message"
        payload["voicemail_message"] = voicemail_message
    if metadata:
        payload["metadata"] = metadata
    return payload


# ---------------------------------------------------------------------------
# Eva's call script (Jake's specs via bland_config)
# ---------------------------------------------------------------------------

def _script_policy(producer_name: str) -> BlandRedialPolicy:
    """Intro and voicemail name the client's assigned producer, not a placeholder."""
    who = (producer_name or "").strip() or "the assigned producer"
    config = BlandCallConfig(
        intro_template=(
            "Hi, this is an AI assistant calling on behalf of "
            f"{who} from StreetSmart Insurance. {{reason}}."
        ),
        voicemail_message_template=(
            "Hi, this is an AI assistant calling on behalf of "
            f"{who} from StreetSmart Insurance. {{reason}}. "
            "Please call us back at {callback_spoken}. "
            "Again, that's {callback_spoken}."
        ),
    )
    return BlandRedialPolicy(config)


def _build_eva_task(instruction: str, applicant_name: str, *,
                    on_behalf_of: bool = False,
                    called_party: str = "",
                    producer_name: str = "",
                    transfer_to_name: str = "",
                    transfer_number: str = "") -> str:
    """Eva's task prompt: AI disclosure + freeform instruction + screener rules.

    Calls are on behalf of the client's assigned producer.
    on_behalf_of / called_party: the task directs a third-party call (e.g.
    the carrier). Eva names who she called and which client it is for.
    transfer_to_name/transfer_number: a resolved direct dial. When
    transfer_number is empty there is no transfer offer; Eva takes a message.
    """
    policy = _script_policy(producer_name)
    name_bit = f" The client is {applicant_name}." if applicant_name else ""
    behalf_bit = ""
    if on_behalf_of and called_party:
        behalf_bit = (
            f" You are calling {called_party}"
            f"{f' for {applicant_name}' if applicant_name else ''}."
        )
    if transfer_number:
        transfer_bit = (
            f" If the person you are speaking with gets frustrated, asks for a "
            f"human, or you cannot complete what was asked, transfer the call "
            f"to {transfer_to_name or 'the person who owns this task'} "
            f"at {transfer_number}."
        )
    else:
        transfer_bit = (
            " If the person gets frustrated or asks for a human, do not "
            "transfer the call. Take a message and ask them to call "
            f"{CALLBACK_NUMBER}."
        )
    return (
        f"{policy.build_intro(instruction.strip())}{name_bit}{behalf_bit} "
        f"{BlandCallConfig().screener_instructions}"
        f"{transfer_bit}"
    )


def _build_first_sentence(instruction: str, producer_name: str = "") -> str:
    policy = _script_policy(producer_name)
    return policy.build_intro(instruction.strip())


def _build_voicemail_message(instruction: str, producer_name: str = "") -> str:
    policy = _script_policy(producer_name)
    # One-sentence reason for the message; the policy adds AI disclosure + callback.
    reason = instruction.strip().split(".")[0][:160]
    return policy.build_voicemail_message(reason or "following up on your account")


# ---------------------------------------------------------------------------
# Writeback (API-only, allowlist-gated, no phone numbers in notes)
# ---------------------------------------------------------------------------

# The task's Discussion ID, pinned for the outcome note. WRITE_SCOPE=all
# still has to find that discussion on this applicant before anything is posted.
_outcome_discussion_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "robie_outcome_discussion_id", default="",
)


def _writeback_outcome_note(
    discussion_client: Any,
    applicant_id: str,
    body: str,
    title_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """Append the outcome note via the repo-standard fail-closed path.

    The discussion id from the task is passed through. ``file_note_to_existing_discussion``
    lists the applicant's discussions and posts only when that id is one of them.
    """
    from .ezlynx_discussions import file_note_to_existing_discussion

    pinned = _outcome_discussion_id.get().strip()
    try:
        return file_note_to_existing_discussion(
            discussion_client,
            applicant_id,
            body[:MAX_NOTE_CHARS],
            title_hint=title_hint,
            discussion_id=pinned or None,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced in result dict
        logger.error("writeback failed for applicant %s: %s", applicant_id, exc)
        return {"status": "error", "error": str(exc)[:300], "discussion_id": None, "note_id": None}


def _merge_checkpoint(ports: RobieCallPorts, task_id: str,
                      update: Dict[str, Any]) -> None:
    """Merge `update` into the existing checkpoint without clobbering it.

    Raises CheckpointError when the read or the (retried) write fails. A
    failed merge is a real failure: silently continuing would lose the
    dial intent / call_ids / notes_filed markers that restart recovery
    depends on.
    """
    current = _load_checkpoint(ports, task_id, strict=True)
    current.update(update)
    _save_checkpoint_retrying(ports, task_id, current)


def _writeback_once(
    ports: RobieCallPorts,
    task_id: str,
    note_key: str,
    applicant_id: str,
    body: str,
    title_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """Post a note at most once per task, even across restarts.

    Before posting, checks the durable checkpoint: if a note with this
    `note_key` was already filed (note_id recorded), returns the cached
    success WITHOUT posting again. After a successful post, records the
    note_id in the checkpoint.

    An uncertain POST (status "held"/"pending"/"error") is NEVER recorded
    as filed and is NEVER retried here — the caller fails closed and a
    later run re-checks the checkpoint rather than blindly re-posting.
    """
    # Strict read: if the checkpoint is unreadable we cannot prove this
    # note wasn't already filed — posting blindly could double-post. Fail
    # closed instead.
    checkpoint = _load_checkpoint(ports, task_id, strict=True)
    filed = (checkpoint.get("notes_filed") or {}).get(note_key) or {}
    if filed.get("note_id"):
        logger.info("note %r already filed for task %s (note_id %s); not re-posting",
                    note_key, task_id, filed.get("note_id"))
        return {
            "status": "filed",
            "note_id": filed.get("note_id"),
            "discussion_id": filed.get("discussion_id"),
            "applicant_id": applicant_id,
            "reason": "already filed (checkpoint); not re-posted",
            "duplicate_suppressed": True,
        }
    result = _writeback_outcome_note(
        ports.discussion_client, applicant_id, body, title_hint=title_hint)
    if result.get("status") == "filed" and result.get("note_id"):
        notes_filed = dict(checkpoint.get("notes_filed") or {})
        notes_filed[note_key] = {
            "note_id": result.get("note_id"),
            "discussion_id": result.get("discussion_id"),
            "filed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _merge_checkpoint(ports, task_id, {"notes_filed": notes_filed})
        logger.info("note %r filed for task %s (note_id %s); recorded in checkpoint",
                    note_key, task_id, result.get("note_id"))
    elif result.get("status") not in ("filed", "dry_run"):
        # Uncertain POST: held/pending/error. Do NOT record, do NOT retry.
        logger.warning("note %r for task %s uncertain (status=%s); "
                       "not recording, not retrying",
                       note_key, task_id, result.get("status"))
    return result


_TOPIC_WORD_CAP = 12
# "Please call …", "reach out to …", "phone the client …". The words after
# the verb are the callee and the reason, not the note's opening.
_CALL_PREFIX_RE = re.compile(
    r"(?i)^(?:(?:please|pls)\s+)?"
    r"(?:reach\s+out\s+to|call|contact|phone|ring)\b[\s,]*"
)
_CONNECTOR_RE = re.compile(r"(?i)^(to|about|regarding|re|and)\b[\s,:;-]*")
_GENERIC_CALLEE_RE = re.compile(
    r"(?i)^the\s+(?:client|insured|customer)\b[\s,]*"
)
# One to three name words, and only when a connector follows. Without the
# connector the leftover ("John Smith at" versus "John Smith at phone")
# stays, so two tasks do not file the same sentence.
_NAME_BEFORE_CONNECTOR_RE = re.compile(
    r"(?i)^(?!(?:to|about|regarding|re|and)\b)"
    r"[A-Za-z][\w'.-]*"
    r"(?:\s+(?!(?:to|about|regarding|re|and)\b)[A-Za-z][\w'.-]*){0,2}"
    r"\s+(?=(?:to|about|regarding|re|and)\b)"
)
# Spoken-script leftovers. The first of these ends the topic.
_INSTRUCTION_PHRASE_RE = re.compile(
    r"(?i)(?:^|[\s,;:.-]+)(?:"
    r"and\s+say|say\s+that|say\s+you(?:'re| are)?|"
    r"ask\s+(?:to|for|how|them|if)|"
    r"tell\s+(?:them|the|him|her)|"
    r"let\s+them\s+know|"
    r"and\s+ask|and\s+tell|and\s+let"
    r")\b"
)
_FUNCTION_WORDS = frozenset({
    "and", "or", "to", "about", "the", "a", "an", "for", "of", "re", "regarding",
})
_DEFINITE_MISS_STATUSES = {
    "no-answer", "no_answer", "no answer",
    "busy", "failed", "canceled", "cancelled",
}


def _clean_topic_text(instruction: str) -> str:
    """Phones and [placeholders] never belong in the note."""
    text = _scrub_phones(instruction or "")
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(
        r"(?i)\bat\s+(?=(?:to|about|regarding|re|and)\b)",
        " ",
        text,
    )
    return re.sub(r"\s+", " ", text).strip(" .")


def _cap_words(text: str, limit: int = _TOPIC_WORD_CAP) -> str:
    words = text.split()
    return " ".join(words[:limit]).strip(" .")


def _strip_instruction(topic: str) -> str:
    """Drop everything from the first 'say that' / 'ask to' / 'tell them' phrase."""
    match = _INSTRUCTION_PHRASE_RE.search(topic)
    if match:
        topic = topic[:match.start()]
    return re.sub(r"\s+", " ", topic).strip(" .,;:-")


def _strip_dangling_and(topic: str) -> str:
    """A topic must not end on a leftover 'and'."""
    return re.sub(r"(?i)(?:\s+\band\b)+$", "", topic).strip(" .")


def _topic_is_thin(topic: str) -> bool:
    """Empty, or only connector words. One real word such as "claim" stays."""
    words = [word.strip(".,;:").lower() for word in topic.split() if word.strip(".,;:")]
    return not any(word not in _FUNCTION_WORDS for word in words)


def _outcome_clause(instruction: str) -> tuple:
    """(connector, topic) for the first sentence of an outcome note.

    A leading call instruction is removed: please/pls, then call/contact/
    reach out to/phone/ring, then the client/insured/customer, a name, or
    a phone number, then to/about/regarding/re/and. A later instruction
    phrase (and say, say that, ask to, ask for, tell them, let them know)
    ends the topic. The topic is capped at about a dozen words and does
    not end on 'and'. What remains, if it is too thin to say, is
    "about the request in this task". An empty description is
    "about this task". A description with no call verb is "about" that
    description.
    """
    text = _clean_topic_text(instruction)
    if not text:
        return "about", "this task"
    stripped = _CALL_PREFIX_RE.sub("", text, count=1).strip()
    had_verb = stripped != text
    if not stripped:
        return "about", "this task"
    connector = "about"
    topic = stripped
    if had_verb:
        match = _CONNECTOR_RE.match(stripped)
        callee = _GENERIC_CALLEE_RE.match(stripped) or _NAME_BEFORE_CONNECTOR_RE.match(stripped)
        if match:
            connector = match.group(1).lower()
            topic = stripped[match.end():].strip()
        elif callee:
            rest = stripped[callee.end():].strip()
            follow = _CONNECTOR_RE.match(rest)
            if follow:
                connector = follow.group(1).lower()
                topic = rest[follow.end():].strip()
    topic = _strip_dangling_and(_cap_words(_strip_instruction(topic)))
    if _topic_is_thin(topic):
        return "about", "the request in this task"
    return connector, topic


def _outcome_opening(who: str, behalf: str, instruction: str, *, dry_run: bool) -> str:
    """'Called X for Y to confirm the new vehicle.' One trailing period."""
    connector, topic = _outcome_clause(instruction)
    if dry_run:
        head = "DRY RUN: no call was placed"
    else:
        head = f"Called {who}"
    if behalf:
        head += f" for {behalf}"
    return f"{head} {connector} {topic}."


def _is_definite_no_answer(attempts: List[Dict[str, Any]]) -> bool:
    """Bland named a miss: no-answer, busy, failed, or canceled."""
    for attempt in attempts:
        final = attempt.get("final_status") or {}
        for key in ("status", "answered_by"):
            status = str(final.get(key) or "").strip().lower()
            folded = status.replace("_", " ").replace("-", " ")
            if status in _DEFINITE_MISS_STATUSES or folded in _DEFINITE_MISS_STATUSES:
                return True
    return False


def _format_outcome_note(
    applicant_name: str,
    instruction: str,
    call: Dict[str, Any],
    recording_status: str,
    phone_mismatch: Optional[str] = None,
    name_mismatch: Optional[str] = None,
    producer_name: str = "",
    called_party: str = "",
    phone_choice: str = "",
) -> str:
    """Outcome note. Plain English, first line states the outcome.

    Never contains a phone number (DiscussionApi refuses them) — mismatched
    numbers from the task text are described, not quoted. Uncertainty
    (unknown answered_by, missing data) is stated plainly; the note never
    pretends an uncertain call went fine.
    """
    attempts = call.get("attempts") or []
    who = (called_party or "").strip() or applicant_name or "the client"
    client = applicant_name or "the client"
    behalf = (producer_name or "").strip()
    opened = _outcome_opening(who, behalf, instruction, dry_run=False)

    # Verdict: who or what answered. Bland's "completed" only means the
    # line picked up; the transcript decides whether a live person was
    # reached (call_contact.classify_contact). A recording, phone menu,
    # hold message or call screener is said plainly: nobody was reached.
    vm_hit = bool(call.get("voicemail_hit"))
    message_left = bool(call.get("redialed")) or any(
        str(
            (a.get("final_status") or {}).get("voicemail_action")
            or a.get("voicemail_action")
            or ""
        ) == "leave_message"
        for a in attempts
    )
    contacts: List[str] = []
    recording_kind = ""
    for a in attempts:
        if not a.get("success"):
            continue
        final = a.get("final_status") or {}
        if not final:
            continue
        contact = final.get("contact")
        if not contact:
            contact, _ = cc.classify_contact(final, message_left=message_left)
        if contact == cc.UNCONFIRMED and vm_hit:
            contact = (cc.VOICEMAIL_MESSAGE_LEFT if message_left
                       else cc.VOICEMAIL_NO_MESSAGE)
        if contact == cc.RECORDING and not recording_kind:
            recording_kind = str(final.get("recording_kind") or "")
        contacts.append(contact)
    contact = cc.best_contact(contacts) if contacts else ""
    connected = contact in (cc.PERSON, cc.VOICEMAIL_MESSAGE_LEFT,
                            cc.VOICEMAIL_NO_MESSAGE, cc.RECORDING,
                            cc.UNCONFIRMED)
    # "unknown" covers the placed-but-unverifiable case: Bland accepted the
    # dial (top-level success) but no attempt proves a connection. Only a
    # top-level failure renders as NOT successful.
    unknown = not connected and (
        bool(call.get("success")) or any(a.get("success") for a in attempts)
    )

    if call.get("dry_run"):
        return _outcome_opening(who, behalf, instruction, dry_run=True)

    disclose = False
    if connected:
        if contact == cc.PERSON:
            lines = [opened, "They answered and I talked to them."]
            disclose = True
        elif contact == cc.VOICEMAIL_MESSAGE_LEFT:
            lines = [opened, "No answer, left a voicemail."]
            disclose = True
        elif contact == cc.VOICEMAIL_NO_MESSAGE:
            lines = [opened, "No answer, no message."]
        elif contact == cc.RECORDING:
            what = recording_kind or "a recording or automated phone system"
            lines = [
                opened,
                f"Nobody was reached. Only {what} answered, not a live "
                "person, so I hung up and left no message.",
                "This task is left open for a person to follow up.",
            ]
        else:
            lines = [
                opened,
                "The call connected, but I couldn't confirm whether it "
                "reached the person or voicemail.",
                "This task is left open for a person to check.",
            ]
    elif unknown and not _is_definite_no_answer(attempts):
        lines = [
            opened,
            "No answer, no message.",
            "I couldn't confirm whether the call went through.",
        ]
    else:
        lines = [opened, "No answer, no message."]

    rec = {
        "ok": "The call recording was uploaded.",
        "failed": "Note: the call recording is not available — the upload failed.",
        "pending": "Note: the call recording is not yet available.",
        "skipped": "",
    }.get(recording_status, "")
    if rec:
        lines.append(rec)

    # Sloppy-human discrepancy flags (trust-building: surface, don't hide).
    if phone_mismatch:
        lines.append("Note: the task mentioned a different phone number than "
                     "the one on file; we called the number on file.")
    if phone_choice in ("first_line", "labeled"):
        where = ("the one on the first line" if phone_choice == "first_line"
                 else "the one labeled as the number to call")
        lines.append("Note: the task had more than one phone number; I "
                     f"called {where}.")
    if name_mismatch:
        lines.append(f"Note: the task mentioned {name_mismatch}, but the "
                     f"applicant on file is {client} — please check the right "
                     f"person was reached.")

    # The AI disclosure is for a conversation: someone answered, or a
    # voicemail was left. A miss is just the outcome.
    if disclose:
        lines.append(_identity_line(behalf))
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def handle_robie_call_task(
    task: Dict[str, Any],
    config: RobieCallConfig,
    ports: RobieCallPorts,
) -> Dict[str, Any]:
    """Handle one freeform "Robie Call" task. NEVER raises.

    Wrapper: claims the task in-flight so two overlapping runs (the 5-minute
    intake plus a manual trigger) can never dial twice, then delegates.
    """
    task_id = _pick(task, "task_id") or None
    if task_id and not _claim_inflight(task_id):
        logger.info("task %s already in flight; suppressing duplicate", task_id)
        return {
            "ok": True,
            "task_id": task_id,
            "applicant_id": _pick(task, "applicant_id") or None,
            "call": None,
            "writeback": None,
            "recording": None,
            "reassigned": False,
            "chat_alerted": False,
            "error": None,
            "duplicate_suppressed": True,
            "duplicate_reason": "task already being processed",
        }
    try:
        return _handle_call_task(task, config, ports)
    except CheckpointError as exc:
        # A checkpoint read or write failed somewhere in the flow. This is a
        # real failure, never success: the durable restart-safety net is
        # compromised, so the task stays open for human review instead of
        # proceeding blind.
        logger.error("task %s checkpoint failure: %s", task_id, exc)
        return {
            "ok": False,
            "task_id": task_id,
            "applicant_id": _pick(task, "applicant_id") or None,
            "call": None,
            "writeback": None,
            "recording": None,
            "reassigned": False,
            "chat_alerted": False,
            "error": f"checkpoint failure: {exc}",
            "checkpoint_failed": True,
        }
    except Exception as exc:  # noqa: BLE001 - the NEVER-raises contract
        logger.exception("task %s unexpected error: %s", task_id, exc)
        return {
            "ok": False,
            "task_id": task_id,
            "applicant_id": _pick(task, "applicant_id") or None,
            "call": None,
            "writeback": None,
            "recording": None,
            "reassigned": False,
            "chat_alerted": False,
            "error": f"unexpected handler error: {exc}",
        }
    finally:
        if task_id:
            _release_inflight(task_id)


def _handle_call_task(
    task: Dict[str, Any],
    config: RobieCallConfig,
    ports: RobieCallPorts,
) -> Dict[str, Any]:
    """Handle one freeform "Robie Call" task. NEVER raises.

    Flow: validate -> idempotency -> crash recovery -> kill switch ->
    phone lookup -> Bland double-dial -> checkpoint -> outcome
    verification -> writeback -> reassign (with read-back).

    On call failure the task is left OPEN and NOT reassigned (a human must
    see it). On writeback failure with a successful call, a chat alert fires
    immediately (silent data loss).
    """
    task_id = _pick(task, "task_id")
    applicant_id = _pick(task, "applicant_id")
    applicant_name = _pick(task, "applicant_name")
    assigned_by = _pick(task, "assigned_by")
    _outcome_discussion_id.set(_pick(task, "discussion_id") or "")
    log = logging.LoggerAdapter(logger, {"task_id": task_id, "applicant_id": applicant_id})

    def fail(error: str, **extra: Any) -> Dict[str, Any]:
        log.error("task failed: %s", error)
        result: Dict[str, Any] = {
            "ok": False,
            "task_id": task_id or None,
            "applicant_id": applicant_id or None,
            "call": None,
            "writeback": None,
            "recording": None,
            "reassigned": False,
            "chat_alerted": False,
            "error": error,
        }
        result.update(extra)
        return result

    # ---- 1. Validate -------------------------------------------------------
    if not task_id:
        return fail("task has no task_id; refusing to process")
    if not applicant_id:
        return fail("task has no applicant_id; refusing to process")
    if not is_call_task(task):
        return fail("task does not look like a call task; skipping")
    if "dialable" in task and task.get("dialable") is not True:
        return fail("call job is not dialable; a non-live queue is never dialed")
    queued_at = str(task.get("queued_at") or "")
    live_at = str(task.get("live_enabled_at") or "")
    if live_at and queued_at and queued_at < live_at:
        return fail("call job was queued before live mode; not dialing")

    # ---- 1b. Authorization: the task must be assigned to Robie ---------------
    # An outbound client call is only ever placed for a task explicitly
    # routed to Robie AI. A forged or misrouted task dict must not dial.
    assigned_to = (_pick(task, "assigned_to") or "").strip()
    if assigned_to and "robie" not in assigned_to.lower():
        return fail(
            f"task is assigned to {assigned_to!r}, not Robie; "
            "refusing to place an outbound call"
        )
    if not assigned_to:
        log.warning("task %s has no assigned_to field; proceeding — the "
                    "intake only creates jobs for Robie-assigned tasks",
                    task_id)

    # ---- 2. Idempotency (task_id) -------------------------------------------
    if _already_processed(task_id):
        log.info("task already processed within window; suppressing duplicate")
        return {
            "ok": True,
            "task_id": task_id,
            "applicant_id": applicant_id,
            "call": None,
            "writeback": None,
            "recording": None,
            "reassigned": False,
            "chat_alerted": False,
            "error": None,
            "duplicate_suppressed": True,
        }

    # ---- 2b. Task still open? (stale CSV guard) ------------------------------
    # The CSV is generated up to 30 min before we process it; a human may
    # have closed the task since. Calling on a closed task erodes trust.
    if ports.task_status is not None:
        try:
            still_open = ports.task_status.is_task_open(task_id)
        except Exception as exc:  # noqa: BLE001 - proceed on check failure
            logger.warning("task status check failed for %s: %s", task_id, exc)
            still_open = None
        if still_open is False:
            log.info("task %s already closed; skipping", task_id)
            _mark_processed(task_id)
            return {
                "ok": True,
                "task_id": task_id,
                "applicant_id": applicant_id,
                "call": None,
                "writeback": None,
                "recording": None,
                "reassigned": False,
                "chat_alerted": False,
                "error": None,
                "skipped_closed_task": True,
            }

    # ---- 3. Instruction (needed by recovery + ambiguity paths) ---------------
    instruction = _extract_instruction(task)
    if not instruction:
        from .call_pickup import classify_call_request as _classify
        from .splice_scripts import get_workflow as _get_workflow

        _early = _classify(_pick(task, "activity_labels"), "")
        _wf = _get_workflow(_early.workflow_id) if _early.action == "workflow" else None
        if _wf is not None:
            # A scripted label needs no typed words; the label is the request.
            instruction = _wf.title
        else:
            return fail("task has no usable instruction text; failing closed")

    # ---- 3b. Content dedup (before ambiguity: identical re-tasks) -----------
    # Same applicant + same instruction as two different tasks (sloppy
    # double-assignment, or the same ambiguous task re-filed): the second
    # must not dial OR file a second clarification note.
    if _content_already_processed(applicant_id, instruction):
        log.info("duplicate instruction for applicant %s; suppressing",
                 applicant_id)
        _mark_processed(task_id)
        return {
            "ok": True,
            "task_id": task_id,
            "applicant_id": applicant_id,
            "call": None,
            "writeback": None,
            "recording": None,
            "reassigned": False,
            "chat_alerted": False,
            "error": None,
            "duplicate_suppressed": True,
            "duplicate_reason": "same instruction already handled for applicant",
        }

    # Robie Call is free-form. Lead follow-up and each enabled Splice
    # label use that workflow's script, spoken for the assigned producer.
    from .call_pickup import (
        SPLICE_WORKFLOW_IDS,
        calling_day,
        classify_call_request,
        is_test_server,
        note_dedupe_key,
        splice_task_predates_enablement,
        splice_test_account_reason,
    )
    from .splice_scripts import get_workflow

    decision = classify_call_request(_pick(task, "activity_labels"), "")
    workflow = None
    if decision.action == "workflow" and decision.workflow_id:
        workflow = get_workflow(decision.workflow_id)
        if workflow is None:
            return fail(
                f"label selected unknown workflow {decision.workflow_id}; not dialing"
            )
    if workflow is not None and workflow.id in SPLICE_WORKFLOW_IDS:
        splice_at = str(task.get("splice_enabled_at") or "")
        created_raw = _pick(task, "created_date")
        if splice_at and splice_task_predates_enablement(created_raw, splice_at):
            return fail(
                "task was labeled before this workflow was enabled; not dialing"
            )
        account_reason = splice_test_account_reason(applicant_id)
        if account_reason:
            log.warning(
                "splice test account blocked task %s: %s", task_id, account_reason,
            )
            clar_note = (
                "Robie did not place this call. On the Test server this "
                "workflow dials only Jake Ferrara's own client account, and "
                "only his test phone. This task is not that account, or that "
                "account is not configured. Buster Brown is not the test "
                "client for these labels."
            )
            wb = _writeback_once(
                ports, task_id, "clarification_test_account",
                applicant_id, clar_note, title_hint=None)
            alerted = _chat_alert(
                ports, config,
                f"Robie Call BLOCKED for applicant {applicant_id} (task "
                f"{task_id}): {account_reason}. No call was placed.",
            )
            return fail(
                f"{account_reason}; not dialing",
                writeback=wb,
                chat_alerted=alerted,
                skipped_test_account=True,
                clarification_note_filed=wb.get("status") in ("filed", "dry_run"),
            )
    note_topic = workflow.title if workflow is not None else instruction

    # ---- 3c. Ambiguity guard ------------------------------------------------
    # "call him" with no topic: do NOT guess. Leave the task open and file
    # a clarification note so staff can fix the instruction.
    # A resolved workflow already has its script, so a short label is enough.
    ambiguity = None if workflow is not None else _instruction_ambiguity(instruction)
    if ambiguity:
        log.warning("ambiguous instruction for task %s: %s", task_id, ambiguity)
        clar_note = (
            f"Robie received a call task but couldn't act on it: {ambiguity}. "
            f"The task said: {_scrub_phones(instruction[:200])!r}. "
            "Please update the task with what the call is about "
            "(for example, 'Call about the renewal documents'), "
            "and Robie will pick it up on the next check."
        )
        wb = _writeback_once(
            ports, task_id, "clarification_instruction",
            applicant_id, clar_note, title_hint=None)
        _mark_processed(task_id)
        _mark_content_processed(applicant_id, instruction)
        return fail(
            f"ambiguous instruction ({ambiguity}); clarification note filed, "
            "task left open",
            writeback=wb,
            clarification_note_filed=wb.get("status") in ("filed", "dry_run"),
        )
    log.info("handling call task for applicant %s: %r", applicant_id, instruction[:80])

    # ---- 3d. Crash recovery (durable checkpoint) -----------------------------
    # If a previous run dialed but died before finishing (process crash,
    # deploy, timeout after Bland accepted), the checkpoint holds the
    # call_ids. Reconcile — poll Bland for the outcome — instead of
    # dialing again. This survives restarts; the in-memory guards do not.
    #
    # Strict read: an unreadable checkpoint blinds every anti-redial guard
    # below (completed_at, call_ids, dial_intent). Failing closed here is
    # what guarantees a retry or restart never places the same call twice.
    try:
        checkpoint = _load_checkpoint(ports, task_id, strict=True)
    except CheckpointError as exc:
        return fail(
            f"checkpoint unreadable ({exc}); cannot verify no prior dial — "
            "NOT dialing, task left open",
            checkpoint_unreadable=True,
        )
    if checkpoint.get("completed_at"):
        log.info("checkpoint shows task %s already completed; suppressing", task_id)
        _mark_processed(task_id)
        return {
            "ok": True,
            "task_id": task_id,
            "applicant_id": applicant_id,
            "call": None,
            "writeback": None,
            "recording": None,
            "reassigned": False,
            "chat_alerted": False,
            "error": None,
            "duplicate_suppressed": True,
            "duplicate_reason": "checkpoint shows completed",
        }
    recovered_ids = [c for c in (checkpoint.get("bland_call_ids") or []) if c]
    if recovered_ids:
        log.warning("recovered checkpoint for task %s with call_ids %s; "
                    "reconciling instead of redialing", task_id, recovered_ids)
        return _recover_interrupted_call(
            task, config, ports, log, fail,
            task_id, applicant_id, applicant_name, assigned_by,
            instruction, recovered_ids, checkpoint,
        )

    # ---- 3d2. Dial intent recovery (timeout during POST) ----------------------
    # If a previous run saved a dial intent but never recorded call_ids,
    # the Bland POST may have timed out AFTER Bland accepted it. We do NOT
    # dial again — we reconcile by checking Bland's recent calls for the
    # number from the intent. A timeout or failed checkpoint must trigger
    # reconciliation, never an automatic redial.
    dial_intent = checkpoint.get("dial_intent") or {}
    if dial_intent.get("status") == "dial_attempted":
        intent_phone = dial_intent.get("phone")
        intent_at = dial_intent.get("attempted_at", "unknown time")
        log.warning(
            "recovered dial intent for task %s (attempted at %s); "
            "reconciling via Bland recent calls instead of redialing",
            task_id, intent_at,
        )
        # Reconcile: check if Bland has a recent call to this number.
        # If found, treat as recovered call_ids. If not found, the dial
        # likely never went through — but we still do NOT auto-redial;
        # we fail closed with a clear message for human review.
        try:
            recent = ports.bland.recent_calls(
                intent_phone, since_seconds=IDEMPOTENCY_WINDOW_S
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("recent_calls check failed during intent recovery: %s", exc)
            recent = {"ok": False}
        if recent.get("ok") and recent.get("calls"):
            found = recent["calls"][0]
            found_id = found.get("call_id") or found.get("id")
            if found_id:
                log.warning(
                    "dial intent reconciled: found Bland call %s; "
                    "recovering instead of redialing", found_id)
                return _recover_interrupted_call(
                    task, config, ports, log, fail,
                    task_id, applicant_id, applicant_name, assigned_by,
                    note_topic, [found_id], checkpoint,
                )
        # No recent call found — the dial likely never went through, but
        # we do NOT auto-redial. Fail closed for human review.
        _mark_processed(task_id)
        return fail(
            "previous dial attempt timed out with unknown outcome; no "
            "recent Bland call found for reconciliation. NOT redialing "
            "automatically — human review required.",
            dial_intent_recovered=True,
            dial_intent_at=intent_at,
        )

    # ---- 4. Kill switch ----------------------------------------------------
    # Recovery above is not a new dial, so it runs even when halted; the
    # kill switch blocks NEW dials only.
    if _kill_switch_active():
        return fail("kill switch active (ROBIE_CALL_HALT); failing closed")

    # ---- 5. Phone lookup ---------------------------------------------------
    # Splice labels dial only the client's phone on file. A number or a
    # policy number in the task note is ignored.
    # Robie Call dials only a number typed in the task. It never uses the
    # phone on file. No usable typed number files one short note and does
    # not dial.
    # Carlo's rule (Oct 7 2026, 9:05 PM): staff just add the label and
    # Robie uses the phone on the client's account. Robie Lead Follow Up
    # and every Splice label therefore dial ONLY the number on file and
    # ignore digits typed in the note. Robie Call is the one exception.
    if decision.action == "freeform":
        phone_policy = "typed_only"
    else:
        phone_policy = "on_file_only"
    explicit_phone = None
    phone_text_ambiguous = False
    phone_why_not = None
    phone_choice = ""
    if phone_policy != "on_file_only":
        explicit_phone, phone_why_not, phone_choice = _typed_phone_choice(instruction)
        phone_text_ambiguous = phone_why_not is not None
    if phone_why_not == MULTIPLE_NUMBERS:
        # Two or more typed numbers and none clearly wins (first line or
        # labeled). Same rule as the Cloud Run Robie Call block (#811):
        # do not guess, ask for the one number.
        log.warning("more than one typed number in task %s and none clearly "
                    "wins; asking instead of dialing", task_id)
        clar_note = (
            "Robie did not call because more than one phone number was "
            f"typed in this {_request_word(task)}, and Robie will not guess "
            f"which one to call. {_redo_hint(task)} with just the one "
            "number to call, or put it on the first line."
        )
        wb = _writeback_once(
            ports, task_id, "clarification_multiple_numbers",
            applicant_id, clar_note, title_hint=None)
        _mark_processed(task_id)
        _mark_content_processed(applicant_id, instruction)
        return fail(
            "more than one phone number typed in the task and none clearly "
            "wins; asked for the one number, task left open",
            writeback=wb,
            multiple_numbers=True,
            clarification_note_filed=wb.get("status") in ("filed", "dry_run"),
        )
    if phone_text_ambiguous:
        log.warning("ambiguous number in task %s; asking instead of dialing",
                    task_id)
        if phone_policy == "typed_only":
            # Intake will not open this request again once it has a job.
            # The note has to ask for a new one.
            clar_note = (
                "Robie did not call because it could not tell which number "
                f"to dial. {_redo_hint(task)} and write 'call at' or 'phone' "
                "before the number."
            )
        else:
            clar_note = (
                "Robie received a call task but couldn't tell which phone to "
                "dial. The task includes a number that might be a policy, claim, "
                "or quote number rather than a phone number. Robie will not "
                "guess and will not dial it. Please update the task with the "
                "phone to call — for example, 'call at' followed by the number, "
                "or the word 'phone' or 'cell' before it — or remove the number "
                "if Robie should use the phone on file. Robie will pick this up "
                "on the next check."
            )
        wb = _writeback_once(
            ports, task_id, "clarification_ambiguous_number",
            applicant_id, clar_note, title_hint=None)
        _mark_processed(task_id)
        _mark_content_processed(applicant_id, instruction)
        return fail(
            "ambiguous number in the task; asked which phone to call, "
            "task left open",
            writeback=wb,
            clarification_note_filed=wb.get("status") in ("filed", "dry_run"),
        )
    if phone_policy == "typed_only" and not explicit_phone:
        log.warning("task %s has no typed phone number; asking instead of dialing",
                    task_id)
        if _PHONE_LIKE_RE.search(instruction or ""):
            log.info("task %s: typed number(s) skipped as a callback/voicemail "
                     "or StreetSmart number", task_id)
        clar_note = (
            "Robie did not call because no phone number was typed in this "
            f"{_request_word(task)}. A Robie Call only dials a number that is "
            f"typed in, never the number on file. {_redo_hint(task)} with "
            "the number to call."
        )
        wb = _writeback_once(
            ports, task_id, "clarification_missing_number",
            applicant_id, clar_note, title_hint=None)
        _mark_processed(task_id)
        _mark_content_processed(applicant_id, instruction)
        return fail(
            "no phone number typed in the task; asked for the number, "
            "task left open",
            writeback=wb,
            clarification_note_filed=wb.get("status") in ("filed", "dry_run"),
        )
    # Real Test intake never reads the applicant phone record. The Bland
    # port posts only Jake's cell. A typed Robie Call number still decides
    # whether the call is allowed; it is not the number that is posted.
    # Injected phone ports still run, including under ROBIE_ENV=TEST.
    if _test_intake_skips_applicant_lookup(ports) and phone_policy != "typed_only":
        log.info("test server: skipping applicant phone lookup for task %s", task_id)
        phone, phone_ambiguity = "+10000000000", None
        explicit_phone = None
    elif explicit_phone:
        log.info("using task-provided phone number for task %s", task_id)
        phone, phone_ambiguity = explicit_phone, None
    else:
        try:
            raw_phone = _retryable_call(
                lambda: ports.phone_lookup.get_phone(applicant_id),
                what=f"phone lookup applicant {applicant_id}",
            )
        except Exception as exc:  # noqa: BLE001
            return fail(f"phone lookup failed: {exc}")
        phone, phone_ambiguity = _resolve_phone(raw_phone)
    if phone_ambiguity is not None:
        # Several numbers on file, no clear best: do NOT guess. File a
        # clarification note (labels only — notes must never contain
        # digits) and leave the task open.
        labels = []
        for cand in phone_ambiguity.get("candidates", [])[:4]:
            label = str((cand or {}).get("label") or "a number").strip()
            if label and label not in labels:
                labels.append(label)
        label_text = ", ".join(labels) if labels else "more than one number"
        log.warning("ambiguous phone for applicant %s (%s); failing closed",
                    applicant_id, label_text)
        clar_note = (
            f"Robie received a call task but couldn't place the call: "
            f"the applicant has {label_text} on file and Robie couldn't "
            f"tell which one to call. Please update the task to say which "
            f"number to call (for example, 'call the cell number'), and "
            f"Robie will pick it up on the next check."
        )
        wb = _writeback_once(
            ports, task_id, "clarification_phone",
            applicant_id, clar_note, title_hint=None)
        _mark_processed(task_id)
        _mark_content_processed(applicant_id, instruction)
        return fail(
            f"ambiguous phone ({label_text}); clarification note filed, "
            "task left open",
            writeback=wb,
            phone_ambiguous=True,
            clarification_note_filed=wb.get("status") in ("filed", "dry_run"),
        )
    if not phone:
        log.warning("no phone on file for applicant %s; asking instead of dialing",
                    applicant_id)
        clar_note = (
            "Robie did not call because there is no phone number on this "
            "client's account. Add a phone number to the account, then "
            f"{_redo_hint(task, lower=True)}."
        )
        wb = _writeback_once(
            ports, task_id, "clarification_no_phone_on_file",
            applicant_id, clar_note, title_hint=None)
        _mark_processed(task_id)
        _mark_content_processed(applicant_id, instruction)
        return fail(
            f"no dialable phone found for applicant {applicant_id}; asked for "
            "a number, not dialed",
            writeback=wb,
            clarification_note_filed=wb.get("status") in ("filed", "dry_run"),
        )
    log.info("resolved phone for applicant %s", applicant_id)

    # ---- 5. Bland circuit breaker ------------------------------------------
    if not _bland_circuit_allows():
        return fail("Bland circuit breaker open (recent failures); failing closed")

    # ---- 5b. Recent-call guard (the timeout double-dial hole) ----------------
    # If a previous attempt timed out AFTER Bland accepted it, a call went
    # out that we never recorded. Redialing now would dial the client twice.
    # Check Bland's recent calls for this number first.
    try:
        recent = ports.bland.recent_calls(phone, since_seconds=IDEMPOTENCY_WINDOW_S)
    except Exception as exc:  # noqa: BLE001 - history unavailable; proceed
        logger.warning("recent_calls check failed for %s: %s", task_id, exc)
        recent = {"ok": False}
    if recent.get("ok") and recent.get("calls"):
        found = recent["calls"][0]
        found_id = found.get("call_id") or found.get("id") or "unknown"
        log.warning("recent Bland call %s found for applicant %s; not redialing",
                    found_id, applicant_id)
        note_body = (
            f"Robie Call for {applicant_name or 'the client'} (freeform task).\n"
            f"Instruction: {_scrub_phones(instruction[:500])}\n"
            f"A call to this number was already placed recently "
            f"(around the time of a previous attempt that timed out), so "
            f"Robie did NOT dial again to avoid calling twice. "
            f"If the client mentions a call from Eva, it was that attempt.\n"
            f"{_identity_line(_pick(task, 'assigned_producer'))}"
        )
        writeback = _writeback_once(
            ports, task_id, "recent_call_skip",
            applicant_id, note_body, title_hint=None)
        wb_ok = writeback.get("status") in ("filed", "dry_run")
        _mark_processed(task_id)
        _mark_content_processed(applicant_id, instruction)
        return {
            "ok": wb_ok,
            "task_id": task_id,
            "applicant_id": applicant_id,
            "call": {"success": False, "recovered_from_history": True,
                     "call_ids": [found_id]},
            "writeback": writeback,
            "recording": None,
            "reassigned": False,
            "chat_alerted": False,
            "error": None if wb_ok else "writeback failed",
            "skipped_recent_call": True,
        }

    # ---- 6. Place the call (double-dial) -----------------------------------
    # Identity gate: the task, applicant, and phone must all agree on WHO
    # we are calling. If the instruction names a different person than the
    # applicant ("call Mary Smith" on John Doe's account), do NOT dial —
    # the phone belongs to the applicant, not the named person. Fail closed
    # with a clarification note instead of merely flagging it in the note.
    # A Robie Call dials the typed number it chose, so the other typed
    # numbers are not a "different number than the one on file".
    phone_mismatch = (
        None
        if phone_policy in ("on_file_only", "typed_only")
        else _instruction_phone_mismatch(instruction, phone)
    )
    name_mismatch = _instruction_name_mismatch(instruction, applicant_name)
    # On-behalf-of calling: the task names someone other than the applicant
    # AND provides an explicit phone ("Call Progressive at 1-800-776-4737"
    # on Mary Smith's account). That's a directed third-party call — allowed.
    # The note names who was called. The call is on behalf of the client's
    # assigned producer. Without an explicit phone, a two-word name mismatch
    # still fails closed: the phone on file belongs to the applicant.
    callee = _named_callee(instruction)
    called_party = ""
    if explicit_phone:
        if name_mismatch:
            called_party = name_mismatch
        elif callee and not _name_overlaps(callee, applicant_name):
            called_party = callee
    on_behalf_of = bool(called_party)
    if on_behalf_of:
        log.info("on-behalf-of call for task %s: dialing %r for applicant %r",
                 task_id, called_party, applicant_name)
    if name_mismatch and not on_behalf_of:
        log.warning("instruction names %r but applicant is %r; failing closed",
                    name_mismatch, applicant_name)
        clar_note = (
            f"Robie received a call task but couldn't place the call: the task "
            f"says to call {name_mismatch}, but the account belongs to "
            f"{applicant_name or 'someone else'}. Robie won't guess who to call. "
            f"Please confirm who Robie should call and update the task, and "
            f"Robie will pick it up on the next check."
        )
        wb = _writeback_once(
            ports, task_id, "clarification_identity",
            applicant_id, clar_note, title_hint=None)
        _mark_processed(task_id)
        _mark_content_processed(applicant_id, instruction)
        return fail(
            f"instruction names {name_mismatch!r} but applicant is "
            f"{applicant_name!r}; clarification note filed, task left open",
            writeback=wb,
            clarification_note_filed=wb.get("status") in ("filed", "dry_run"),
        )
    # Calls are on behalf of the client's assigned producer. Missing
    # producer is a question, not a guess and not Jake.
    producer_name = _pick(task, "assigned_producer")
    if not producer_name:
        log.warning("task %s has no assigned producer; asking instead of dialing",
                    task_id)
        clar_note = (
            "Robie received a call task but couldn't place the call: this "
            "client has no assigned producer. Calls are placed on behalf of "
            "the client's assigned producer. Please set the assigned producer "
            "on the account and Robie will pick this up on the next check."
        )
        wb = _writeback_once(
            ports, task_id, "clarification_assigned_producer",
            applicant_id, clar_note, title_hint=None)
        _mark_processed(task_id)
        _mark_content_processed(applicant_id, instruction)
        return fail(
            "no assigned producer; asked instead of dialing, task left open",
            writeback=wb,
            clarification_note_filed=wb.get("status") in ("filed", "dry_run"),
        )
    opt_outs = getattr(ports, "opt_out_store", None)
    if opt_outs is not None and opt_outs.is_opted_out(applicant_id):
        log.info("task %s skipped; applicant opted out of automated calls", task_id)
        skip_note = (
            "Robie did not call. This client opted out of automated phone "
            "calls. Automated calls stay off until a person turns them back on."
        )
        wb = _writeback_once(
            ports, task_id, "opt_out_skip",
            applicant_id, skip_note, title_hint=None)
        _mark_processed(task_id)
        _mark_content_processed(applicant_id, instruction)
        return fail(
            "client opted out of automated calls; not dialed",
            writeback=wb,
        )
    if workflow is not None and workflow.marketing:
        opt_ins = getattr(ports, "opt_in_store", None)
        recorded = opt_ins is not None and opt_ins.has_opt_in(applicant_id)
        if not recorded:
            log.info(
                "task %s skipped; %s requires a recorded opt-in and this "
                "client has none",
                task_id, workflow.title,
            )
            return fail(
                "no recorded opt-in; marketing call not dialed",
                skipped_opt_in=True,
            )
    # Created Date in the report is Central. Convert it to Eastern before
    # the calling window, the same-day key, and any age check.
    from .report_clock import age_minutes, report_created_et

    raw_created = _pick(task, "created_date")
    created_et = report_created_et(raw_created) if raw_created else None
    if created_et is not None:
        now_for_age = _calling_now(config)
        log.info(
            "task %s created %s ET (age %.0f min)",
            task_id, created_et.isoformat(), age_minutes(created_et, now_for_age),
        )
    # No outbound dials outside the calling window. Queue the task (leave
    # it open, do not mark it processed) and say so once. Never dial.
    window_block = _outside_calling_window(config)
    if window_block:
        log.info("task %s outside calling window (%s); not dialing",
                 task_id, window_block)
        writeback: Dict[str, Any] = {
            "status": "dry_run" if config.dry_run else "skipped",
            "note_id": None,
            "discussion_id": None,
            "reason": "outside calling window; nothing written"
            if config.dry_run else "outside calling window",
        }
        if not config.dry_run:
            topic = _scrub_phones((instruction or "")[:120].strip())
            queue_note = (
                f"Robie queued this call and did not dial. "
                f"Outbound calls are placed only during "
                f"{_calling_window_label(config)}. "
                f"This was about {topic}. "
                f"Robie will place the call on the next check inside that "
                f"window. To schedule a different time, update the task."
            )
            writeback = _writeback_once(
                ports, task_id, "outside_calling_window",
                applicant_id, queue_note, title_hint=None)
        return fail(
            f"outside calling window ({window_block}); call queued, not dialed",
            queued_for_calling_window=True,
            writeback=writeback,
            call={"success": False, "call_ids": [], "dry_run": bool(config.dry_run)},
        )
    # Transfer target only when a lookup resolves one. Otherwise Eva takes
    # a message. There is no placeholder transfer number.
    transfer_number = _resolve_transfer_number(ports, producer_name) or ""
    if workflow is not None:
        from .splice_scripts import (
            render_live,
            render_text,
            render_voicemail,
            spoken_first_name,
            text_option_allowed,
        )

        first = spoken_first_name(applicant_name)
        offer_text = text_option_allowed(
            workflow,
            mobile=_phone_is_mobile(ports, task, applicant_id),
            sms_configured=bool(config.sms_configured),
        )
        live = render_live(
            workflow,
            first_name=first,
            agent=producer_name,
            transfer_number=transfer_number,
            offer_text=offer_text,
        )
        first_sentence = f"Hi {first}," if first else "Hi,"
        voicemail_message = render_voicemail(
            workflow, first_name=first, agent=producer_name,
        )
        operator = []
        if transfer_number:
            operator.append(
                f"When the caller presses 1, transfer to {producer_name} "
                f"at {transfer_number}."
            )
        else:
            operator.append(
                "Do not transfer this call. If the caller presses 1, take a "
                f"message and ask them to call {CALLBACK_NUMBER}."
            )
        if offer_text:
            operator.append("When the caller presses 2, send this text and nothing else:")
            operator.append(render_text(workflow) or "")
        else:
            operator.append("Do not offer or send a text message.")
        operator.append("When the caller presses 4, repeat the spoken script.")
        operator.append(
            "When the caller presses 6, they opted out of automated calls. "
            "Confirm that and end the call."
        )
        eva_task = live + "\n\n" + "\n".join(operator)
    else:
        eva_task = _build_eva_task(
            instruction, applicant_name,
            on_behalf_of=on_behalf_of,
            called_party=called_party,
            producer_name=producer_name,
            transfer_to_name=producer_name if transfer_number else "",
            transfer_number=transfer_number,
        )
        first_sentence = _normalize_spoken(
            _build_first_sentence(instruction, producer_name))
        voicemail_message = _normalize_spoken(
            _build_voicemail_message(instruction, producer_name))
    metadata = {"task_id": task_id, "applicant_id": applicant_id, "source": "robie-call-task",
                "transfer_to": producer_name if transfer_number else None,
                "transfer_phone_number": transfer_number or None,
                "on_behalf_of": on_behalf_of,
                "called_party": called_party or applicant_name or None,
                "on_behalf_of_producer": producer_name,
                "workflow": workflow.id if workflow is not None else None}

    # ---- 5c. Daily hard cap (Jake, Oct 7 2026: 5 calls on day one) -------
    # Counted per America/New_York day before anything is dialed. Dry runs
    # count in their own bucket so a rehearsal never uses up live calls.
    from .call_pickup import daily_call_cap

    cap = daily_call_cap()
    cap_store = getattr(ports, "daily_cap", None)
    if cap is not None and cap_store is not None:
        cap_day = calling_day(_calling_now(config))
        bucket = f"dry:{cap_day}" if config.dry_run else cap_day
        used = cap_store.count(bucket)
        if used >= cap:
            log.warning("daily call cap reached (%s of %s); not dialing task %s",
                        used, cap, task_id)
            cap_note = (
                "Robie did not call because today's limit of "
                f"{cap} automated calls was already reached. "
                f"{_redo_hint(task)} on the next business day and Robie will "
                "call then."
            )
            wb = _writeback_once(
                ports, task_id, "daily_cap_reached",
                applicant_id, cap_note, title_hint=None)
            _mark_processed(task_id)
            _mark_content_processed(applicant_id, instruction)
            return fail(
                f"daily call cap reached ({used} of {cap}); not dialed",
                writeback=wb,
                daily_cap_reached=True,
            )
        cap_store.record(bucket)

    # ---- 6a. Durable call intent (BEFORE the dial) --------------------------
    # Save the intent to dial BEFORE the Bland POST. If the POST times out,
    # the process crashes, or the checkpoint write after the dial fails,
    # the next run sees this intent and RECONCILES (checks Bland recent
    # calls for this number) instead of dialing again. A timeout or failed
    # checkpoint must trigger reconciliation, never an automatic redial.
    # The intent is cleared only when the dial is confirmed or reconciled.
    if not config.dry_run:
        try:
            _merge_checkpoint(ports, task_id, {
                "bland_call_ids": [],
                "phone": phone,
                "completed_at": None,
                "dial_intent": {
                    "attempted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "phone": phone,
                    "status": "dial_attempted",
                },
            })
        except CheckpointError as exc:
            # The intent is the restart-safety net: without it, a timeout
            # during the Bland POST would leave no trace and the next run
            # would dial the client again. A failed intent save fails the
            # task BEFORE dialing — zero Bland calls.
            log.error("dial intent checkpoint failed for task %s: %s — NOT dialing",
                      task_id, exc)
            _chat_alert(
                ports, config,
                f"Robie Call BLOCKED for applicant {applicant_id} (task "
                f"{task_id}): the dial-intent checkpoint could not be saved "
                f"({exc}). No call was placed. Task left OPEN — fix the "
                "checkpoint store and re-run.",
            )
            return fail(
                f"dial intent checkpoint failed ({exc}); NOT dialing — "
                "task left open",
                checkpoint_failed=True,
                chat_alerted=True,
            )
        log.info("durable dial intent saved for task %s (phone %s)",
                 task_id, "***")

    if config.dry_run:
        # A dry run dials nothing and files nothing. It must not write a
        # lost-outcome note for a call that never left.
        log.info("[DRY_RUN] would place call to applicant %s; nothing written",
                 applicant_id)
        _mark_processed(task_id)
        _mark_content_processed(applicant_id, instruction)
        return {
            "ok": True,
            "task_id": task_id,
            "applicant_id": applicant_id,
            "call": {
                "success": True,
                "dry_run": True,
                "call_ids": [],
                "attempts": [],
                "voicemail_hit": False,
                "redialed": False,
                "recording_url": None,
                "error": None,
            },
            "writeback": {
                "status": "dry_run",
                "note_id": None,
                "discussion_id": None,
                "reason": "dry run: nothing written",
            },
            "recording": None,
            "reassigned": False,
            "chat_alerted": False,
            "outcome_verified": False,
            "outcome_successful": False,
            "error": None,
        }
    else:
        # Single attempt: the Bland port applies the double-dial internally.
        # We deliberately do NOT retry the POST — Bland has no idempotency key
        # on /v1/calls, so a retried timeout could double-dial the client.
        # A failed task stays OPEN and is redelivered on the next intake
        # run, with human visibility via the chat alert below.
        dedupe = getattr(ports, "call_dedupe", None)
        if dedupe is not None:
            # Same-day key is the Eastern day. A Created Date is converted
            # from Central first; otherwise the key is the Eastern call day.
            day = calling_day(created_et) if created_et is not None else calling_day(
                _calling_now(config)
            )
            dedupe_key = note_dedupe_key(
                applicant_id, _pick(task, "discussion_id"), instruction, task_id,
            )
            if dedupe.already_called(dedupe_key, day):
                log.info(
                    "task %s already called for this note today; not dialing",
                    task_id,
                )
                wb = _writeback_once(
                    ports, task_id, "same_day_dedupe", applicant_id,
                    "Robie did not call again. This note was already called today.",
                    title_hint=None,
                )
                _mark_processed(task_id)
                _mark_content_processed(applicant_id, instruction)
                return {
                    "ok": True,
                    "task_id": task_id,
                    "applicant_id": applicant_id,
                    "call": {"success": False, "call_ids": []},
                    "writeback": wb,
                    "recording": None,
                    "reassigned": False,
                    "chat_alerted": False,
                    "error": None,
                    "duplicate_suppressed": True,
                }
            dedupe.record(dedupe_key, day)
        try:
            call_result = ports.bland.place_call_with_double_dial(
                phone, eva_task, first_sentence, voicemail_message, metadata
            )
        except Exception as exc:  # noqa: BLE001
            call_result = {"success": False, "error": str(exc)[:300], "call_ids": []}

    if opt_outs is not None and pressed_opt_out(call_result):
        opt_outs.record_opt_out(applicant_id, source="press-6")
        log.info("applicant %s opted out of automated calls", applicant_id)

    tripped = _bland_record(bool(call_result.get("success")))
    if tripped:
        _chat_alert(
            ports, config,
            "Bland API circuit breaker tripped — calls paused for 120s. "
            f"Last task: {task_id}, applicant {applicant_id}.",
        )

    if not call_result.get("success"):
        err = call_result.get("error") or "Bland call failed"
        chat_alerted = _chat_alert(
            ports, config,
            f"Robie Call FAILED for applicant {applicant_id} (task {task_id}): {err}. "
            "Task left OPEN for human review — not reassigned.",
        )
        return fail(f"Bland call failed: {err}", call=call_result, chat_alerted=chat_alerted)

    call_ids = call_result.get("call_ids") or []
    log.info("call placed for applicant %s: %s", applicant_id, call_ids)

    # ---- 7. Checkpoint IMMEDIATELY after the dial ---------------------------
    # A crash anywhere below must reconcile, never redial. This is the
    # durable write; the in-memory marks at the end are the fast path.
    # The dial_intent from step 6a is cleared — we now have confirmed call_ids.
    # Merge (not replace) so notes_filed entries recorded earlier survive.
    #
    # A failed write here is a real failure, not a warning: without the
    # call_ids on disk, a crash before the note is filed would leave the
    # next run with only the 5b recent-calls guard. Fail loudly — the next
    # run reconciles via recent_calls instead of redialing.
    try:
        _merge_checkpoint(ports, task_id, {
            "bland_call_ids": list(call_ids),
            "phone": phone,
            "completed_at": None,
            "dial_intent": None,
        })
    except CheckpointError as exc:
        log.error("post-dial checkpoint failed for task %s: %s", task_id, exc)
        chat_alerted = _chat_alert(
            ports, config,
            f"Robie Call PLACED but checkpoint FAILED for applicant "
            f"{applicant_id} (task {task_id}, call IDs "
            f"{', '.join(call_ids) or 'n/a'}): {exc}. The call went out; "
            "the next run will reconcile via recent-calls instead of "
            "redialing. Task left OPEN for human review.",
        )
        return fail(
            f"call placed but post-dial checkpoint failed ({exc}); task "
            "left open — next run reconciles, never redials",
            call=call_result,
            checkpoint_failed=True,
            chat_alerted=chat_alerted,
        )

    # ---- 8. Outcome verification --------------------------------------------
    # The placement ack is not proof. Poll Bland for a terminal status.
    if config.dry_run:
        outcome: Dict[str, Any] = {
            "verified": True, "successful": True, "available": True,
            "statuses": {}, "source": "dry_run",
        }
    else:
        outcome = _verify_call_outcome(
            ports.bland, call_ids, config,
            redialed=bool(call_result.get("redialed")))
    call_result = _attach_verified_status(call_result, outcome)
    outcome_verified = bool(outcome.get("verified"))
    outcome_successful = bool(outcome.get("successful"))
    log.info("outcome verification for task %s: verified=%s successful=%s available=%s",
             task_id, outcome_verified, outcome_successful, outcome.get("available"))

    if not outcome_verified:
        # The call went out but Bland never confirmed a terminal status.
        # File the note honestly (the "unknown" verdict), alert, fail
        # closed. NO reassignment — a human must review. The checkpoint
        # keeps the call_ids so the next cycle reconciles instead of
        # redialing.
        note_body = _format_outcome_note(
            applicant_name, note_topic, call_result, "skipped",
            phone_mismatch=phone_mismatch, name_mismatch=name_mismatch,
            producer_name=producer_name, called_party=called_party,
            phone_choice=phone_choice,
        )
        writeback = _writeback_once(
            ports, task_id, "outcome_unverified",
            applicant_id, note_body, title_hint=None
        )
        chat_alerted = _chat_alert(
            ports, config,
            f"Robie Call placed but OUTCOME UNVERIFIED for applicant "
            f"{applicant_id} (task {task_id}, call IDs "
            f"{', '.join(call_ids) or 'n/a'}). Bland never confirmed a "
            f"terminal status. Task left OPEN for human review — not reassigned.",
        )
        _mark_processed(task_id)
        _mark_content_processed(applicant_id, instruction)
        return fail(
            "call placed but outcome could not be verified with Bland; "
            "task left open",
            call=call_result,
            writeback=writeback,
            outcome_verified=False,
            outcome_verification_available=outcome.get("available"),
            chat_alerted=chat_alerted,
        )

    # A transferred call is verified (we know what happened) but never
    # task success — Eva handed a live call to the assigner. It must not
    # fall into the "ended without success" branch below.
    transferred = _outcome_was_transferred(outcome)
    if transferred:
        log.info("call transferred to %s for task %s", assigned_by, task_id)

    if outcome_verified and not outcome_successful and not transferred:
        # The call ENDED but did NOT succeed (failed, busy, no-answer,
        # canceled). This is not task completion — file the note honestly,
        # alert, fail closed. NO reassignment. The checkpoint keeps the
        # call_ids so a restart reconciles instead of redialing.
        terminal_statuses = [
            f"{cid}: {(outcome.get('statuses') or {}).get(cid, {}).get('status')}"
            for cid in call_ids
        ]
        note_body = _format_outcome_note(
            applicant_name, note_topic, call_result, "skipped",
            phone_mismatch=phone_mismatch, name_mismatch=name_mismatch,
            producer_name=producer_name, called_party=called_party,
            phone_choice=phone_choice,
        )
        writeback = _writeback_once(
            ports, task_id, "outcome_unsuccessful",
            applicant_id, note_body, title_hint=None
        )
        contact = outcome.get("contact") or cc.MISS
        reached_line = {
            cc.RECORDING: "Nobody was reached: only a recording or automated "
                          "phone system answered.",
            cc.VOICEMAIL_NO_MESSAGE: "Nobody was reached: voicemail "
                                     "greeting, no message left.",
            cc.UNCONFIRMED: "The line connected, but nothing shows a live "
                            "person was reached.",
        }.get(contact, "The call did not connect.")
        chat_alerted = _chat_alert(
            ports, config,
            f"Robie Call ENDED WITHOUT SUCCESS for applicant "
            f"{applicant_id} (task {task_id}, {'; '.join(terminal_statuses)}). "
            f"{reached_line} Task left OPEN for human review — "
            f"not reassigned.",
        )
        _mark_processed(task_id)
        _mark_content_processed(applicant_id, instruction)
        if contact == cc.MISS:
            error = ("call ended without success (failed/busy/no-answer/"
                     "canceled); task left open")
        else:
            error = (f"call ended without success (no live person reached: "
                     f"{contact}); task left open")
        return fail(
            error,
            call=call_result,
            writeback=writeback,
            outcome_verified=True,
            outcome_successful=False,
            outcome_verification_available=outcome.get("available"),
            contact=contact,
            chat_alerted=chat_alerted,
        )

    # ---- 9. Recording upload (best-effort, optional) ------------------------
    recording: Optional[Dict[str, Any]] = None
    recording_status = "skipped"
    recording_url = call_result.get("recording_url")
    if recording_url and ports.recording_upload is not None and not config.dry_run:
        try:
            recording = _retryable_call(
                lambda: ports.recording_upload.upload_recording(  # type: ignore[union-attr]
                    applicant_id, f"robie-call-{task_id}.mp3", recording_url
                ),
                what=f"recording upload task {task_id}",
            )
            recording_status = "ok" if recording.get("ok") else "failed"
        except Exception as exc:  # noqa: BLE001
            recording = {"ok": False, "error": str(exc)[:200]}
            recording_status = "failed"
            log.warning("recording upload failed: %s", exc)
    elif recording_url:
        recording_status = "pending"

    # ---- 10. Writeback ------------------------------------------------------
    # Transferred calls (Eva handed a frustrated caller to the assigner) get
    # the structured transfer note — why, outcome, call notes — and are never
    # ok: the human now owns the objective.
    if transferred:
        note_body = _format_transfer_note(
            applicant_name, note_topic, call_result, assigned_by,
            producer_name=producer_name)
    else:
        note_body = _format_outcome_note(
            applicant_name, note_topic, call_result, recording_status,
            phone_mismatch=phone_mismatch, name_mismatch=name_mismatch,
            producer_name=producer_name, called_party=called_party,
            phone_choice=phone_choice,
        )
    return _finalize_call(
        task_id=task_id,
        applicant_id=applicant_id,
        applicant_name=applicant_name,
        assigned_by=assigned_by,
        instruction=instruction,
        call_result=call_result,
        recording=recording,
        note_body=note_body,
        outcome=outcome,
        outcome_verified=outcome_verified,
        outcome_successful=outcome_successful,
        phone=phone,
        config=config,
        ports=ports,
        log=log,
        fail=fail,
        transferred=transferred,
    )


def _finalize_call(
    *,
    task_id: str,
    applicant_id: str,
    applicant_name: str,
    assigned_by: str,
    instruction: str,
    call_result: Dict[str, Any],
    recording: Optional[Dict[str, Any]],
    note_body: str,
    outcome: Dict[str, Any],
    outcome_verified: bool,
    outcome_successful: bool = True,
    phone: Optional[str],
    config: RobieCallConfig,
    ports: RobieCallPorts,
    log: logging.LoggerAdapter,
    fail: Callable[..., Dict[str, Any]],
    transferred: bool = False,
) -> Dict[str, Any]:
    """Shared tail: writeback -> reassign (with read-back) -> mark -> result.

    Used by both the normal path and the crash-recovery path.

    transferred: Eva handed the live call to the assigner (frustration
    path). The note body is the structured transfer note; ok is False even
    when the call verified — Roby did not complete the objective, the
    human now owns it. Reassignment back to the assigner still runs.
    """
    call_ids = call_result.get("call_ids") or []
    writeback = _writeback_once(
        ports, task_id, "outcome_transferred" if transferred else "outcome",
        applicant_id, note_body, title_hint=None
    )
    wb_ok = writeback.get("status") in ("filed", "dry_run")
    chat_alerted = False
    if not wb_ok:
        # Call happened but there's no EZLynx record: silent data loss. Alert now.
        chat_alerted = _chat_alert(
            ports, config,
            f"Bland call COMPLETED but EZLynx writeback FAILED. "
            f"Applicant {applicant_id}, task {task_id}, call IDs: "
            f"{', '.join(call_ids) or 'n/a'}. Manual note may be needed.",
        )
        log.error("writeback failed after successful call: %s", writeback.get("reason") or writeback.get("error"))

    # ---- Reassign (with pre-check + read-back) --------------------------------
    # reassigned=True ONLY when ALL of these hold:
    #   1. The task is currently assigned to Robie (driver ownership check).
    #      Never steal a task that a human already moved elsewhere.
    #   2. reassign_task(task_id, ...) reports ok for the EXACT task ID.
    #   3. read_task_assignee(task_id) for that SAME task ID reads back as
    #      the intended assignee.
    # A mismatch at any step fires a chat alert: the task may be sitting
    # with the wrong person.
    reassigned = False
    reassign_error: Optional[str] = None
    if wb_ok and assigned_by and ports.task_reassign is not None and not config.dry_run:
        try:
            read_back = getattr(ports.task_reassign, "read_task_assignee", None)
            # Step 1: verify driver ownership BEFORE touching the task.
            if callable(read_back):
                try:
                    pre_assignee = read_back(task_id)
                except Exception as exc:  # noqa: BLE001
                    pre_assignee = None
                    log.warning("pre-reassign assignee read failed for %s: %s",
                                task_id, exc)
                if pre_assignee is not None:
                    owned = "robie" in (pre_assignee or "").strip().lower()
                    if not owned:
                        reassign_error = (
                            "task not owned by Robie (currently assigned to "
                            f"{pre_assignee!r}); refusing to reassign"
                        )
                        log.error("task %s: %s", task_id, reassign_error)
                        chat_alerted = _chat_alert(
                            ports, config,
                            f"Task {task_id} reassignment REFUSED: the task is "
                            f"currently assigned to {pre_assignee!r}, not Robie. "
                            f"A human may have moved it. Not touching it.",
                        ) or chat_alerted
            else:
                log.warning("task %s: no read_task_assignee available; "
                            "skipping pre-reassign ownership check", task_id)
            # Step 2: reassign by exact task ID (only if ownership held).
            if reassign_error is None:
                r = _retryable_call(
                    lambda: ports.task_reassign.reassign_task(  # type: ignore[union-attr]
                        task_id,
                        assigned_by,
                        "Robie completed the call task. Outcome note filed in EZLynx.",
                    ),
                    what=f"task reassign {task_id}",
                )
                if not r.get("ok"):
                    reassign_error = str(r.get("error") or "reassign returned not-ok")[:200]
                elif not callable(read_back):
                    reassign_error = (
                        "reassignment claimed by port but NOT verified "
                        "(no read-back available); left for human confirmation"
                    )
                    log.warning("task %s: %s", task_id, reassign_error)
                else:
                    # Step 3: read back that SAME task ID to confirm.
                    try:
                        current_assignee = read_back(task_id)
                    except Exception as exc:  # noqa: BLE001
                        current_assignee = None
                        log.warning("assignee read-back failed for %s: %s", task_id, exc)
                    if ((current_assignee or "").strip().lower()
                            == assigned_by.strip().lower()):
                        reassigned = True
                        log.info("task %s reassignment verified: %r",
                                 task_id, current_assignee)
                    else:
                        reassign_error = (
                            "reassign claimed ok but assignee reads back as "
                            f"{current_assignee!r}, expected {assigned_by!r}"
                        )
                        log.error("task %s: %s", task_id, reassign_error)
                        chat_alerted = _chat_alert(
                            ports, config,
                            f"Task {task_id} reassignment MISMATCH: the call "
                            f"completed and the outcome note was filed, but the "
                            f"task assignee reads back as {current_assignee!r} "
                            f"instead of {assigned_by!r}. A human should confirm "
                            f"where the task sits.",
                        ) or chat_alerted
        except Exception as exc:  # noqa: BLE001
            reassign_error = str(exc)[:200]
            log.warning("task reassignment failed: %s", exc)
    elif wb_ok and assigned_by and ports.task_reassign is None:
        reassign_error = "no TaskReassignmentPort wired; task left for manual reassignment"
        log.warning("%s", reassign_error)

    _mark_processed(task_id)
    _mark_content_processed(applicant_id, instruction)

    # ok requires the VERIFIED call outcome AND the filed note. No opt-out:
    # a placement ack (HTTP 200) is never proof the call happened. The task
    # is done only when Bland confirms a terminal status AND that status
    # shows a real connection.
    # A transferred call is never ok: Eva handed a live call to the assigner,
    # so Roby did not complete the objective — the human now owns it.
    # Reassignment is reported separately (reassigned + reassign_error) — a
    # routing problem must not masquerade as a call failure, nor vice versa.
    # CRITICAL: verified alone is not enough — the call must have SUCCEEDED
    # (connected). A verified "failed"/"busy"/"no-answer" is not task success.
    ok = wb_ok and outcome_verified and outcome_successful and not transferred
    if ok:
        error = None
    elif transferred:
        error = (f"call transferred to {assigned_by or 'the task assigner'}; "
                 "task returned for human follow-up")
    elif not outcome_verified:
        error = "call outcome unverified"
    elif not outcome_successful:
        error = "call ended without success"
    else:
        error = writeback.get("reason") or writeback.get("error") or "writeback failed"

    # Mark the checkpoint complete ONLY on real completion. A failed task
    # keeps its call_ids with completed_at=None so the next cycle
    # reconciles instead of treating it as done. Merge (not replace) so
    # the notes_filed write-once record survives.
    #
    # A failed completion write is a real failure: reporting ok=True while
    # the checkpoint still shows incomplete would let a later run redo work
    # (re-file notes, re-attempt reassignment). Fail loudly instead.
    try:
        _merge_checkpoint(ports, task_id, {
            "bland_call_ids": list(call_ids),
            "phone": phone,
            "completed_at": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if ok else None
            ),
        })
    except CheckpointError as exc:
        log.error("completion checkpoint failed for task %s: %s", task_id, exc)
        chat_alerted = _chat_alert(
            ports, config,
            f"Robie Call finished for applicant {applicant_id} (task "
            f"{task_id}) but the completion checkpoint could not be saved "
            f"({exc}). The call outcome was {outcome.get('statuses')}, but "
            "the task is left OPEN so a human can confirm nothing is lost.",
        ) or chat_alerted
        return fail(
            f"completion checkpoint failed ({exc}); task left open",
            call=call_result,
            writeback=writeback,
            outcome_verified=outcome_verified,
            outcome_successful=outcome_successful,
            outcome_verification_available=outcome.get("available"),
            checkpoint_failed=True,
            chat_alerted=chat_alerted,
        )

    result = {
        "ok": ok,
        "task_id": task_id,
        "applicant_id": applicant_id,
        "call": call_result,
        "writeback": writeback,
        "recording": recording,
        "reassigned": reassigned,
        "reassign_error": reassign_error,
        "chat_alerted": chat_alerted,
        "outcome_verified": outcome_verified,
        "outcome_successful": outcome_successful,
        "outcome_verification_available": outcome.get("available"),
        "contact": outcome.get("contact"),
        "error": error,
    }
    if call_result.get("recovered_from_checkpoint"):
        result["recovered_from_checkpoint"] = True
    log.info("task complete: ok=%s reassigned=%s outcome_verified=%s outcome_successful=%s",
             ok, reassigned, outcome_verified, outcome_successful)
    return result


def _recover_interrupted_call(
    task: Dict[str, Any],
    config: RobieCallConfig,
    ports: RobieCallPorts,
    log: logging.LoggerAdapter,
    fail: Callable[..., Dict[str, Any]],
    task_id: str,
    applicant_id: str,
    applicant_name: str,
    assigned_by: str,
    instruction: str,
    recovered_ids: List[str],
    checkpoint: Dict[str, Any],
) -> Dict[str, Any]:
    """Reconcile a previous run that dialed but never finished.

    NEVER dials. Polls Bland for the known call_ids, files the outcome
    note honestly, then runs the shared finalize tail (reassign with
    read-back). Fails closed when the outcome stays unverifiable.
    """
    if config.dry_run:
        outcome: Dict[str, Any] = {
            "verified": True, "successful": True, "available": True,
            "statuses": {}, "source": "dry_run",
        }
    else:
        outcome = _verify_call_outcome(ports.bland, recovered_ids, config)
    call_result: Dict[str, Any] = {
        "success": True,
        "recovered_from_checkpoint": True,
        "call_ids": list(recovered_ids),
        "attempts": [],
        "voicemail_hit": False,
        "redialed": False,
        "recording_url": None,
        "error": None,
    }
    call_result = _attach_verified_status(call_result, outcome)
    outcome_verified = bool(outcome.get("verified"))
    outcome_successful = bool(outcome.get("successful"))
    log.info("recovery for task %s: outcome verified=%s successful=%s",
             task_id, outcome_verified, outcome_successful)

    producer_name = _pick(task, "assigned_producer")
    called_party = ""
    callee = _named_callee(instruction)
    if callee and not _name_overlaps(callee, applicant_name):
        called_party = callee
    note_body = _format_recovery_note(
        applicant_name, instruction, call_result,
        producer_name=producer_name, called_party=called_party)

    if not outcome_verified:
        writeback = _writeback_once(
            ports, task_id, "recovery_unverified",
            applicant_id, note_body, title_hint=None
        )
        chat_alerted = _chat_alert(
            ports, config,
            f"Recovered Robie Call for applicant {applicant_id} (task "
            f"{task_id}): a previous attempt dialed (call IDs "
            f"{', '.join(recovered_ids)}), but the outcome still cannot be "
            f"verified with Bland. Task left OPEN for human review.",
        )
        _mark_processed(task_id)
        _mark_content_processed(applicant_id, instruction)
        return fail(
            "recovered call but outcome unverifiable; task left open",
            call=call_result,
            writeback=writeback,
            outcome_verified=False,
            outcome_verification_available=outcome.get("available"),
            recovered_from_checkpoint=True,
            chat_alerted=chat_alerted,
        )

    if outcome_verified and not outcome_successful:
        # Recovered call ended but did NOT succeed. File honestly, alert,
        # fail closed. Task left OPEN.
        writeback = _writeback_once(
            ports, task_id, "recovery_unsuccessful",
            applicant_id, note_body, title_hint=None
        )
        chat_alerted = _chat_alert(
            ports, config,
            f"Recovered Robie Call for applicant {applicant_id} (task "
            f"{task_id}): the previous attempt's call ENDED WITHOUT SUCCESS "
            f"(no live person reached: {outcome.get('contact') or 'miss'}). "
            f"Task left OPEN for human review.",
        )
        _mark_processed(task_id)
        _mark_content_processed(applicant_id, instruction)
        return fail(
            "recovered call ended without success; task left open",
            call=call_result,
            writeback=writeback,
            outcome_verified=True,
            outcome_successful=False,
            outcome_verification_available=outcome.get("available"),
            recovered_from_checkpoint=True,
            chat_alerted=chat_alerted,
        )

    return _finalize_call(
        task_id=task_id,
        applicant_id=applicant_id,
        applicant_name=applicant_name,
        assigned_by=assigned_by,
        instruction=instruction,
        call_result=call_result,
        recording=None,
        note_body=note_body,
        outcome=outcome,
        outcome_verified=outcome_verified,
        phone=checkpoint.get("phone"),
        config=config,
        ports=ports,
        log=log,
        fail=fail,
    )
