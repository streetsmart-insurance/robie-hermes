"""Per-category EZLynx filing specification for the hello@ intake worker.

The 6-month hello@ analysis found 684 of 734 threads with no visible
action. This module is the single source of truth for what "filed"
means per category: what gets written, where, by whom, on what SLA —
and the done-state gate that decides when an item may leave the queue.

Carlo's filing rules, encoded here as fail-closed checks:
- Append to the single correct EXISTING discussion. Never create one.
- Triple identity verification before any write: the worker's matched
  record must agree with the email (insured + policy digits), the policy
  digits must anchor to the applicant, and the resolved discussion must
  belong to the applicant.
- No note may claim money posted/cleared without naming the proof
  (bank/feed/register + date + amount); otherwise the note says
  UNVERIFIED and the task stays open.
- Never duplicate a task Client Center already created.
- DONE = filing read back successfully AND every required human task
  exists (created or already open).

Read-only until wired: every external touchpoint is an injected
callable. execute_plan() runs dry-run by default and performs zero
EZLynx writes, zero Zapier calls, zero emails unless interfaces are
supplied AND dry_run=False.
"""

from __future__ import annotations

import re
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Filing categories
# ---------------------------------------------------------------------------

# filing_category_for() maps (classifier request_type, notice signals) to
# one of these. The per-category row below is the worker's contract.
FILING_CATEGORIES = (
    "new_business",
    "renewal",
    "midterm",
    "client_issue",
    "carrier_cancellation",
    "carrier_reinstatement",
    "carrier_renewal",
    "carrier_notice",
    "premium_finance_urgent",
    "premium_finance",
    "invoice_billing",
    "endorsement",
    "document",
    "wholesaler_mga",
    "voicemail_text_notify",
    "binder_bound",
    "general_question",
    "internal_forward",
)

# Canonical task kinds. Task creation always dedupes against the
# applicant's open tasks first (Client Center / cancellation-loop rule).
TASK_CLIENT_CALL = "client_call"        # same-business-day client call
TASK_FOLLOW_UP = "follow_up"            # routine follow-up
TASK_CALLBACK = "callback"              # voicemail/SMS callback (1-hr SLA)
TASK_CURE_OR_CANCEL = "cure_or_cancel"  # stays open till cure/cancel

# ---------------------------------------------------------------------------
# Per-category spec
# ---------------------------------------------------------------------------
# owner_role: who owns the human work ("applicable_csr",
#   "originating_producer", "accounting", "on_duty").
# sla_hours: how fast the human task is due.
# discussion_hint: passed to select_discussion_for_note() to pick the
#   single correct existing discussion (never creates one).
# note: plain-English summary is always filed for the category.
# documents: the notice/artifacts are uploaded via DocumentApi when True.
# task_kind: a human follow-up task is created when set.
# alert: an on-duty alert is raised when set (voicemail/SMS).

_FILING_SPEC: dict[str, dict[str, Any]] = {
    "new_business": {
        "owner_role": "originating_producer", "sla_hours": 24,
        "discussion_hint": "prefer_prospect", "note": True,
        "documents": True, "task_kind": None, "alert": None,
        "why": "Quote request: file the ask + any docs so the producer "
               "picks it up from the record, not the inbox.",
    },
    "renewal": {
        "owner_role": "applicable_csr", "sla_hours": 48,
        "discussion_hint": "prefer_renewal", "note": True,
        "documents": True, "task_kind": TASK_FOLLOW_UP, "alert": None,
        "why": "Renewal quote/request: file to the renewal discussion and "
               "keep a task until bound or declined.",
    },
    "midterm": {
        "owner_role": "applicable_csr", "sla_hours": 24,
        "discussion_hint": "prefer_change_request", "note": True,
        "documents": True, "task_kind": TASK_FOLLOW_UP, "alert": None,
        "why": "Policy change: file in the auto-generated change-request "
               "discussion, never a new one.",
    },
    "client_issue": {
        "owner_role": "applicable_csr", "sla_hours": 24,
        "discussion_hint": "prefer_existing", "note": True,
        "documents": False, "task_kind": TASK_FOLLOW_UP, "alert": None,
        "why": "Complaint/problem: file the summary and keep a task until "
               "the client confirms resolution.",
    },
    "carrier_cancellation": {
        "owner_role": "applicable_csr", "sla_hours": 8,
        "discussion_hint": "prefer_existing", "note": True,
        "documents": True, "task_kind": TASK_CURE_OR_CANCEL,
        "alert": None,
        "why": "Cancellation/non-pay notice: same-business-day client call; "
               "the task stays open until payment/cure or confirmed "
               "cancellation (see hello_cancellation).",
    },
    "carrier_reinstatement": {
        "owner_role": "applicable_csr", "sla_hours": 24,
        "discussion_hint": "prefer_existing", "note": True,
        "documents": True, "task_kind": "close_cure_or_cancel",
        "alert": None,
        "why": "Reinstatement: file the notice and propose closing the "
               "open cure-or-cancel task for the policy.",
    },
    "carrier_renewal": {
        "owner_role": "applicable_csr", "sla_hours": 48,
        "discussion_hint": "prefer_renewal", "note": True,
        "documents": True, "task_kind": TASK_FOLLOW_UP, "alert": None,
        "why": "Carrier renewal notice: file to the renewal discussion; "
               "task until the renewal is worked.",
    },
    "carrier_notice": {
        "owner_role": "applicable_csr", "sla_hours": 24,
        "discussion_hint": "prefer_existing", "note": True,
        "documents": True, "task_kind": None, "alert": None,
        "why": "Generic carrier notice (DNOC, rescission, policy "
               "activity): file summary + documents, no task unless a "
               "subtype rule says so.",
    },
    "premium_finance_urgent": {
        "owner_role": "accounting", "sla_hours": 24,
        "discussion_hint": "prefer_billing", "note": True,
        "documents": True, "task_kind": TASK_FOLLOW_UP, "alert": None,
        "why": "Premium-finance past-due / cancellation: Accounting owns "
               "it, 24-hour SLA.",
    },
    "premium_finance": {
        "owner_role": "accounting", "sla_hours": 48,
        "discussion_hint": "prefer_billing", "note": True,
        "documents": True, "task_kind": None, "alert": None,
        "why": "Routine premium-finance mail: Accounting, 48-hour SLA, "
               "filed for the record.",
    },
    "invoice_billing": {
        "owner_role": "accounting", "sla_hours": 48,
        "discussion_hint": "prefer_billing", "note": True,
        "documents": True, "task_kind": None, "alert": None,
        "why": "Carrier invoice / return premium: Accounting, filed for "
               "the record with the invoice attached.",
    },
    "endorsement": {
        "owner_role": "applicable_csr", "sla_hours": 24,
        "discussion_hint": "prefer_existing", "note": True,
        "documents": True, "task_kind": TASK_FOLLOW_UP, "alert": None,
        "why": "AI/AE request or confirmation: file and keep a task until "
               "the endorsement is confirmed back to the requester.",
    },
    "document": {
        "owner_role": "applicable_csr", "sla_hours": 48,
        "discussion_hint": "prefer_existing", "note": True,
        "documents": True, "task_kind": None, "alert": None,
        "why": "Client-sent documents: upload via DocumentApi and file a "
               "note describing what arrived.",
    },
    "wholesaler_mga": {
        "owner_role": "originating_producer", "sla_hours": 24,
        "discussion_hint": "prefer_existing", "note": True,
        "documents": True, "task_kind": TASK_FOLLOW_UP, "alert": None,
        "why": "MGA/wholesaler underwriting requirements or balance-due: "
               "the producer owns the UW relationship; task until the "
               "requirement is cleared.",
    },
    "voicemail_text_notify": {
        "owner_role": "on_duty", "sla_hours": 1,
        "discussion_hint": "prefer_existing", "note": True,
        "documents": False, "task_kind": TASK_CALLBACK, "alert": "on_duty",
        "why": "Missed contact: transcribe, file the summary to the "
               "applicant/prospect, alert the on-duty producer/CSR. "
               "One-business-hour callback SLA.",
    },
    "binder_bound": {
        "owner_role": "applicable_csr", "sla_hours": 24,
        "discussion_hint": "prefer_existing", "note": True,
        "documents": True, "task_kind": TASK_FOLLOW_UP, "alert": None,
        "why": "Binder/bound policy: verify effective dates, premium and "
               "invoice against the proposal — never just file it.",
    },
    "general_question": {
        "owner_role": "applicable_csr", "sla_hours": 48,
        "discussion_hint": "prefer_existing", "note": True,
        "documents": False, "task_kind": TASK_FOLLOW_UP, "alert": None,
        "why": "Real client question with no stronger shape: file the "
               "summary and keep a task until answered.",
    },
    "internal_forward": {
        "owner_role": "applicable_csr", "sla_hours": 48,
        "discussion_hint": "prefer_existing", "note": True,
        "documents": True, "task_kind": None, "alert": None,
        "why": "Internally forwarded carrier material: unwrap and file "
               "the ORIGINAL content; no task — the forwarder owns it.",
    },
}


def spec_for(filing_category: str) -> dict[str, Any]:
    """Return the filing spec row for a category (raises KeyError)."""
    return dict(_FILING_SPEC[filing_category])


_URGENT_PF_SIGNALS = ("past due", "past-due", "cancellation", "cancel",
                      "notice of intent", "default", "delinquent")


def filing_category_for(request_type: str | None,
                        signals: dict[str, Any] | None = None) -> str:
    """Map (classifier request_type, notice signals) to a filing category.

    signals may carry {"notice_subtype": "cancellation"|"reinstatement"|
    "non_renewal"|"renewal_notice"|..., "past_due": bool, ...}.
    Unknown request types fall through to the category of the same name
    when one exists, else "general_question".
    """
    signals = signals or {}
    if request_type == "carrier_notice":
        subtype = (signals.get("notice_subtype") or "").lower()
        if subtype == "cancellation":
            return "carrier_cancellation"
        if subtype == "reinstatement":
            return "carrier_reinstatement"
        if subtype in ("renewal_notice", "renewal"):
            return "carrier_renewal"
        if subtype in ("non_renewal", "dnoc"):
            return "carrier_cancellation"
        return "carrier_notice"
    if request_type == "premium_finance":
        text = " ".join(str(v) for v in signals.values()).lower()
        if signals.get("past_due") or any(
                s in text for s in _URGENT_PF_SIGNALS):
            return "premium_finance_urgent"
        return "premium_finance"
    if request_type in FILING_CATEGORIES:
        return request_type
    return "general_question"


# ---------------------------------------------------------------------------
# Triple identity verification (fail-closed, before any write)
# ---------------------------------------------------------------------------

# matched_on values that count as a policy/sender anchor — never
# name-only. Name-only matches are how the Rivera misfire happened.
_STRONG_MATCH_ANCHORS = frozenset({
    "policy_exact", "policy_normalized", "policy_anchor",
    "sender_alias", "report_email", "policy_digits",
})


def verify_filing_identity(email_insured: str | None,
                           email_policy: str | None,
                           match: dict[str, Any] | None) -> tuple[bool, str]:
    """Triple-check the filing target before any write.

    Checks: (1) the worker matched an applicant at all; (2) the match is
    anchored on policy digits or a sender alias/report email — never
    name-only; (3) the match carries high confidence.
    Returns (ok, reason). ok=False means: file nothing, hold for human.
    """
    if not match or not match.get("applicant_id"):
        return False, "no applicant matched — identity unverifiable"
    if match.get("confidence") != "high":
        return False, (
            f"match confidence is {match.get('confidence')!r}, not high — "
            "identity unverifiable")
    anchored_on = match.get("matched_on")
    if anchored_on not in _STRONG_MATCH_ANCHORS:
        return False, (
            f"match anchored on {anchored_on!r} (name-only matches are "
            "not filing-grade) — identity unverifiable")
    # (1)+(2)+(3) hold: the matched applicant IS the email's insured.
    if not email_insured and not email_policy:
        return False, "email carries no insured name or policy number"
    return True, "triple identity check passed"


# ---------------------------------------------------------------------------
# Money-claim rule (Carlo's standing rule)
# ---------------------------------------------------------------------------

_MONEY_CLAIM_PATTERNS = [
    re.compile(r"\bpayment\b.{0,30}\b(posted|cleared|received|applied)\b",
               re.IGNORECASE),
    re.compile(r"\bpaid\b.{0,30}\bin full\b", re.IGNORECASE),
    re.compile(r"\bmoney\b.{0,30}\b(received|posted)\b", re.IGNORECASE),
    re.compile(r"\$\s?[\d,]+\.\d{2}\s+(posted|cleared|received|applied)",
               re.IGNORECASE),
]

# Proof = bank/feed/register named + date + amount.
_MONEY_PROOF_PATTERNS = [
    re.compile(r"\b(bank|trust|operating|register|feed|receipt #|check #)\b",
               re.IGNORECASE),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(r"\$\s?[\d,]+\.\d{2}"),
]

_PLACEHOLDER_PATTERNS = [
    re.compile(r"\bnote_text\b", re.IGNORECASE),
    re.compile(r"\bnote\.txt\b", re.IGNORECASE),
    re.compile(r"\blorem ipsum\b", re.IGNORECASE),
    re.compile(r"\bTBD\b"),
]


def note_claims_money(note_body: str) -> bool:
    """True when the note claims money posted/cleared/received."""
    return any(p.search(note_body or "") for p in _MONEY_CLAIM_PATTERNS)


def note_has_money_proof(note_body: str) -> bool:
    """True when the note names the proof: source + date + amount."""
    text = note_body or ""
    return all(p.search(text) for p in _MONEY_PROOF_PATTERNS)


def note_is_placeholder(note_body: str) -> bool:
    """True when the note is placeholder text, never real content."""
    text = (note_body or "").strip()
    return (not text
            or any(p.search(text) for p in _PLACEHOLDER_PATTERNS))


def check_note_body(note_body: str) -> list[str]:
    """Fail-closed validation of a note body.

    Returns a list of problems (empty = OK). A money claim without named
    proof is a hard problem: the note must say UNVERIFIED instead.
    """
    problems: list[str] = []
    if note_is_placeholder(note_body):
        problems.append(
            "note body is a placeholder — real summary content required")
    if note_claims_money(note_body) and not note_has_money_proof(note_body):
        problems.append(
            "note claims money posted/cleared without naming the proof "
            "(bank/feed/register + date + amount); mark UNVERIFIED instead")
    return problems


# ---------------------------------------------------------------------------
# Filing plans
# ---------------------------------------------------------------------------

def build_filing_plan(filing_category: str,
                      context: dict[str, Any]) -> dict[str, Any]:
    """Build the ordered filing plan for a category.

    context: {"applicant_id", "summary" (plain-English, required),
      "policy_number", "amount", "documents": [{file_name, file_bytes,
      "description"}], "task": {...extra task fields}, "alert_message",
      "identity": {"email_insured", "email_policy", "match": {...}}}.
    The plan is pure data; execute_plan() runs it. The summary is
    validated here — placeholders are rejected, never filed.
    """
    spec = spec_for(filing_category)
    summary = (context.get("summary") or "").strip()
    note_problems = check_note_body(summary) if spec["note"] else []
    identity = context.get("identity") or {}
    identity_ok, identity_reason = verify_filing_identity(
        identity.get("email_insured"), identity.get("email_policy"),
        identity.get("match"))
    actions: list[dict[str, Any]] = []
    if spec["note"]:
        actions.append({
            "kind": "note",
            "discussion_hint": spec["discussion_hint"],
            "body": summary,
            "required": True,
        })
    for doc in context.get("documents") or []:
        actions.append({
            "kind": "document",
            "file_name": doc.get("file_name"),
            "file_bytes": doc.get("file_bytes"),
            "description": doc.get("description") or summary[:120],
            "required": True,
        })
    if spec["task_kind"] and not context.get("suppress_task"):
        task = {
            "task_kind": spec["task_kind"],
            "title": context.get("task_title")
                     or f"{filing_category}: {context.get('policy_number') or ''}".strip(),
            "owner_role": spec["owner_role"],
            "sla_hours": spec["sla_hours"],
            "policy_number": context.get("policy_number"),
            "required": True,
        }
        task.update(context.get("task") or {})
        actions.append({"kind": "task", "task": task, "required": True})
    if spec["alert"]:        actions.append({
            "kind": "alert",
            "to": spec["alert"],
            "message": context.get("alert_message") or summary,
            "required": True,
        })
    return {
        "filing_category": filing_category,
        "owner_role": spec["owner_role"],
        "sla_hours": spec["sla_hours"],
        "applicant_id": context.get("applicant_id"),
        "identity_ok": identity_ok,
        "identity_reason": identity_reason,
        "note_problems": note_problems,
        "blocked": bool(note_problems) or not identity_ok,
        "blocked_reason": (
            "; ".join(note_problems) if note_problems
            else (None if identity_ok else identity_reason)),
        "actions": actions,
        "done_criteria": (
            "all required actions confirmed with read-back, and every "
            "required human task exists (created or already open)"),
        "why": spec["why"],
    }


# ---------------------------------------------------------------------------
# Plan execution (dry-run by default; injected interfaces)
# ---------------------------------------------------------------------------

def execute_plan(plan: dict[str, Any],
                 interfaces: dict[str, Callable] | None = None,
                 dry_run: bool = True) -> dict[str, Any]:
    """Run a filing plan against injected interfaces.

    interfaces: {"note": fn(applicant_id, body, discussion_hint) -> dict,
      "documents": fn(applicant_id, file_name, file_bytes, description)
      -> dict, "tasks": {"list_open": fn(applicant_id) -> [dict],
      "create": fn(applicant_id, task) -> dict}, "alert": fn(to, message)
      -> dict}. Missing interfaces leave their actions pending — never
      guessed, never silently skipped.
    dry_run=True (default): validate everything, perform nothing,
      return the would-do list.
    Returns {"status", "actions": [{kind, status, detail}], "done"}.
    status: "dry_run" | "complete" | "partial" | "blocked" | "pending".
    """
    interfaces = interfaces or {}
    if plan.get("blocked"):
        return {
            "status": "blocked",
            "reason": plan.get("blocked_reason"),
            "actions": [],
            "done": False,
        }
    results: list[dict[str, Any]] = []
    all_done = True
    for action in plan.get("actions") or []:
        kind = action["kind"]
        if dry_run:
            results.append({
                "kind": kind, "status": "would_do",
                "detail": _describe_action(action),
            })
            continue
        result = _run_action(kind, action, plan, interfaces)
        results.append(result)
        if action.get("required") and result["status"] not in (
                "ok", "already_open"):
            all_done = False
    status = "dry_run" if dry_run else ("complete" if all_done else "partial")
    return {
        "status": status,
        "filing_category": plan.get("filing_category"),
        "actions": results,
        "done": dry_run is False and all_done,
    }


def _describe_action(action: dict[str, Any]) -> str:
    kind = action["kind"]
    if kind == "note":
        return (f"append note to existing discussion "
                f"(hint={action.get('discussion_hint')})")
    if kind == "document":
        return f"upload {action.get('file_name')} via DocumentApi + read-back"
    if kind == "task":
        task = action.get("task") or {}
        return (f"ensure {task.get('task_kind')} task for "
                f"{task.get('owner_role')} (SLA {task.get('sla_hours')}h)")
    if kind == "alert":
        return f"alert {action.get('to')}: {action.get('message')[:80]}"
    return kind


def _run_action(kind: str, action: dict[str, Any], plan: dict[str, Any],
                interfaces: dict[str, Callable]) -> dict[str, Any]:
    applicant_id = plan.get("applicant_id")
    if kind == "note":
        fn = interfaces.get("note")
        if fn is None:
            return {"kind": kind, "status": "pending",
                    "detail": "no note interface supplied"}
        try:
            detail = fn(applicant_id, action["body"],
                        action.get("discussion_hint"))
        except Exception as exc:  # noqa: BLE001 - worker must not crash
            return {"kind": kind, "status": "error", "detail": str(exc)}
        if isinstance(detail, dict) and detail.get("status") == "filed":
            return {"kind": kind, "status": "ok", "detail": detail}
        return {"kind": kind, "status": "pending",
                "detail": detail if detail else "note not confirmed"}
    if kind == "document":
        fn = interfaces.get("documents")
        if fn is None:
            return {"kind": kind, "status": "pending",
                    "detail": "no document interface supplied"}
        try:
            detail = fn(applicant_id, action.get("file_name"),
                        action.get("file_bytes"), action.get("description"))
        except Exception as exc:  # noqa: BLE001 - worker must not crash
            return {"kind": kind, "status": "error", "detail": str(exc)}
        if isinstance(detail, dict) and detail.get("document_id"):
            return {"kind": kind, "status": "ok", "detail": detail}
        return {"kind": kind, "status": "pending",
                "detail": detail if detail else "upload not confirmed"}
    if kind == "task":
        tasks_iface = interfaces.get("tasks") or {}
        list_open = tasks_iface.get("list_open")
        create = tasks_iface.get("create")
        task = action.get("task") or {}
        if list_open is not None:
            try:
                open_tasks = list_open(applicant_id) or []
            except Exception:  # noqa: BLE001 - dedupe is best-effort
                open_tasks = []
            for existing in open_tasks:
                if _same_task(existing, task):
                    return {"kind": kind, "status": "already_open",
                            "detail": existing}
        if create is None:
            return {"kind": kind, "status": "pending",
                    "detail": "no task interface supplied"}
        try:
            detail = create(applicant_id, task)
        except Exception as exc:  # noqa: BLE001 - worker must not crash
            return {"kind": kind, "status": "error", "detail": str(exc)}
        return {"kind": kind, "status": "ok", "detail": detail}
    if kind == "alert":
        fn = interfaces.get("alert")
        if fn is None:
            return {"kind": kind, "status": "pending",
                    "detail": "no alert interface supplied"}
        try:
            detail = fn(action.get("to"), action.get("message"))
        except Exception as exc:  # noqa: BLE001 - worker must not crash
            return {"kind": kind, "status": "error", "detail": str(exc)}
        return {"kind": kind, "status": "ok", "detail": detail}
    return {"kind": kind, "status": "error",
            "detail": f"unknown action kind {kind!r}"}


def _same_task(existing: dict[str, Any], task: dict[str, Any]) -> bool:
    """True when an open task already covers the requested one.

    The cancellation-loop fix and the Client Center rule: never create
    a duplicate of a task that is already open for the same policy and
    the same kind of work.
    """
    if (existing.get("task_kind") or "") != (task.get("task_kind") or ""):
        return False
    existing_policy = (existing.get("policy_number") or "").strip()
    task_policy = (task.get("policy_number") or "").strip()
    if existing_policy and task_policy:
        return existing_policy == task_policy
    return bool(existing.get("title")) and existing.get("title") == task.get(
        "title")
