"""Resume a parked email HITL job from a Gmail reply.

Live miss: job 28bff7c8 stayed AWAITING_HUMAN_INPUT after Carlo replied
on the [ROBIE HITL] thread. Inbox created a second hermes.email_task
(a8068d3d) that then hit POLICY_SETUP_ORDER. A HITL reply must resume
the waiting job id and put the stated Coverage A–F amounts on that job.
Never invent a letter the human omitted.
"""

from __future__ import annotations

import re
from typing import Any

from .models import JobStatus
from .store import JobStore

HITL_SUBJECT_JOB_RE = re.compile(
    r"\[ROBIE HITL\]\s+Job\s+([0-9a-fA-F-]{8,})",
    re.IGNORECASE,
)
COVERAGE_KEYS = (
    "dwelling",
    "other_structures",
    "personal_property",
    "loss_of_use",
    "personal_liability",
    "medical_payments",
)
LETTER_FOR_KEY = {
    "dwelling": "A",
    "other_structures": "B",
    "personal_property": "C",
    "loss_of_use": "D",
    "personal_liability": "E",
    "medical_payments": "F",
}


def parse_hitl_job_token(subject: str, body: str = "") -> str | None:
    """Return the Job id token from a [ROBIE HITL] subject or quoted body."""
    for raw in (subject, body):
        match = HITL_SUBJECT_JOB_RE.search(str(raw or ""))
        if match:
            return match.group(1).strip()
    return None


def resolve_parked_email_hitl_job(
    store: JobStore,
    token: str,
) -> dict[str, Any] | None:
    """Resolve a subject token (full uuid or 8-char prefix) to one parked job."""
    token = str(token or "").strip()
    if not token:
        return None
    folded = token.casefold()
    compact = folded.replace("-", "")
    try:
        job = store.get_job(token)
    except KeyError:
        job = None
    if job is None:
        matches: list[dict[str, Any]] = []
        for row in store.list_jobs_by_status({JobStatus.AWAITING_HUMAN_INPUT}):
            job_id = str(row.get("id") or "")
            if job_id.casefold().startswith(folded) or job_id.replace(
                "-", ""
            ).casefold().startswith(compact):
                matches.append(row)
        if len(matches) != 1:
            return None
        job = matches[0]
    if str(job.get("action_type") or "") != "hermes.email_task":
        return None
    if JobStatus(job["status"]) != JobStatus.AWAITING_HUMAN_INPUT:
        return None
    return job


def find_parked_email_hitl_job(
    store: JobStore,
    *,
    subject: str = "",
    body: str = "",
    thread_id: str = "",
) -> dict[str, Any] | None:
    """Find the AWAITING_HUMAN_INPUT email job this Gmail reply belongs to."""
    token = parse_hitl_job_token(subject, body)
    if token:
        found = resolve_parked_email_hitl_job(store, token)
        if found:
            return found
    thread = str(thread_id or "").strip()
    if not thread:
        return None
    matches = []
    for row in store.list_jobs_by_status({JobStatus.AWAITING_HUMAN_INPUT}):
        if str(row.get("action_type") or "") != "hermes.email_task":
            continue
        payload = dict(row.get("payload") or {})
        if str(payload.get("gmail_thread_id") or "").strip() == thread:
            matches.append(row)
    if len(matches) == 1:
        return matches[0]
    return None


def missing_coverage_letters(amounts: dict[str, Any] | None) -> list[str]:
    """Return Coverage A–F letters that have no dollar amount. Do not invent."""
    values = dict(amounts or {})
    missing: list[str] = []
    for key, letter in LETTER_FOR_KEY.items():
        raw = str(values.get(key) or "").strip()
        if not raw:
            missing.append(letter)
    return missing


def policy_setup_args_from_hitl_payload(payload: dict[str, Any]) -> dict[str, str]:
    """Build ezlynx_policy_setup args from the parked job + HITL amounts."""
    from .policy_setup_dispatch import GOLD_EFFECTIVE_DATE, GOLD_EXPIRATION_DATE

    payload = dict(payload or {})
    human = dict(payload.get("human_input_values") or {})
    coverage = dict(human.get("coverage") or {})
    args = {
        "policy_number": str(
            payload.get("policy_number") or coverage.get("policy_number") or ""
        ).strip(),
        "effective_date": str(
            payload.get("effective_date") or GOLD_EFFECTIVE_DATE
        ).strip(),
        "expiration_date": str(
            payload.get("expiration_date") or GOLD_EXPIRATION_DATE
        ).strip(),
    }
    for key in COVERAGE_KEYS:
        value = str(coverage.get(key) or payload.get(key) or "").strip()
        if value:
            args[key] = value
    return args


def ingest_email_hitl_reply(
    store: JobStore,
    *,
    job_id: str,
    gmail_message_id: str,
    subject: str,
    body: str,
    thread_id: str = "",
) -> dict[str, Any]:
    """Merge the reply onto the parked job and resume it. No second job_intake."""
    from .engine import is_retry_text
    from .policy_setup_dispatch import parse_coverage_amounts_from_reply

    job = store.get_job(job_id)
    if JobStatus(job["status"]) != JobStatus.AWAITING_HUMAN_INPUT:
        raise RuntimeError(f"job {job_id} is not awaiting human input")
    reply = str(body or "")
    amounts = parse_coverage_amounts_from_reply(f"{subject}\n{reply}")
    payload = dict(job.get("payload") or {})
    human = dict(payload.get("human_input_values") or {})
    coverage = dict(human.get("coverage") or {})
    coverage.update(amounts)
    human["coverage"] = coverage
    if is_retry_text(reply):
        human["operator_response"] = "RETRY"
    payload["human_input_values"] = human
    payload["hitl_resume"] = True
    payload["hitl_reply_text"] = reply
    payload["hitl_reply_gmail_message_id"] = str(gmail_message_id or "").strip()
    if thread_id:
        payload["gmail_thread_id"] = str(thread_id).strip()
    store.update_payload(job_id, payload)
    store.checkpoint(
        job_id,
        "human_input_resume",
        {
            "channel": "email",
            "gmail_message_id": str(gmail_message_id or "").strip(),
            "coverage_keys": sorted(coverage.keys()),
            "missing_letters": missing_coverage_letters(coverage),
        },
    )
    store.checkpoint(
        job_id,
        f"gmail_hitl_reply:{gmail_message_id}",
        {"job_id": job_id, "resumed": True},
    )
    resumed = store.resume(job_id)
    return {
        "resumed": True,
        "job_id": job_id,
        "coverage": coverage,
        "missing_letters": missing_coverage_letters(coverage),
        "status": resumed["status"],
    }


def apply_hitl_coverage_fill(store: JobStore, job_id: str) -> str:
    """Re-run ezlynx_policy_setup with HITL amounts on the parked job.

    FormEntry may already be open. Do not fall through to playwright_exec.
    Do not invent a Coverage letter the human omitted — ask again for it.
    """
    from .hitl_copy import missing_coverage_letters_human_text
    from .policy_setup_dispatch import (
        POLICY_SETUP_REQUIRED_KIND,
        PolicySetupToolMissing,
        invoke_policy_setup_tool,
        is_formentry_mint_miss,
        is_policy_setup_fail_closed,
    )

    job = store.get_job(job_id)
    payload = dict(job.get("payload") or {})
    channel = (
        "email"
        if str(job.get("action_type") or "") == "hermes.email_task"
        else "chat"
    )
    action = store.get_checkpoint(job_id, "action") or {}
    dest = dict(action.get("destination") or {})
    args = policy_setup_args_from_hitl_payload(payload)
    if not args.get("policy_number"):
        args["policy_number"] = str(dest.get("policy_number") or "").strip()
    coverage = dict((payload.get("human_input_values") or {}).get("coverage") or {})
    coverage_sig = [[key, str(coverage.get(key) or "")] for key in COVERAGE_KEYS]
    existing_fill = store.get_checkpoint(job_id, "hitl_coverage_fill") or {}
    if existing_fill.get("coverage_sig") == coverage_sig and existing_fill.get("text"):
        return str(existing_fill["text"])
    try:
        report = invoke_policy_setup_tool(args)
    except PolicySetupToolMissing as exc:
        return f"ROBIE_OUTCOME_UNKNOWN: {exc}"
    actual = report
    if isinstance(report, dict):
        for key in ("result", "data", "report"):
            if isinstance(report.get(key), dict):
                actual = report[key]
                break
    success = bool(isinstance(actual, dict) and actual.get("success"))
    policy_id = str(
        dest.get("policy_id")
        or payload.get("policy_id")
        or (actual.get("policy_id") if isinstance(actual, dict) else "")
        or ""
    ).strip()
    destination = {
        "policy_number": args.get("policy_number") or dest.get("policy_number"),
        "applicant_id": str(payload.get("applicant_id") or dest.get("applicant_id") or "220250093"),
    }
    if policy_id:
        destination["policy_id"] = policy_id
        payload["policy_id"] = policy_id
    if destination.get("policy_number"):
        payload["policy_number"] = destination["policy_number"]
    store.update_payload(job_id, payload)
    store.checkpoint(
        job_id,
        POLICY_SETUP_REQUIRED_KIND,
        {
            "policy_number": destination.get("policy_number"),
            "tool_called": True,
            "setup_complete": bool(success),
            "hitl_resume": True,
        },
    )
    store.checkpoint(
        job_id,
        "action",
        {
            "action": "ezlynx_policy_setup",
            "destination": destination,
            "detail": {"report": actual} if success else {"error": actual},
        },
    )
    fail_text = ""
    if isinstance(actual, dict):
        fail_text = str(actual.get("error") or actual.get("message") or "")
    missing = missing_coverage_letters(coverage)
    if missing and not is_formentry_mint_miss(fail_text) and not is_policy_setup_fail_closed(fail_text):
        have = [letter for key, letter in LETTER_FOR_KEY.items() if coverage.get(key)]
        text = missing_coverage_letters_human_text(
            channel=channel, missing=missing, have=have
        )
    elif success:
        text = (
            f"Policy {destination.get('policy_number')} coverage fill used the "
            "amounts from the HITL reply. Verify the FormEntry before relying on it."
        )
    else:
        text = (
            f"ROBIE_OUTCOME_UNKNOWN: Coverage fill after HITL reply did not complete "
            f"({fail_text or 'handler reported failure'}). "
            "Check the EZLynx destination before trying again."
        )
    store.checkpoint(
        job_id,
        "hitl_coverage_fill",
        {
            "channel": channel,
            "coverage_sig": coverage_sig,
            "missing_letters": missing,
            "text": text,
        },
    )
    return text


def _subject_from_prompt(prompt: str) -> str:
    text = str(prompt or "")
    if text.lower().startswith("subject:"):
        return text.split("\n", 1)[0].split(":", 1)[-1].strip()
    return ""
