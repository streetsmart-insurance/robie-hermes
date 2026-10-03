"""Freeform "Robie Call" task handler.

When agency staff assign an EZLynx task to "Robie AI" with a freeform call
instruction (e.g. "Please call John about his renewal"), the 30-minute
"Robie AI - Task Check-In" Looker schedule emails a CSV of Robie's open
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
    #      - BlandCallPort: HTTP POST https://api.bland.ai/v1/calls with the
    #        Jake-spec payload (see robie_job_engine.bland_config).
    #        Optional get_call_status(call_id) lets the handler VERIFY the
    #        outcome instead of trusting the placement ack; without it the
    #        handler refuses to mark the task ok (unless
    #        require_outcome_verification=False is set explicitly).
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
  and are redelivered on the next 30-min report cycle.
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

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol

from .bland_config import (
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

TRANSFER_NUMBER = "+17324622360"  # Carlo's direct line for live transfers

CALL_KEYWORDS = ("call", "phone", "dial", "ring", "callback", "call back")

# Word-boundary regex for call keywords. Substring matching caused false
# positives: "recall the policy" (contains "call"), "morning meeting"
# (contains "ring"), "telephone" (contains "phone"). A mistaken "call task"
# could dial a client for a non-call task — the exact trust violation
# Carlo wants eliminated.
_CALL_KEYWORD_RE = re.compile(
    r"\b(call|phone|dial|ring|callback|call\s+back)\b",
    re.IGNORECASE,
)

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


@dataclass
class RobieCallConfig:
    """Configuration for the freeform call handler."""

    dry_run: bool = True  # Fail-closed default: nothing dials unless opted in
    health_chat_webhook_url: Optional[str] = None
    redial_delay_seconds: int = 10
    max_attempts: int = 2
    request_timeout_s: int = 30
    # Outcome verification: the task is ok only on a VERIFIED terminal Bland
    # status, never on the placement ack alone. Set False only explicitly
    # (auditable) when the Bland port has no status API.
    require_outcome_verification: bool = True
    outcome_poll_tries: int = 6
    outcome_poll_interval_s: int = 10


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
# Two overlapping runs (30-min loop + manual trigger) must not dial twice.
_inflight_tasks: set = set()
_bland_failures: int = 0
_bland_circuit_open_until: float = 0.0


def _reset_module_state_for_tests() -> None:
    """Test seam only: clear idempotency + circuit state."""
    global _processed_tasks, _processed_content
    global _bland_failures, _bland_circuit_open_until
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
    "due_date": ["Task Due Date", "Due Date", "due_date"],
}


def _pick(task: Dict[str, Any], logical: str) -> str:
    for key in _TASK_FIELD_ALIASES[logical]:
        if key in task and task[key] not in (None, ""):
            return str(task[key]).strip()
    return ""


def is_call_task(task: Dict[str, Any]) -> bool:
    """True when the task subject/description asks for a phone call.

    Uses word-boundary matching: "recall the policy" and "morning meeting"
    are NOT call tasks (substring matching caused false positives that
    could have dialed a client for a non-call task).
    """
    text = _pick(task, "subject") + " " + _pick(task, "description")
    return bool(_CALL_KEYWORD_RE.search(text))


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


_PHONE_LIKE_RE = re.compile(
    r"(\+?1?[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4})")


def _phones_in_text(text: str) -> List[str]:
    """Extract phone-like numbers from free text, normalized to digits."""
    return ["".join(c for c in m if c.isdigit())
            for m in _PHONE_LIKE_RE.findall(text or "")]


def _scrub_phones(text: str) -> str:
    """Replace phone-like values with [phone number] (DiscussionApi rejects digits)."""
    return _PHONE_LIKE_RE.sub("[phone number]", text or "")


def _instruction_phone_mismatch(instruction: str, dialed_phone: str) -> Optional[str]:
    """Detect when the task text names a different number than EZLynx.

    EZLynx wins (it's the system of record), but the mismatch is flagged
    for the note — a sloppy human may have typed a wrong number, and staff
    should know which one was actually dialed.
    """
    dialed_digits = "".join(c for c in str(dialed_phone or "") if c.isdigit())
    for found in _phones_in_text(instruction):
        # Compare on last 10 digits (formatting/country-code differences).
        if (found and dialed_digits and len(found) >= 10
                and len(dialed_digits) >= 10
                and found[-10:] != dialed_digits[-10:]):
            return found
    return None


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


def _load_checkpoint(ports: RobieCallPorts, task_id: str) -> Dict[str, Any]:
    """Read the durable checkpoint. {} when the port is absent or unreadable."""
    if ports.job_checkpoint is None:
        return {}
    try:
        data = ports.job_checkpoint.get_checkpoint(_checkpoint_key(task_id))
        return dict(data or {})
    except Exception as exc:  # noqa: BLE001 - checkpoint is best-effort
        logger.warning("checkpoint read failed for %s: %s", task_id, exc)
        return {}


def _save_checkpoint(ports: RobieCallPorts, task_id: str, value: Dict[str, Any]) -> None:
    """Persist the checkpoint. Best-effort: logs loudly on failure."""
    if ports.job_checkpoint is None:
        return
    try:
        ports.job_checkpoint.set_checkpoint(_checkpoint_key(task_id), dict(value))
    except Exception as exc:  # noqa: BLE001
        logger.error("checkpoint WRITE failed for %s: %s — restart may redial",
                     task_id, exc)


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


def _call_was_connected(status_result: Dict[str, Any]) -> bool:
    """Did this terminal call status represent an actual successful connection?

    A call that ended as "failed", "busy", "no-answer", or "canceled" is
    terminal but NOT successful. Only "completed" with evidence of actual
    connection (answered_by human/voicemail, or positive duration) counts.
    This separates "call ended" from "task succeeded."
    """
    if not status_result.get("terminal"):
        return False
    status = str(status_result.get("status") or "").lower()
    if status not in SUCCESSFUL_TERMINAL_STATUSES:
        return False
    # "completed" needs evidence of actual connection
    answered = str(status_result.get("answered_by") or "").lower()
    if answered in ("human", "voicemail"):
        return True
    try:
        dur = float(status_result.get("duration")
                    or status_result.get("duration_s")
                    or status_result.get("call_duration") or 0)
    except (TypeError, ValueError):
        dur = 0
    return dur > 0


def _verify_call_outcome(
    bland_port: Any, call_ids: List[str], config: RobieCallConfig
) -> Dict[str, Any]:
    """Verify every placed call_id against Bland.

    Returns {"verified": bool, "successful": bool, "available": bool,
             "statuses": {call_id: poll-result}}.
    verified=True when at least one call reached a terminal status.
    successful=True only when at least one call reached a SUCCESSFUL
    terminal status with evidence of actual connection. A call that ended
    as failed/busy/no-answer/canceled is verified (we know it ended) but
    NOT successful (it must not count as task completion).
    """
    statuses: Dict[str, Dict[str, Any]] = {}
    available = False
    for cid in call_ids:
        res = _poll_call_status(bland_port, cid, config)
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
) -> str:
    """Outcome note for a call recovered after an interruption.

    States plainly that Robie is reconciling a previous attempt, then the
    verified outcome. Never contains a phone number.
    """
    who = applicant_name or "the client"
    topic = _scrub_phones((instruction or "")[:120].strip())
    verdict = _format_outcome_note(
        applicant_name, instruction, call_result, "skipped")
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
) -> Dict[str, Any]:
    """The exact POST /v1/calls body the BlandCallPort must send per attempt.

    This is the contract the worker's production Bland wiring implements.
    Attempt 1: voicemail_action=hangup (silent). Attempt 2: leave_message
    with the full slow voicemail_message.
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
        "transfer_phone_number": TRANSFER_NUMBER,
        "max_duration": 12,  # minutes
    }
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

def _build_eva_task(instruction: str, applicant_name: str) -> str:
    """Eva's task prompt: AI disclosure + freeform instruction + screener rules."""
    policy = BlandRedialPolicy()
    name_bit = f" The client is {applicant_name}." if applicant_name else ""
    return (
        f"{policy.build_intro(instruction.strip())}{name_bit} "
        f"{BlandCallConfig().screener_instructions} "
        f"If the call is transferred, transfer to {TRANSFER_NUMBER}."
    )


def _build_first_sentence(instruction: str) -> str:
    policy = BlandRedialPolicy()
    return policy.build_intro(instruction.strip())


def _build_voicemail_message(instruction: str) -> str:
    policy = BlandRedialPolicy()
    # One-sentence reason for the message; the policy adds AI disclosure + callback.
    reason = instruction.strip().split(".")[0][:160]
    return policy.build_voicemail_message(reason or "following up on your account")


# ---------------------------------------------------------------------------
# Writeback (API-only, allowlist-gated, no phone numbers in notes)
# ---------------------------------------------------------------------------

def _writeback_outcome_note(
    discussion_client: Any,
    applicant_id: str,
    body: str,
    title_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """Append the outcome note via the repo-standard fail-closed path."""
    from .ezlynx_discussions import file_note_to_existing_discussion

    try:
        return file_note_to_existing_discussion(
            discussion_client,
            applicant_id,
            body[:MAX_NOTE_CHARS],
            title_hint=title_hint,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced in result dict
        logger.error("writeback failed for applicant %s: %s", applicant_id, exc)
        return {"status": "error", "error": str(exc)[:300], "discussion_id": None, "note_id": None}


def _merge_checkpoint(ports: RobieCallPorts, task_id: str,
                      update: Dict[str, Any]) -> None:
    """Merge `update` into the existing checkpoint without clobbering it."""
    current = _load_checkpoint(ports, task_id)
    current.update(update)
    _save_checkpoint(ports, task_id, current)


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
    checkpoint = _load_checkpoint(ports, task_id)
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


def _format_outcome_note(
    applicant_name: str,
    instruction: str,
    call: Dict[str, Any],
    recording_status: str,
    phone_mismatch: Optional[str] = None,
    name_mismatch: Optional[str] = None,
) -> str:
    """Outcome note. Plain English, first line states the outcome.

    Never contains a phone number (DiscussionApi refuses them) — mismatched
    numbers from the task text are described, not quoted. Uncertainty
    (unknown answered_by, missing data) is stated plainly; the note never
    pretends an uncertain call went fine.
    """
    call_ids = ", ".join(call.get("call_ids") or []) or "n/a"
    attempts = call.get("attempts") or []
    who = applicant_name or "the client"
    first = who.split()[0]
    # The instruction goes into the note — scrub any phone numbers first
    # (the Discussion API rejects them).
    topic = _scrub_phones(instruction[:120].strip())

    # Verdict: did the call verifiably happen? A completed call with
    # duration connected, even if answered_by is "unknown" (we know it
    # ran; we just don't know who picked up).
    def _connected(a: Dict[str, Any]) -> bool:
        if not a.get("success"):
            return False
        final = a.get("final_status") or {}
        answered = str(final.get("answered_by") or "").lower()
        if answered in ("human", "voicemail"):
            return True
        if str(final.get("status") or "").lower() == "completed":
            try:
                dur = float(final.get("duration") or
                            final.get("call_duration") or 0)
            except (TypeError, ValueError):
                dur = 0
            if dur > 0:
                return True
        return False

    connected = any(_connected(a) for a in attempts)
    # "unknown" covers the placed-but-unverifiable case: Bland accepted the
    # dial (top-level success) but no attempt proves a connection. Only a
    # top-level failure renders as NOT successful.
    unknown = not connected and (
        bool(call.get("success")) or any(a.get("success") for a in attempts)
    )

    if connected:
        lines = [f"Called {who} about {topic}.",
                 "The call was successful."]
        # What happened, honestly.
        vm_hit = call.get("voicemail_hit")
        redialed = call.get("redialed")
        answered = ""
        for a in reversed(attempts):
            if a.get("success"):
                answered = str((a.get("final_status") or {}).get("answered_by")
                               or "").lower()
                break
        if redialed and vm_hit:
            lines.append("The first attempt went to voicemail, so we called "
                         "back and left a message.")
        elif vm_hit or answered == "voicemail":
            lines.append("The call went to voicemail.")
        elif answered == "human":
            lines.append(f"Spoke with {first}.")
        elif answered == "unknown" or not answered:
            lines.append("The call connected but we couldn't confirm whether "
                         "it reached the person or voicemail.")
    elif unknown:
        lines = [f"Attempted to call {who} about {topic}.",
                 "We couldn't confirm whether the call went through — the "
                 "phone system accepted the request but we lost track of the "
                 "outcome. No message was confirmed left."]
    else:
        err = str(call.get("error") or "the call did not connect")
        lines = [f"Attempted to call {who} about {topic}.",
                 f"The call was NOT successful: {err}.",
                 "No message was left."]

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
    if name_mismatch:
        lines.append(f"Note: the task mentioned {name_mismatch}, but the "
                     f"applicant on file is {who} — please check the right "
                     f"person was reached.")

    lines.append("Eva identified herself as an AI assistant calling for Jake "
                 "from StreetSmart Insurance.")
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

    Wrapper: claims the task in-flight so two overlapping runs (the 30-min
    loop plus a manual trigger) can never dial twice, then delegates.
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

    # ---- 3c. Ambiguity guard ------------------------------------------------
    # "call him" with no topic: do NOT guess. Leave the task open and file
    # a clarification note so staff can fix the instruction.
    ambiguity = _instruction_ambiguity(instruction)
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
    checkpoint = _load_checkpoint(ports, task_id)
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
                    instruction, [found_id], checkpoint,
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
        return fail(
            f"no dialable phone found for applicant {applicant_id}; failing closed"
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
            f"Eva identifies as an AI assistant for Jake from StreetSmart Insurance."
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
    phone_mismatch = _instruction_phone_mismatch(instruction, phone)
    name_mismatch = _instruction_name_mismatch(instruction, applicant_name)
    if name_mismatch:
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
    eva_task = _build_eva_task(instruction, applicant_name)
    first_sentence = _normalize_spoken(_build_first_sentence(instruction))
    voicemail_message = _normalize_spoken(_build_voicemail_message(instruction))
    metadata = {"task_id": task_id, "applicant_id": applicant_id, "source": "robie-call-task"}

    # ---- 6a. Durable call intent (BEFORE the dial) --------------------------
    # Save the intent to dial BEFORE the Bland POST. If the POST times out,
    # the process crashes, or the checkpoint write after the dial fails,
    # the next run sees this intent and RECONCILES (checks Bland recent
    # calls for this number) instead of dialing again. A timeout or failed
    # checkpoint must trigger reconciliation, never an automatic redial.
    # The intent is cleared only when the dial is confirmed or reconciled.
    if not config.dry_run:
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
        log.info("durable dial intent saved for task %s (phone %s)",
                 task_id, "***")

    if config.dry_run:
        log.info("[DRY_RUN] would place call to applicant %s", applicant_id)
        call_result: Dict[str, Any] = {
            "success": True,
            "dry_run": True,
            "call_ids": [],
            "attempts": [{"attempt": 1, "mode": "DRY_RUN"}, {"attempt": 2, "mode": "DRY_RUN"}],
            "voicemail_hit": False,
            "redialed": False,
            "recording_url": None,
            "error": None,
        }
    else:
        # Single attempt: the Bland port applies the double-dial internally.
        # We deliberately do NOT retry the POST — Bland has no idempotency key
        # on /v1/calls, so a retried timeout could double-dial the client.
        # A failed task stays OPEN and is redelivered on the next 30-min
        # report cycle, with human visibility via the chat alert below.
        try:
            call_result = ports.bland.place_call_with_double_dial(
                phone, eva_task, first_sentence, voicemail_message, metadata
            )
        except Exception as exc:  # noqa: BLE001
            call_result = {"success": False, "error": str(exc)[:300], "call_ids": []}

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
    _merge_checkpoint(ports, task_id, {
        "bland_call_ids": list(call_ids),
        "phone": phone,
        "completed_at": None,
        "dial_intent": None,
    })

    # ---- 8. Outcome verification --------------------------------------------
    # The placement ack is not proof. Poll Bland for a terminal status.
    if config.dry_run:
        outcome: Dict[str, Any] = {
            "verified": True, "available": True, "statuses": {},
            "source": "dry_run",
        }
    else:
        outcome = _verify_call_outcome(ports.bland, call_ids, config)
    call_result = _attach_verified_status(call_result, outcome)
    outcome_verified = bool(outcome.get("verified"))
    outcome_successful = bool(outcome.get("successful"))
    log.info("outcome verification for task %s: verified=%s successful=%s available=%s",
             task_id, outcome_verified, outcome_successful, outcome.get("available"))

    if config.require_outcome_verification and not outcome_verified:
        # The call went out but Bland never confirmed a terminal status.
        # File the note honestly (the "unknown" verdict), alert, fail
        # closed. NO reassignment — a human must review. The checkpoint
        # keeps the call_ids so the next cycle reconciles instead of
        # redialing.
        note_body = _format_outcome_note(
            applicant_name, instruction, call_result, "skipped",
            phone_mismatch=phone_mismatch, name_mismatch=name_mismatch,
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

    if outcome_verified and not outcome_successful:
        # The call ENDED but did NOT succeed (failed, busy, no-answer,
        # canceled). This is not task completion — file the note honestly,
        # alert, fail closed. NO reassignment. The checkpoint keeps the
        # call_ids so a restart reconciles instead of redialing.
        terminal_statuses = [
            f"{cid}: {(outcome.get('statuses') or {}).get(cid, {}).get('status')}"
            for cid in call_ids
        ]
        note_body = _format_outcome_note(
            applicant_name, instruction, call_result, "skipped",
            phone_mismatch=phone_mismatch, name_mismatch=name_mismatch,
        )
        writeback = _writeback_once(
            ports, task_id, "outcome_unsuccessful",
            applicant_id, note_body, title_hint=None
        )
        chat_alerted = _chat_alert(
            ports, config,
            f"Robie Call ENDED WITHOUT SUCCESS for applicant "
            f"{applicant_id} (task {task_id}, {'; '.join(terminal_statuses)}). "
            f"The call did not connect. Task left OPEN for human review — "
            f"not reassigned.",
        )
        _mark_processed(task_id)
        _mark_content_processed(applicant_id, instruction)
        return fail(
            "call ended without success (failed/busy/no-answer/canceled); "
            "task left open",
            call=call_result,
            writeback=writeback,
            outcome_verified=True,
            outcome_successful=False,
            outcome_verification_available=outcome.get("available"),
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
    note_body = _format_outcome_note(
        applicant_name, instruction, call_result, recording_status,
        phone_mismatch=phone_mismatch, name_mismatch=name_mismatch,
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
) -> Dict[str, Any]:
    """Shared tail: writeback -> reassign (with read-back) -> mark -> result.

    Used by both the normal path and the crash-recovery path.
    """
    call_ids = call_result.get("call_ids") or []
    writeback = _writeback_once(
        ports, task_id, "outcome",
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

    # ok requires the VERIFIED call outcome AND the filed note — unless the
    # worker explicitly opted out of verification (auditable config).
    # Reassignment is reported separately (reassigned + reassign_error) — a
    # routing problem must not masquerade as a call failure, nor vice versa.
    # CRITICAL: verified alone is not enough — the call must have SUCCEEDED
    # (connected). A verified "failed"/"busy"/"no-answer" is not task success.
    verification_required = bool(config.require_outcome_verification)
    ok = wb_ok and (outcome_verified or not verification_required) and outcome_successful
    if ok:
        error = None
    elif not outcome_verified and verification_required:
        error = "call outcome unverified"
    elif not outcome_successful:
        error = "call ended without success"
    else:
        error = writeback.get("reason") or writeback.get("error") or "writeback failed"

    # Mark the checkpoint complete ONLY on real completion. A failed task
    # keeps its call_ids with completed_at=None so the next cycle
    # reconciles instead of treating it as done. Merge (not replace) so
    # the notes_filed write-once record survives.
    _merge_checkpoint(ports, task_id, {
        "bland_call_ids": list(call_ids),
        "phone": phone,
        "completed_at": (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if ok else None
        ),
    })

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
            "verified": True, "available": True, "statuses": {},
            "source": "dry_run",
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

    note_body = _format_recovery_note(applicant_name, instruction, call_result)

    if config.require_outcome_verification and not outcome_verified:
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
            f"(failed/busy/no-answer/canceled). Task left OPEN for human review.",
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
