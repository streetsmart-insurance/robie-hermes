"""Audit verification worker (job type ``audit_verification``).

Post-renewal audit chase for workers' comp policies. AUDIT TIMING IS
POST-RENEWAL (Carlo 2026-09-10): audit verifications run 30-45 days AFTER a
policy RENEWS. The job: check whether the carrier's audit documents for the
just-completed term are available yet, and get them to the client to complete
the audit.

Per-policy timing:
- < 30 days since renewal -> pending, waiting_on="schedule"
  ("audit not yet due - X days since renewal").
- 30-45 days (work window) -> chase papers: portal -> email -> voice.
- > 45 days with no papers -> escalate: pending, waiting_on="csr".

Ports the old renewal-automation-system ``src/voice/`` audit stack:
PATHWAY_AUDIT script, Bland POST /v1/calls queue discipline (POST acceptance
is NOT proof a call was placed - only queue/progress read-back counts), the
carrier phone directory, and call-completion -> EZLynx filing (<=6000 chars,
``ROBIE was here`` sign-off).

Voice (Bland AI, CARRIERS ONLY) is APPROVED. HARD RULES, enforced in code:
- The voice target must be a carrier number from the audit call directory.
  Unknown number -> outcome not_done with reason "phone number needed";
  never dialed.
- NEVER dial clients: the voice path uses ONLY directory-resolved carrier
  numbers. Row-supplied phones are never dialed, and a directory number that
  matches a client/insured phone on the row is refused.
- ``carrier_voice_attempted`` once-only guard: the voice branch fires at most
  once per policy; it never re-fires.

Login gaps: when a carrier portal login stalls on something the SOPs don't
cover, the worker calls ``record_login_gap(store, job_id, job_type,
portal_name, step, whats_missing)`` (verification_common) and marks the
policy pending (waiting_on="carrier") meanwhile, so Carlo's walkthrough can
report exactly which portal is stuck and on what.

Sibling contracts (built by sibling agents - import, do NOT reimplement):
- .report_fetcher.fetch_report_rows(*, report_id, fields=None, filters=None,
  db_path=None, session=None) -> list[dict]
- .verification_common.PolicyOutcome, .verification_common.record_outcomes,
  .verification_common.is_action_authorized(job, action_name),
  .verification_common.record_login_gap(store, job_id, job_type, portal_name,
  step, whats_missing)
- .verification_mailer.send_verification_email(...)

Assumed contracts (integration tests must confirm these with the sibling
agents):
- is_action_authorized(job, action) -> bool. Convention: the action is in
  job["payload"].get("authorized_actions", []).
- PolicyOutcome is a dataclass accepting keyword fields: policy_number,
  audit_id, insured_name, carrier, department, applicant_id, policy_aliases,
  status, reason, actions_taken, waiting_on, evidence, updated_at.
- record_outcomes(job, outcomes) -> list. Persists outcomes to the job's
  action-checkpoint detail AND mirrors each outcome into durable_work_items
  under namespace "audit_verification", keyed by the audit identity
  (audit_id, falling back to policy_number), with the full outcome
  (including evidence) as the row's outcome JSON.
- send_verification_email(*, job, to, subject, body, cc=(), policy_number=None)
  -> dict receipt. Performs the actual send; never called unless
  is_action_authorized(job, "send_carrier_email").
- record_login_gap(store, job_id, job_type, portal_name, step, whats_missing)
  -> dict/None. Logs a portal login gap for the walkthrough report.
- VoiceDispatcherPort.dispatch_voice_call(request: BlandCallRequest) -> dict
  with keys: success, call_id, call_placed, queue_status, status, retryable,
  error. ``call_placed`` follows Bland queue discipline (see
  classify_voice_result): POST acceptance alone never counts as placed.
  The production wiring supplies a Bland-backed port; until it is wired the
  worker records "voice enabled but no dispatcher wired - no call placed"
  and dials nothing (fail closed).
- Report 4246 row fields (tolerant reader; missing fields degrade to safe
  defaults, never to invented values): audit_id, policy_number, insured_name,
  carrier, line_of_business, department, applicant_id, policy_aliases,
  renewal_effective_date (ISO or mm/dd/yyyy; also accepts
  policy_effective_date / effective_date), audit_status, missing_documents,
  document_refs / audit_documents, carrier_email / underwriter_email,
  no_contact_count / followup_attempts, policy_status, portal_login_gap /
  portal_login_status / portal_gap_detail / portal_name, voice_requested,
  client_phone / insured_phone / applicant_phone (used ONLY to refuse
  dialing, never as a dial target), call_recording_url / call_transcript /
  call_summary (prior call artifacts to file).

Cardinal rule: ROBIE never deletes a policy. This module performs no
deletions, no binds, no payments, no policy mutations. Every outbound action
(email send, EZLynx note post, voice dispatch) is gated on
is_action_authorized() (or on explicit dispatcher wiring for voice);
unauthorized actions become pending outcomes, never executions.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult
from .report_fetcher import fetch_report_rows
from .report_registry import get_report_spec
from .verification_common import (
    PolicyOutcome,
    is_action_authorized,
    record_login_gap,
    record_outcomes,
)
from .verification_mailer import send_verification_email

logger = logging.getLogger("audit_verification_worker")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_TYPE = "audit_verification"
WORKER_NAME = "audit-verification"
AUDIT_REPORT_ID = "4246"
DURABLE_NAMESPACE = "audit_verification"

# Post-renewal audit window (days since the policy's renewal effective date).
WORK_WINDOW_START_DAYS = 30
WORK_WINDOW_END_DAYS = 45
FOLLOWUP_DAYS = 7
NO_CONTACT_ESCALATION_THRESHOLD = 2

# EZLynx note limits / conventions (ported from old call_completion.py).
NOTE_CHAR_LIMIT = 6000
NOTE_SIGNOFF = "ROBIE was here"

# Voice.
PATHWAY_AUDIT = "audit"
ROBIE_SENDER_EMAIL = "robie@streetsmart.insurance"
BLAND_VOICE = "nat"
BLAND_MODEL = "enhanced"

# Bland queue discipline (ported from old voice_client.py): POST acceptance
# ("status: success" + call_id) is NOT proof a call was placed. Only
# queue/progress read-back counts.
BLAND_QUEUE_ERROR_STATUSES = frozenset({"pre_queue_error", "queue_error"})
BLAND_CALL_PLACED_STATUSES = frozenset(
    {
        "queued",
        "allocated",
        "started",
        "in_progress",
        "inprogress",
        "ringing",
        "answered",
        "completed",
        "complete",
        "busy",
        "no-answer",
        "no_answer",
        "noanswer",
        "voicemail",
        "failed",
    }
)
_BLAND_POST_ONLY_STATUSES = frozenset({"success", "ok", "accepted"})

VALID_OUTCOME_STATUSES = frozenset({"done", "not_done", "pending"})
VALID_WAITING_ON = frozenset({"carrier", "client", "csr", "schedule"})

_DIRECTORY_CACHE: Optional[dict[str, str]] = None


# ---------------------------------------------------------------------------
# Pure helpers: phone directory
# ---------------------------------------------------------------------------


def normalize_phone_e164(raw_phone: Optional[str]) -> Optional[str]:
    """Normalize any phone string to E.164 (+1XXXXXXXXXX).

    Port of the old voice call_directory.normalize_phone_e164. Returns None
    instead of inventing a number.
    """
    if not raw_phone:
        return None
    digits = re.sub(r"\D", "", str(raw_phone))
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if digits:
        return f"+{digits}"
    return None


def load_audit_call_directory(path: Optional[str | Path] = None) -> dict[str, str]:
    """Load the seeded carrier phone directory (name -> E.164).

    Ported data: HARDCODED_CARRIER_PHONES from the old voice call_directory.py.
    Carrier business lines only as seeded; never invent numbers.
    """
    global _DIRECTORY_CACHE
    if path is None and _DIRECTORY_CACHE is not None:
        return _DIRECTORY_CACHE
    directory_path = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parent / "data" / "audit_call_directory.json"
    )
    try:
        payload = json.loads(directory_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("audit call directory unreadable at %s: %s", directory_path, exc)
        return {}
    phones = payload.get("phones") if isinstance(payload, dict) else None
    result: dict[str, str] = {}
    if isinstance(phones, dict):
        for name, value in phones.items():
            normalized = normalize_phone_e164(value)
            if name and normalized:
                result[str(name).strip().lower()] = normalized
    if path is None:
        _DIRECTORY_CACHE = result
    return result


def lookup_carrier_phone(
    carrier_name: Optional[str],
    directory: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """Resolve a carrier's E.164 business line from the seeded directory.

    Exact match first, then substring match (ported from the old directory).
    Returns None when unknown - callers must treat that as "phone needed",
    never dial anything else.
    """
    phones = directory if directory is not None else load_audit_call_directory()
    clean = (carrier_name or "").strip().lower()
    if not clean or not phones:
        return None
    if clean in phones:
        return phones[clean]
    for key, phone in phones.items():
        if key in clean or clean in key:
            return phone
    return None


# ---------------------------------------------------------------------------
# Pure helpers: PATHWAY_AUDIT voice script + dossier (ported, no I/O)
# ---------------------------------------------------------------------------


def build_audit_call_script(
    policy_number: str,
    named_insured: str,
    agency_code: Optional[str],
    fein: Optional[str],
) -> str:
    """PATHWAY_AUDIT live script (pure function, ported from the old stack).

    Identify as Robie from StreetSmart, give agency code + policy# + named
    insured + FEIN, request the final audit statement to
    robie@streetsmart.insurance.
    """
    code_clause = (
        f"Our agency producer code with your company is {agency_code}."
        if (agency_code or "").strip()
        else "We are calling from StreetSmart Insurance."
    )
    fein_clause = (
        f" The federal EIN on file is {fein}." if (fein or "").strip() else ""
    )
    return (
        f"Hello, my name is Robie calling from StreetSmart Insurance. {code_clause} "
        f"I am checking on the final payroll audit for policy number {policy_number}, "
        f"named insured {named_insured}.{fein_clause} "
        "Is the audit closed, or are additional 941s needed? "
        f"Please email the final audit statement to {ROBIE_SENDER_EMAIL}. Thank you!"
    )


def build_audit_voicemail_script(
    policy_number: str,
    named_insured: str,
    agency_code: Optional[str],
    fein: Optional[str],
) -> str:
    """PATHWAY_AUDIT voicemail variant (pure function)."""
    code_clause = (
        f"Our agency producer code is {agency_code}."
        if (agency_code or "").strip()
        else "StreetSmart Insurance."
    )
    fein_clause = (
        f" The federal EIN on file is {fein}." if (fein or "").strip() else ""
    )
    return (
        f"Hello, this is Robie from StreetSmart Insurance, {code_clause} "
        f"calling about the final payroll audit for policy number {policy_number}, "
        f"named insured {named_insured}.{fein_clause} "
        f"Please email the final audit statement to {ROBIE_SENDER_EMAIL}. Thank you!"
    )


def hydrate_calling_dossier(
    *,
    policy_number: str,
    named_insured: str,
    carrier_name: str,
    line_of_business: str = "Workers Compensation",
    agency_code: Optional[str] = None,
    fein: Optional[str] = None,
    carrier_phone: Optional[str] = None,
    applicant_id: Optional[Any] = None,
    assigned_csr_email: Optional[str] = None,
    expiration_date: Optional[str] = None,
) -> dict[str, Any]:
    """Build the carrier-call dossier as pure data (no DB, no network).

    Port of the old CallingDossier hydrate step, minus every I/O lookup.
    ``carrier_phone`` stays None unless the caller resolved it from the
    seeded directory - this function never invents a number.
    """
    return {
        "policy_number": policy_number,
        "insured_name": named_insured,
        "carrier_name": carrier_name,
        "line_of_business": line_of_business,
        "agency_code": agency_code,
        "fein": fein,
        "carrier_phone": carrier_phone,
        "applicant_id": applicant_id,
        "assigned_csr_email": assigned_csr_email,
        "expiration_date": expiration_date,
        "call_type": "carrier",
        "outreach_pathway": PATHWAY_AUDIT,
    }


# ---------------------------------------------------------------------------
# Bland call request (pure) + queue-discipline classification (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlandCallRequest:
    """Everything a Bland POST /v1/calls needs for a PATHWAY_AUDIT call.

    ``phone_number`` MUST be a carrier number resolved from the audit call
    directory (E.164). The worker enforces this before dispatch.
    """

    phone_number: str
    task: str
    first_sentence: str
    voicemail_message: str
    policy_number: str
    named_insured: str
    carrier_name: str
    voice: str = BLAND_VOICE
    model: str = BLAND_MODEL
    applicant_id: Optional[Any] = None
    metadata: dict[str, Any] = field(default_factory=dict)


class VoiceDispatcherPort(Protocol):
    """Outbound voice dispatch. Implemented by the Bland-backed wiring.

    Returns a dict with keys: success, call_id, call_placed, queue_status,
    status, retryable, error. ``call_placed`` must follow Bland queue
    discipline (see classify_voice_result): POST acceptance alone never
    counts as placed.
    """

    def dispatch_voice_call(self, request: BlandCallRequest) -> dict[str, Any]:
        ...


def build_bland_call_request(
    *,
    policy_number: str,
    named_insured: str,
    carrier_name: str,
    carrier_phone: str,
    agency_code: Optional[str] = None,
    fein: Optional[str] = None,
    applicant_id: Optional[Any] = None,
    line_of_business: str = "Workers Compensation",
) -> BlandCallRequest:
    """Build the Bland PATHWAY_AUDIT call request (pure; no network)."""
    task = build_audit_call_script(policy_number, named_insured, agency_code, fein)
    first_sentence = (
        "Hello! My name is Robie calling from StreetSmart Insurance regarding "
        f"the final audit for policy number {policy_number}."
    )
    return BlandCallRequest(
        phone_number=carrier_phone,
        task=task,
        first_sentence=first_sentence,
        voicemail_message=build_audit_voicemail_script(
            policy_number, named_insured, agency_code, fein
        ),
        policy_number=policy_number,
        named_insured=named_insured,
        carrier_name=carrier_name,
        applicant_id=applicant_id,
        metadata={
            "policy_number": policy_number,
            "insured_name": named_insured,
            "carrier_name": carrier_name,
            "line_of_business": line_of_business,
            "agency_code": agency_code,
            "outreach_pathway": PATHWAY_AUDIT,
            "call_type": "carrier",
        },
    )


def bland_post_payload(request: BlandCallRequest) -> dict[str, Any]:
    """Render the Bland POST /v1/calls JSON body for a request (pure).

    Mirrors the old CarrierVoiceClient._dispatch_bland_ai payload semantics
    (voice/model/record/answered_by/ivr/first_sentence/voicemail/metadata).
    The live POST itself belongs to the VoiceDispatcherPort implementation,
    which owns the API key - never this worker.
    """
    return {
        "phone_number": request.phone_number,
        "task": request.task,
        "voice": request.voice,
        "model": request.model,
        "record": True,
        "answered_by_enabled": True,
        "wait_for_greeting": True,
        "ivr_navigation": True,
        "first_sentence": request.first_sentence,
        "voicemail_action": "leave_message",
        "voicemail_message": request.voicemail_message,
        "metadata": dict(request.metadata),
    }


def _normalize_bland_status(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def classify_voice_result(result: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Classify a voice dispatch result with Bland queue discipline (pure).

    ``call_placed`` is True only when queue/progress shows the call queued,
    in progress, or finished. Bland POST acceptance (``status: success`` +
    call_id) is ignored on its own. Queue errors are retryable because the
    phone never rang.
    """
    result = result or {}
    queue_status = _normalize_bland_status(result.get("queue_status"))
    progress_status = _normalize_bland_status(result.get("status"))
    if progress_status in _BLAND_POST_ONLY_STATUSES:
        progress_status = ""
    for candidate in (queue_status, progress_status):
        if candidate in BLAND_QUEUE_ERROR_STATUSES:
            return {
                "call_placed": False,
                "retryable": True,
                "queue_status": candidate,
                "status": "QUEUE_ERROR",
            }
    for candidate in (queue_status, progress_status):
        if candidate in BLAND_CALL_PLACED_STATUSES:
            return {
                "call_placed": True,
                "retryable": False,
                "queue_status": candidate,
                "status": "DISPATCHED",
            }
    observed = queue_status or progress_status or None
    return {
        "call_placed": False,
        "retryable": True,
        "queue_status": observed,
        "status": "ACCEPTED_NOT_CONFIRMED",
    }


# ---------------------------------------------------------------------------
# Concrete Bland dispatcher (stdlib HTTP; owns the API key it is given)
# ---------------------------------------------------------------------------


class BlandVoiceDispatcher:
    """VoiceDispatcherPort backed by the Bland /v1/calls API.

    Construction/wiring only - the worker never instantiates this itself.
    The API key is supplied by the engine wiring (read from the secret
    store at runtime); it is never logged, never written to files, and
    never placed in a URL.

    Dispatch discipline: POST the call, then immediately read back
    GET /v1/calls/{call_id}. ``call_placed`` follows classify_voice_result:
    POST acceptance alone is not proof - only queue/progress read-back
    counts. Transport failures return a retryable dict, never raise.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.bland.ai",
        timeout_s: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("BlandVoiceDispatcher requires an api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    # -- stdlib HTTP -----------------------------------------------------
    def _request_json(
        self, method: str, path: str, body: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        import json as _json
        import urllib.request as _request

        data = None
        headers = {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
        }
        if body is not None:
            data = _json.dumps(body).encode("utf-8")
        req = _request.Request(
            self._base_url + path, data=data, headers=headers, method=method
        )
        with _request.urlopen(req, timeout=self._timeout_s) as resp:
            raw = resp.read().decode("utf-8") if resp is not None else "{}"
        try:
            parsed = _json.loads(raw)
        except ValueError:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    # -- port implementation ----------------------------------------------
    def dispatch_voice_call(self, request: BlandCallRequest) -> dict[str, Any]:
        try:
            posted = self._request_json(
                "POST", "/v1/calls", bland_post_payload(request)
            )
        except Exception as exc:  # transport / HTTP error: retryable
            return {
                "success": False,
                "call_id": None,
                "call_placed": False,
                "queue_status": None,
                "status": "POST_FAILED",
                "retryable": True,
                "error": f"{type(exc).__name__}: {exc}",
            }
        call_id = posted.get("call_id")
        if not call_id:
            return {
                "success": False,
                "call_id": None,
                "call_placed": False,
                "queue_status": None,
                "status": "NO_CALL_ID",
                "retryable": True,
                "error": "Bland POST returned no call_id",
            }
        # Queue/progress read-back: the only proof a call was placed.
        try:
            progress = self._request_json("GET", f"/v1/calls/{call_id}")
        except Exception:
            progress = {}
        classification = classify_voice_result(
            {
                "queue_status": progress.get("queue_status"),
                "status": progress.get("status"),
                "call_id": call_id,
            }
        )
        return {
            "success": classification["call_placed"],
            "call_id": call_id,
            "call_placed": classification["call_placed"],
            "queue_status": classification["queue_status"],
            "status": classification["status"],
            "retryable": classification["retryable"],
            "error": None if classification["call_placed"] else (
                "queue read-back did not confirm a placed call"
            ),
        }


# ---------------------------------------------------------------------------
# Call-artifact filing -> EZLynx note (pure builder + gated post)
# ---------------------------------------------------------------------------


class NotePosterPort(Protocol):
    """EZLynx discussion-note posting. Implemented by engine wiring."""

    def post_note(self, *, policy: dict[str, Any], note_body: str) -> dict[str, Any]:
        ...


def _truncate_transcript(text: str, limit: int) -> tuple[str, bool]:
    text = text or ""
    if len(text) <= limit:
        return text, False
    return (
        text[:limit].rstrip()
        + "\n\n[Transcript truncated - full text uploaded to EZLynx Documents]",
        True,
    )


def build_call_completion_note(
    *,
    policy: dict[str, Any],
    call_id: Optional[str],
    recording_url: str,
    transcript: str,
    summary: str,
) -> str:
    """Build the EZLynx discussion note for a completed audit call (pure).

    Header ``Policy: #{num} ({LOB} - {carrier})``, body <= NOTE_CHAR_LIMIT,
    ends with the exact line ``ROBIE was here``. Ported from the old
    call_completion.build_completion_note.
    """
    policy_number = policy.get("policy_number") or "unknown"
    lob = policy.get("line_of_business") or "Workers Compensation"
    carrier = policy.get("carrier") or policy.get("carrier_name") or "Carrier"
    header = (
        f"Policy: #{policy_number} ({lob} - {carrier})\n"
        "Autonomous Carrier Audit Phone Outreach Completed:\n"
        f"- Result: Call finished\n"
        f"- Call ID: {call_id or 'unknown'}\n"
        f"- Summary: {summary or 'Call completed.'}\n"
        f"- Audio Recording: {recording_url or 'N/A'}\n"
    )
    footer = f"\n{NOTE_SIGNOFF}"
    transcript_label = "\nTranscript:\n"
    # Reserve space for header + label + footer; transcript gets the rest.
    reserved = len(header) + len(transcript_label) + len(footer)
    excerpt, _ = _truncate_transcript(
        transcript, max(0, NOTE_CHAR_LIMIT - reserved)
    )
    note = f"{header}{transcript_label}{excerpt or '(no transcript returned)'}{footer}"
    if len(note) > NOTE_CHAR_LIMIT:
        # Absolute backstop: hard-trim the transcript further.
        overflow = len(note) - NOTE_CHAR_LIMIT
        excerpt = excerpt[: max(0, len(excerpt) - overflow)]
        note = f"{header}{transcript_label}{excerpt}{footer}"
    return note


def file_call_artifacts(
    policy: dict[str, Any],
    recording_url: str,
    transcript: str,
    summary: str,
    *,
    job: Optional[dict[str, Any]] = None,
    note_poster: Optional[NotePosterPort] = None,
) -> dict[str, Any]:
    """File call artifacts as an EZLynx discussion note.

    Always returns the built note body (<=6000 chars, ROBIE was here
    sign-off). The note is POSTED only when ``job`` authorizes
    ``post_ezlynx_note`` via is_action_authorized AND a ``note_poster`` is
    supplied; otherwise it is returned unposted with the blocking reason.
    """
    note_body = build_call_completion_note(
        policy=policy,
        call_id=(policy or {}).get("call_id"),
        recording_url=recording_url,
        transcript=transcript,
        summary=summary,
    )
    result: dict[str, Any] = {
        "note_body": note_body,
        "note_chars": len(note_body),
        "posted": False,
        "post_blocked_reason": None,
        "receipt": None,
    }
    if job is None or note_poster is None:
        result["post_blocked_reason"] = (
            "no job/note_poster supplied - note built but not posted"
        )
        return result
    if not is_action_authorized(job, "post_ezlynx_note"):
        result["post_blocked_reason"] = (
            "post_ezlynx_note not authorized - note built but not posted"
        )
        return result
    try:
        receipt = note_poster.post_note(policy=policy, note_body=note_body)
    except Exception as exc:
        result["post_blocked_reason"] = f"note post failed: {type(exc).__name__}: {exc}"
        return result
    result["posted"] = True
    result["receipt"] = receipt
    return result


# ---------------------------------------------------------------------------
# Row helpers (tolerant readers - never invent values)
# ---------------------------------------------------------------------------


def _row_str(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _days_since_renewal(row: dict[str, Any], today: date) -> Optional[int]:
    raw = (
        row.get("renewal_effective_date")
        or row.get("policy_effective_date")
        or row.get("effective_date")
    )
    effective = _parse_date(raw)
    if effective is None:
        return None
    return (today - effective).days


def _missing_papers(row: dict[str, Any]) -> list[str]:
    missing = row.get("missing_documents") or row.get("missing_papers")
    if isinstance(missing, str):
        parts = [p.strip() for p in re.split(r"[;,]", missing) if p.strip()]
        if parts:
            return parts
    elif isinstance(missing, (list, tuple)):
        parts = [str(p).strip() for p in missing if str(p).strip()]
        if parts:
            return parts
    status = str(row.get("audit_status") or row.get("status") or "").lower()
    if any(
        token in status
        for token in ("complete", "closed", "received", "finalized", "done")
    ):
        return []
    return ["final audit statement"]


def _document_refs(row: dict[str, Any]) -> list[str]:
    refs = row.get("document_refs") or row.get("audit_documents") or row.get("documents")
    if isinstance(refs, str):
        return [p.strip() for p in re.split(r"[;,]", refs) if p.strip()]
    if isinstance(refs, (list, tuple)):
        return [str(p).strip() for p in refs if str(p).strip()]
    return []


def _is_cancelled(row: dict[str, Any]) -> bool:
    status = str(
        row.get("policy_status") or row.get("account_status") or ""
    ).lower()
    return any(
        token in status for token in ("cancel", "inactive", "non-renew", "nonrenew")
    )


def _no_contact_count(row: dict[str, Any]) -> int:
    for key in ("no_contact_count", "followup_attempts", "follow_up_attempts"):
        try:
            value = int(row.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value:
            return value
    return 0


def _carrier_email(row: dict[str, Any]) -> str:
    return _row_str(row, "carrier_email", "underwriter_email", "uw_email")


def _detect_login_gap(row: dict[str, Any]) -> Optional[dict[str, str]]:
    """Detect a portal login stall the SOPs don't cover (row-driven).

    Returns {"portal_name", "step", "whats_missing"} or None. The worker
    records it via record_login_gap() and marks the policy pending meanwhile.
    """
    gap = row.get("portal_login_gap")
    if isinstance(gap, dict) and gap:
        return {
            "portal_name": str(
                gap.get("portal_name") or row.get("portal_name") or row.get("carrier") or "unknown portal"
            ),
            "step": str(gap.get("step") or "login"),
            "whats_missing": str(gap.get("whats_missing") or "not specified in report row"),
        }
    status = str(row.get("portal_login_status") or "").strip().lower()
    if status in {"stalled", "blocked", "mfa_required", "credentials_missing", "login_failed"}:
        return {
            "portal_name": str(
                row.get("portal_name") or row.get("carrier") or "unknown portal"
            ),
            "step": str(row.get("portal_login_status") or "login"),
            "whats_missing": str(
                row.get("portal_gap_detail") or "not specified in report row"
            ),
        }
    return None


def _client_phones(row: dict[str, Any]) -> set[str]:
    phones: set[str] = set()
    for key in ("client_phone", "insured_phone", "applicant_phone", "contact_phone"):
        normalized = normalize_phone_e164(row.get(key))
        if normalized:
            phones.add(normalized)
    return phones


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class AuditVerificationWorker:
    """Work the EZLynx audit queue (report 4246) once per day.

    Worker name: ``audit-verification``. Job type: ``audit_verification``.
    """

    def __init__(
        self,
        *,
        directory: Optional[dict[str, str]] = None,
        voice_dispatcher: Optional[VoiceDispatcherPort] = None,
        note_poster: Optional[NotePosterPort] = None,
        store: Optional[Any] = None,
    ) -> None:
        self._directory = directory  # None -> lazy load of the seeded file
        self._voice_dispatcher = voice_dispatcher
        self._note_poster = note_poster
        self._store = store  # JobStore, for record_login_gap(); may be None
        self._voice_attempted: set[str] = set()  # in-run once-only guard

    # -- small utilities -------------------------------------------------

    def _directory_map(self) -> dict[str, str]:
        if self._directory is not None:
            return self._directory
        return load_audit_call_directory()

    def _db_path(self, job: dict[str, Any]) -> Optional[str]:
        payload = job.get("payload") or {}
        return (
            payload.get("jobs_db_path")
            or os.environ.get("ROBIE_JOB_DB")
            or None
        )

    def _resolve_store(self, job: dict[str, Any]) -> Any:
        """JobStore for shared outcome recording.

        Registration passes a store; otherwise build one from the job's DB path.
        Raises when neither is available (fail closed — never silently drop outcomes).
        """
        if self._store is not None:
            return self._store
        db_path = self._db_path(job)
        if db_path:
            from .store import JobStore

            return JobStore(db_path)
        raise RuntimeError("no JobStore available for record_outcomes")

    def _durable_evidence(self, job: dict[str, Any], audit_key: str) -> dict[str, Any]:
        """Best-effort read of a prior outcome from durable_work_items.

        Used for the carrier_voice_attempted once-only guard across runs.
        Never raises; returns {} when unreadable.
        """
        db_path = self._db_path(job)
        if not db_path:
            return {}
        try:
            from .idempotency import DurableWorkLedger

            row = DurableWorkLedger(db_path).get(DURABLE_NAMESPACE, audit_key)
        except Exception:
            return {}
        outcome = (row or {}).get("outcome")
        if isinstance(outcome, str):
            try:
                outcome = json.loads(outcome)
            except Exception:
                return {}
        if isinstance(outcome, dict):
            evidence = outcome.get("evidence")
            return evidence if isinstance(evidence, dict) else {}
        return {}

    def _voice_already_attempted(self, job: dict[str, Any], audit_key: str) -> bool:
        if audit_key in self._voice_attempted:
            return True
        return bool(self._durable_evidence(job, audit_key).get("carrier_voice_attempted"))

    def _resolve_carrier_voice_number(
        self, carrier_name: str, row: dict[str, Any]
    ) -> Optional[str]:
        """Resolve the voice target: directory carrier number ONLY.

        HARD RULE: never dial clients. The number must come from the seeded
        carrier directory; row-supplied phones are never dialed, and a
        directory number matching a client/insured phone on the row is
        refused (returns None).
        """
        phone = lookup_carrier_phone(carrier_name, self._directory_map())
        if not phone:
            return None
        if phone in _client_phones(row):
            logger.warning(
                "refusing to dial directory number %s: matches a client/insured "
                "phone on the audit row",
                phone,
            )
            return None
        return phone

    # -- outcome construction --------------------------------------------

    def _new_outcome_fields(
        self,
        row: dict[str, Any],
        *,
        status: str,
        reason: str,
        actions_taken: list[str],
        waiting_on: str,
        evidence: dict[str, Any],
        audit_key: str,
    ) -> dict[str, Any]:
        aliases = row.get("policy_aliases") or row.get("aliases")
        audit_id_value = _row_str(row, "audit_id") or audit_key
        merged_evidence = dict(evidence)
        merged_evidence.setdefault("audit_id", audit_id_value)
        merged_evidence.setdefault("audit_key", audit_key)
        return {
            "policy_number": _row_str(row, "policy_number") or audit_key,
            "audit_id": audit_id_value,
            "insured_name": _row_str(row, "insured_name", "named_insured"),
            "carrier": _row_str(row, "carrier", "carrier_name"),
            "department": _row_str(row, "department") or "Operations",
            "applicant_id": row.get("applicant_id"),
            "policy_aliases": list(aliases) if isinstance(aliases, (list, tuple)) else [],
            "status": status,
            "reason": reason,
            "actions_taken": list(actions_taken),
            "waiting_on": waiting_on,
            "evidence": merged_evidence,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _serialize_outcome(outcome: Any) -> dict[str, Any]:
        try:
            return asdict(outcome)
        except Exception:
            pass
        to_dict = getattr(outcome, "to_dict", None)
        if callable(to_dict):
            try:
                result = to_dict()
                if isinstance(result, dict):
                    return result
            except Exception:
                pass
        if hasattr(outcome, "__dict__"):
            return dict(vars(outcome))
        return {"repr": repr(outcome)}

    # -- per-row processing ----------------------------------------------

    def _process_row(
        self,
        *,
        job: dict[str, Any],
        row: dict[str, Any],
        today: date,
        voice_enabled: bool,
    ) -> dict[str, Any]:
        audit_key = _row_str(row, "audit_id") or _row_str(row, "policy_number") or "unknown"
        policy_number = _row_str(row, "policy_number") or audit_key
        carrier = _row_str(row, "carrier", "carrier_name")
        insured = _row_str(row, "insured_name", "named_insured")
        actions: list[str] = []
        evidence: dict[str, Any] = {"audit_key": audit_key}

        # 0. Cancellation gate: never work outreach on cancelled/inactive.
        if _is_cancelled(row):
            return self._new_outcome_fields(
                row,
                status="not_done",
                reason="EXCLUDED_INACTIVE_ACCOUNT - policy cancelled/inactive; no outreach",
                actions_taken=actions,
                waiting_on="csr",
                evidence=evidence,
                audit_key=audit_key,
            )

        # 1. Post-renewal timing.
        days = _days_since_renewal(row, today)
        if days is None:
            return self._new_outcome_fields(
                row,
                status="not_done",
                reason="renewal effective date needed - cannot compute post-renewal audit window",
                actions_taken=actions,
                waiting_on="csr",
                evidence=evidence,
                audit_key=audit_key,
            )
        evidence["days_since_renewal"] = days
        if days < WORK_WINDOW_START_DAYS:
            return self._new_outcome_fields(
                row,
                status="pending",
                reason=(
                    f"audit not yet due - {days} days since renewal "
                    f"(work window {WORK_WINDOW_START_DAYS}-{WORK_WINDOW_END_DAYS} days post-renewal)"
                ),
                actions_taken=["window_check"],
                waiting_on="schedule",
                evidence=evidence,
                audit_key=audit_key,
            )

        # 2. Papers already in hand -> done (only with evidence refs).
        missing = _missing_papers(row)
        refs = _document_refs(row)
        if not missing and refs:
            evidence["document_refs"] = refs
            return self._new_outcome_fields(
                row,
                status="done",
                reason="audit papers received - filed to client",
                actions_taken=["papers_received"],
                waiting_on="client",
                evidence=evidence,
                audit_key=audit_key,
            )
        if not missing:
            # Flagged received but nothing on file: stay pending, chase evidence.
            evidence["document_refs"] = []
            return self._new_outcome_fields(
                row,
                status="pending",
                reason=(
                    "audit papers marked received but no document refs on file - "
                    "pending evidence filing"
                ),
                actions_taken=["papers_received_unverified"],
                waiting_on="carrier",
                evidence=evidence,
                audit_key=audit_key,
            )

        escalated = days > WORK_WINDOW_END_DAYS
        no_contact_count = _no_contact_count(row)
        repeated_no_contact = no_contact_count >= NO_CONTACT_ESCALATION_THRESHOLD
        escalated = escalated or repeated_no_contact
        evidence["missing_papers"] = missing
        follow_up_due = (today + timedelta(days=FOLLOWUP_DAYS)).isoformat()

        # 3. Portal channel: record the retrieval as a bounded action intent.
        #    A real portal crawl is future browser work; the intent is
        #    idempotent and has no external effect.
        evidence["portal_intent"] = {
            "channel": "portal",
            "carrier": carrier,
            "policy_number": policy_number,
            "intent": "retrieve_final_audit_statement",
            "bounded": True,
        }
        actions.append("portal_retrieval_intent")

        # 3b. Portal login gap: log it for Carlo's walkthrough, stay pending.
        gap = _detect_login_gap(row)
        if gap is not None:
            evidence["login_gap"] = gap
            actions.append("login_gap_logged")
            if self._store is not None:
                try:
                    record_login_gap(
                        self._store,
                        job.get("id"),
                        JOB_TYPE,
                        gap["portal_name"],
                        gap["step"],
                        gap["whats_missing"],
                    )
                except Exception as exc:
                    logger.warning("record_login_gap failed: %s", exc)
                    evidence["login_gap"]["record_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
            else:
                evidence["login_gap"]["record_error"] = (
                    "no store wired on worker - gap kept in outcome evidence only"
                )

        # 4. Email channel (gated).
        carrier_email = _carrier_email(row)
        email_authorized = is_action_authorized(job, "send_carrier_email")
        if email_authorized and carrier_email:
            subject = (
                f"[AUDIT-REQ-{audit_key}] Final audit statement needed: "
                f"{insured or 'insured'} - Pol #{policy_number}"
            )
            body = (
                f"Hello,\n\nThis is Robie from StreetSmart Insurance.\n\n"
                f"Agency code: {_row_str(row, 'agency_code') or 'on file'}\n"
                f"Policy number: {policy_number}\n"
                f"Named insured: {insured or 'on file'}\n"
                f"FEIN: {_row_str(row, 'fein') or 'on file'}\n\n"
                "Please send the final audit statement for the just-completed "
                f"policy term to {ROBIE_SENDER_EMAIL}.\n\n"
                "Thank you,\nRobie\nStreetSmart Insurance"
            )
            try:
                receipt = send_verification_email(
                    to=[carrier_email],
                    cc=[],
                    subject=subject,
                    text_body=body,
                )
            except Exception as exc:
                return self._new_outcome_fields(
                    row,
                    status="not_done",
                    reason=f"carrier email send failed: {type(exc).__name__}: {exc}",
                    actions_taken=actions,
                    waiting_on="carrier",
                    evidence=evidence,
                    audit_key=audit_key,
                )
            actions.append("carrier_email_sent")
            evidence["email_receipt"] = receipt
            evidence["follow_up_due"] = follow_up_due
        elif email_authorized:
            return self._new_outcome_fields(
                row,
                status="not_done",
                reason="carrier email needed - no carrier/underwriter email on the audit row",
                actions_taken=actions,
                waiting_on="csr",
                evidence=evidence,
                audit_key=audit_key,
            )
        else:
            evidence["intended_action"] = {
                "action": "send_carrier_email",
                "to": carrier_email or "carrier (no email on file)",
                "subject_preview": f"[AUDIT-REQ-{audit_key}] Final audit statement needed",
            }
            actions.append("carrier_email_intended_not_sent")

        # 5. Voice channel (Bland, carriers only).
        voice_fired = self._maybe_voice(
            job=job,
            row=row,
            audit_key=audit_key,
            policy_number=policy_number,
            carrier=carrier,
            insured=insured,
            voice_enabled=voice_enabled,
            escalated=escalated,
            actions=actions,
            evidence=evidence,
        )

        # 6. File prior call artifacts when the row carries them.
        self._maybe_file_artifacts(job=job, row=row, actions=actions, evidence=evidence)

        # 7. Final status for the chase.
        if escalated:
            if days > WORK_WINDOW_END_DAYS:
                cause = f"audit papers missing {days} days post-renewal"
            else:
                cause = f"repeated no-contact ({no_contact_count} attempts)"
            return self._new_outcome_fields(
                row,
                status="pending",
                reason=(
                    f"{cause} - escalated to CSR"
                    + ("; carrier voice attempted" if voice_fired else "")
                ),
                actions_taken=actions,
                waiting_on="csr",
                evidence=evidence,
                audit_key=audit_key,
            )
        if gap is not None:
            return self._new_outcome_fields(
                row,
                status="pending",
                reason=(
                    f"portal login gap at {gap['portal_name']}: {gap['whats_missing']} "
                    "- logged for walkthrough; carrier chase continues"
                ),
                actions_taken=actions,
                waiting_on="carrier",
                evidence=evidence,
                audit_key=audit_key,
            )
        return self._new_outcome_fields(
            row,
            status="pending",
            reason=(
                f"final audit statement requested from {carrier or 'carrier'}; "
                f"follow up {follow_up_due}"
            ),
            actions_taken=actions,
            waiting_on="carrier",
            evidence=evidence,
            audit_key=audit_key,
        )

    def _voice_branch_due(
        self,
        *,
        row: dict[str, Any],
        escalated: bool,
        durable_evidence: dict[str, Any],
    ) -> bool:
        """Decide whether the voice branch should fire for this policy.

        Fires when papers are still missing and the email channel has already
        had a turn (prior no-contact, a prior run's email, an explicit
        request, or escalation) - and never when carrier_voice_attempted is
        set. An email sent moments ago in this same run does not by itself
        trigger a call.
        """
        if _no_contact_count(row) >= 1:
            return True
        if durable_evidence.get("email_sent") or durable_evidence.get(
            "carrier_email_sent"
        ):
            return True
        if row.get("voice_requested"):
            return True
        if escalated:
            return True
        return False

    def _maybe_voice(
        self,
        *,
        job: dict[str, Any],
        row: dict[str, Any],
        audit_key: str,
        policy_number: str,
        carrier: str,
        insured: str,
        voice_enabled: bool,
        escalated: bool,
        actions: list[str],
        evidence: dict[str, Any],
    ) -> bool:
        """Evaluate the voice branch. Returns True when it fired.

        HARD RULES: the target must be a carrier number from the seeded
        directory; unknown -> not_done "phone number needed", never dialed.
        Row-supplied phones are never dialed. carrier_voice_attempted is set
        when the branch fires so it never re-fires.
        """
        durable_evidence = self._durable_evidence(job, audit_key)
        if self._voice_already_attempted(job, audit_key):
            evidence["voice"] = {
                "skipped": "carrier_voice_attempted already set - once-only guard"
            }
            return False
        if not self._voice_branch_due(
            row=row,
            escalated=escalated,
            durable_evidence=durable_evidence,
        ):
            return False

        phone = self._resolve_carrier_voice_number(carrier, row)
        if phone is None:
            evidence["voice"] = {
                "blocked": True,
                "reason": "phone number needed",
                "note": "no carrier number in the audit call directory; nothing dialed",
            }
            actions.append("voice_blocked_no_phone")
            # Signal the caller to flip this policy to not_done.
            evidence["_voice_phone_needed"] = True
            return False

        if not voice_enabled:
            evidence["voice_deferred"] = True
            evidence["carrier_voice_attempted"] = True
            evidence["voice"] = {
                "deferred": True,
                "reason": "voice deferred — portal+email only at launch",
            }
            self._voice_attempted.add(audit_key)
            actions.append("voice_deferred")
            return True

        if self._voice_dispatcher is None:
            evidence["voice"] = {
                "blocked": True,
                "reason": "voice enabled but no voice dispatcher wired - no call placed",
            }
            actions.append("voice_not_wired")
            return False

        if not is_action_authorized(job, "place_carrier_voice_call"):
            evidence["voice"] = {
                "blocked": True,
                "reason": (
                    "not authorized - place_carrier_voice_call not in the job's "
                    "authorizations; no call placed"
                ),
            }
            evidence["intended_action"] = {
                "action": "place_carrier_voice_call",
                "to": phone,
                "note": "carrier voice call intended but not authorized; pending",
            }
            actions.append("voice_not_authorized")
            return False

        request = build_bland_call_request(
            policy_number=policy_number,
            named_insured=insured or policy_number,
            carrier_name=carrier,
            carrier_phone=phone,
            agency_code=_row_str(row, "agency_code") or None,
            fein=_row_str(row, "fein") or None,
            applicant_id=row.get("applicant_id"),
        )
        try:
            raw = self._voice_dispatcher.dispatch_voice_call(request)
        except Exception as exc:
            evidence["voice"] = {
                "dispatch_failed": True,
                "error": f"{type(exc).__name__}: {exc}",
                "phone_from_directory": True,
            }
            actions.append("voice_dispatch_failed")
            self._voice_attempted.add(audit_key)
            evidence["carrier_voice_attempted"] = True
            return True

        classification = classify_voice_result(raw or {})
        evidence["voice"] = {
            "call_id": (raw or {}).get("call_id"),
            "queue_status": classification.get("queue_status"),
            "call_placed": classification.get("call_placed"),
            "dispatch_status": classification.get("status"),
            "phone_from_directory": True,
            "phone": phone,
            "artifacts_pending": bool(classification.get("call_placed")),
        }
        evidence["carrier_voice_attempted"] = True
        self._voice_attempted.add(audit_key)
        if classification.get("call_placed"):
            actions.append("carrier_voice_call_placed")
            evidence["follow_up_due"] = evidence.get("follow_up_due")
        elif classification.get("retryable"):
            actions.append("carrier_voice_queue_error_retryable")
        else:
            actions.append("carrier_voice_not_placed")
        return True

    def _maybe_file_artifacts(
        self,
        *,
        job: dict[str, Any],
        row: dict[str, Any],
        actions: list[str],
        evidence: dict[str, Any],
    ) -> None:
        """File prior call artifacts from the row into an EZLynx note."""
        recording_url = _row_str(row, "call_recording_url", "recording_url")
        transcript = _row_str(row, "call_transcript", "transcript")
        if not recording_url and not transcript:
            return
        policy = {
            "policy_number": _row_str(row, "policy_number"),
            "line_of_business": _row_str(row, "line_of_business")
            or "Workers Compensation",
            "carrier": _row_str(row, "carrier", "carrier_name"),
            "call_id": _row_str(row, "call_id"),
        }
        filing = file_call_artifacts(
            policy,
            recording_url,
            transcript,
            _row_str(row, "call_summary", "summary"),
            job=job,
            note_poster=self._note_poster,
        )
        actions.append(
            "call_artifacts_filed" if filing["posted"] else "call_artifacts_noted_not_posted"
        )
        evidence["call_artifact_filing"] = {
            "posted": filing["posted"],
            "note_chars": filing["note_chars"],
            "post_blocked_reason": filing["post_blocked_reason"],
        }

    # -- perform ---------------------------------------------------------

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        action = str(job.get("action_type") or "")
        if action != JOB_TYPE:
            return WorkerResult(
                False,
                action,
                {},
                retryable=False,
                error=f"audit worker received unexpected action_type {action!r}",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        payload = dict(job.get("payload") or {})
        # Voice is APPROVED: default True; payload may explicitly disable.
        voice_enabled = bool(payload.get("voice_enabled", True))
        today = _parse_date(payload.get("as_of_date")) or date.today()

        try:
            spec = get_report_spec(AUDIT_REPORT_ID)
        except Exception as exc:
            return WorkerResult(
                False,
                action,
                {"report_id": AUDIT_REPORT_ID},
                retryable=True,
                error=f"report registry lookup failed for {AUDIT_REPORT_ID}: {exc}",
            )
        if not spec.schema_verified:
            return WorkerResult(
                False,
                action,
                {"report_id": AUDIT_REPORT_ID},
                retryable=False,
                error=f"report {AUDIT_REPORT_ID} schema is not verified",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        try:
            rows = fetch_report_rows(report_id=AUDIT_REPORT_ID)
        except Exception as exc:
            return WorkerResult(
                False,
                action,
                {"report_id": AUDIT_REPORT_ID},
                retryable=True,
                error=f"fetch_report_rows failed for report {AUDIT_REPORT_ID}: "
                f"{type(exc).__name__}: {exc}",
            )
        if not isinstance(rows, list):
            return WorkerResult(
                False,
                action,
                {"report_id": AUDIT_REPORT_ID},
                retryable=False,
                error="fetch_report_rows did not return a list",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )

        outcome_fields: list[dict[str, Any]] = []
        phone_needed_keys: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            fields = self._process_row(
                job=job, row=row, today=today, voice_enabled=voice_enabled
            )
            audit_key = fields["evidence"].get("audit_key", fields["audit_id"])
            if fields["evidence"].pop("_voice_phone_needed", False):
                # Voice branch fired but no directory number: hard block.
                fields = self._new_outcome_fields(
                    row,
                    status="not_done",
                    reason="phone number needed - no carrier number in the audit call directory; never dialed",
                    actions_taken=fields["actions_taken"],
                    waiting_on="csr",
                    evidence=fields["evidence"],
                    audit_key=audit_key,
                )
                phone_needed_keys.add(audit_key)
            outcome_fields.append(fields)

        outcome_field_names = frozenset(PolicyOutcome.__dataclass_fields__)
        outcomes = [
            PolicyOutcome(**{k: v for k, v in fields.items() if k in outcome_field_names})
            for fields in outcome_fields
        ]
        try:
            record_outcomes(self._resolve_store(job), job.get("id"), JOB_TYPE, outcomes)
        except Exception as exc:
            return WorkerResult(
                False,
                action,
                {"report_id": AUDIT_REPORT_ID},
                {"processed": len(outcome_fields)},
                retryable=True,
                error=f"record_outcomes failed: {type(exc).__name__}: {exc}",
            )

        serialized = [self._serialize_outcome(o) for o in outcomes]
        counts: dict[str, int] = {}
        for item in serialized:
            counts[item.get("status", "unknown")] = counts.get(item.get("status", "unknown"), 0) + 1
        return WorkerResult(
            True,
            action,
            {
                "report_id": AUDIT_REPORT_ID,
                "idempotency_key": idempotency_key,
            },
            {
                "outcomes": serialized,
                "counts": counts,
                "voice_enabled": voice_enabled,
                "as_of_date": today.isoformat(),
                "phone_needed": sorted(phone_needed_keys),
            },
            retryable=False,
        )


# ---------------------------------------------------------------------------
# Verifier (fresh read-back, never the worker's snapshot)
# ---------------------------------------------------------------------------


class AuditVerificationVerifier:
    """Independently verify audit_verification outcomes.

    Fresh read-back: re-reads the job's ``action`` checkpoint from the store
    and each outcome's durable_work_items row (namespace
    ``audit_verification``). Every outcome needs policy/audit identity, a
    valid status, a reason, and updated_at; done outcomes need evidence refs.
    Flags any voice call recorded as placed when voice_enabled was false, and
    any placed call lacking proof the number came from the carrier directory.
    """

    def __init__(
        self,
        *,
        store: Optional[Any] = None,
        durable: Optional[Callable[[str, str], Optional[dict[str, Any]]]] = None,
    ) -> None:
        self._store = store
        self._durable = durable

    def _fresh_action(self, job: dict[str, Any]) -> tuple[Optional[dict[str, Any]], bool]:
        """Return (fresh action checkpoint, authoritative)."""
        if self._store is None:
            return None, False
        try:
            fresh = self._store.get_checkpoint(job.get("id"), "action")
        except Exception:
            return None, False
        if not isinstance(fresh, dict):
            return None, False
        return fresh, True

    def _durable_get(
        self, namespace: str, key: str
    ) -> Optional[dict[str, Any]]:
        if self._durable is not None:
            try:
                return self._durable(namespace, key)
            except Exception:
                return None
        store = self._store
        db_path = getattr(store, "path", None)
        if not db_path:
            return None
        try:
            from .idempotency import DurableWorkLedger

            return DurableWorkLedger(db_path).get(namespace, key)
        except Exception:
            return None

    @staticmethod
    def _durable_outcome_dict(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not isinstance(row, dict):
            return None
        outcome = row.get("outcome")
        if isinstance(outcome, str):
            try:
                outcome = json.loads(outcome)
            except Exception:
                return None
        return outcome if isinstance(outcome, dict) else None

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        job_id = job.get("id")
        action_type = str(job.get("action_type") or "")
        captured_at = datetime.now(timezone.utc).isoformat()
        violations: list[str] = []
        observed: dict[str, Any] = {"job_id": job_id, "action_type": action_type}

        fresh, authoritative = self._fresh_action(job)
        if self._store is not None and fresh is None:
            violations.append("action checkpoint missing on fresh store read-back")
        if fresh is None:
            fresh = action if isinstance(action, dict) else {}
            if self._store is None:
                violations.append("no store wired - verifier fell back to the passed action snapshot")
        detail = dict(fresh.get("detail") or {})
        outcomes = detail.get("outcomes")
        if not isinstance(outcomes, list) or not outcomes:
            violations.append("no per-policy outcomes in the action checkpoint detail")
            outcomes = []
        observed["outcome_count"] = len(outcomes)
        voice_enabled = bool(detail.get("voice_enabled", False))
        observed["voice_enabled"] = voice_enabled

        checked_keys: list[str] = []
        for index, item in enumerate(outcomes):
            label = f"outcomes[{index}]"
            if not isinstance(item, dict):
                violations.append(f"{label} is not an object")
                continue
            identity = item.get("audit_id") or item.get("policy_number")
            if not identity:
                violations.append(f"{label} missing policy_number/audit identity")
                identity = f"#{index}"
            status = item.get("status")
            if status not in VALID_OUTCOME_STATUSES:
                violations.append(
                    f"{label} ({identity}) has invalid status {status!r}"
                )
            if not item.get("reason"):
                violations.append(f"{label} ({identity}) missing reason")
            if not item.get("updated_at"):
                violations.append(f"{label} ({identity}) missing updated_at")
            waiting_on = item.get("waiting_on")
            if waiting_on not in VALID_WAITING_ON:
                violations.append(
                    f"{label} ({identity}) has invalid waiting_on {waiting_on!r}"
                )
            evidence = item.get("evidence")
            if status == "done" and not evidence:
                violations.append(
                    f"{label} ({identity}) is done but carries no evidence refs"
                )
            # Voice authorization + directory-proof checks.
            if isinstance(evidence, dict):
                voice = evidence.get("voice") or {}
                if isinstance(voice, dict) and voice.get("call_placed"):
                    if not voice_enabled:
                        violations.append(
                            f"{label} ({identity}) records a placed voice call "
                            "while voice_enabled was false"
                        )
                    job_authorized = (job.get("payload") or {}).get(
                        "authorized_actions", []
                    ) or []
                    if "place_carrier_voice_call" not in job_authorized:
                        violations.append(
                            f"{label} ({identity}) records a placed voice call "
                            "without place_carrier_voice_call authorization"
                        )
                    if not voice.get("phone_from_directory"):
                        violations.append(
                            f"{label} ({identity}) records a placed voice call "
                            "without proof the number came from the carrier directory"
                        )
            # Durable mirror check.
            key = str(identity)
            checked_keys.append(key)
            durable_row = self._durable_get(DURABLE_NAMESPACE, key)
            durable_outcome = self._durable_outcome_dict(durable_row)
            if durable_outcome is None:
                violations.append(
                    f"{label} ({identity}) missing durable_work_items mirror "
                    f"(namespace {DURABLE_NAMESPACE!r})"
                )
            else:
                if durable_outcome.get("status") != status:
                    violations.append(
                        f"{label} ({identity}) status {status!r} != durable mirror "
                        f"{durable_outcome.get('status')!r}"
                    )
        observed["checked_keys"] = checked_keys
        observed["violations"] = violations

        verified = not violations
        evidence_obj = VerificationEvidence(
            method="FRESH_CHECKPOINT_AND_DURABLE_READBACK",
            source="audit_verification",
            expected={
                "outcome_count": len(outcomes),
                "all_outcomes_well_formed": True,
                "done_outcomes_have_evidence": True,
                "no_unauthorized_voice": True,
                "durable_mirror_present": True,
            },
            observed=observed,
            authoritative=authoritative and verified,
            captured_at=captured_at,
            locator=f"job:{job_id}:checkpoint:action",
        )
        return VerificationResult(
            verified,
            evidence_obj,
            retryable=False,
            error=None if verified else "; ".join(violations),
        )
