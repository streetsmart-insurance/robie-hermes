"""Worker / Chat contract for destination progress claims.

Job 468d1575 stored success-shaped ``worker_response`` prose ("Filling Policy
Shell" / identified a carrier from quote data) with zero destination-action
checkpoints and zero destination-verified evidence. Chat already suppresses
that wording on UNVERIFIED/FAILED and does not authorize COMPLETE. This
module rewrites the stored worker_response so the ledger cannot look like
success unless a destination checkpoint or destination-verified row exists.

Never weakens destination-verification. Never authorizes COMPLETE.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


UNVERIFIED_STUCK_TEXT = (
    "ROBIE made no verified destination progress. "
    "There was no destination-action checkpoint and no destination-verified "
    "evidence. The worker was stuck."
)

# 468d1575-shaped and sibling fill/save claims. A genuine ROBIE_BLOCKED /
# PLAYWRIGHT_BLOCKED line is not rewritten.
_SUCCESS_PROGRESS_MARKERS = (
    "filling policy shell",
    "filling the policy shell",
    "filling the shell",
    "i am filling",
    "i'm filling",
    "remaining policy shell",
    "policy shell details",
    "i identified",
    "identified drive",
    "i filled",
    "i uploaded",
    "i saved",
    "saved and continue",
    "i created the policy",
    "i set up the policy",
    "i finished setting up the policy",
    "policy has been set up",
    "policy was created",
    "policy setup is complete",
    "successfully set up the policy",
    "successfully created the policy",
    "the policy is set up",
    "the insurance policy is set up",
    "the destination looks good",
    "destination looks good",
    "i did it",
    "i've done it",
    "the job is complete",
    "job is complete",
    "completed successfully",
    "i completed",
    "marked complete",
    "status: complete",
    "— complete",
    "- complete",
)


_COMPLETE_SHAPED_EXACT = frozenset(
    {
        "done",
        "success",
        "complete",
        "finished",
        "ok",
        "all done",
        "updated",
        "updated.",
    }
)
_DESTINATION_PROGRESS_MARKERS = (
    "i clicked",
    "i uploaded",
    "i entered",
    "i opened",
    "i navigated",
    "i attached",
    "clicked upload",
)
_INFRA_ERROR_RE = re.compile(
    r"(PLAYWRIGHT_BLOCKED[^\n]*|"
    r"ROBIE_BLOCKED[^\n]*|"
    r"PLAYWRIGHT_FAIL_CLOSED[^\n]*|"
    r"PLAYWRIGHT_TIMEOUT[^\n]*|"
    r"ECONNREFUSED[^\n]*|"
    r"(?:\[Errno 111\] )?Connection refused[^\n]*|"
    r"HTTP 429[^\n]*|"
    r"status(?: code)? 429[^\n]*|"
    r"Too Many Requests[^\n]*)",
    re.IGNORECASE,
)
_BLOCKER_TOKEN_RE = re.compile(
    r"PLAYWRIGHT_BLOCKED|ROBIE_BLOCKED|MISSING_REQUIRED_FIELD|PLAYWRIGHT_FAIL_CLOSED",
    re.IGNORECASE,
)


def claims_unverified_destination_progress(content: str) -> bool:
    """True when prose claims a fill/save/identify from quote data."""
    text = str(content or "")
    if "ROBIE_BLOCKED" in text or "PLAYWRIGHT_BLOCKED" in text:
        return False
    if "PLAYWRIGHT_FAIL_CLOSED" in text:
        return False
    normalized = " ".join(text.casefold().split())
    if not normalized:
        return False
    return any(marker in normalized for marker in _SUCCESS_PROGRESS_MARKERS)


def is_complete_shaped_close(content: str) -> bool:
    """True for a short I-finished / Done close without an infra error."""
    text = str(content or "")
    if _BLOCKER_TOKEN_RE.search(text) or _INFRA_ERROR_RE.search(text):
        return False
    normalized = " ".join(text.casefold().split()).strip(" .!?\t")
    if not normalized:
        return False
    if normalized in _COMPLETE_SHAPED_EXACT:
        return True
    return normalized.startswith("success:")


def claimed_destination_progress_or_complete(content: str) -> bool:
    """Worker claimed destination work or tried to complete without evidence."""
    if claims_unverified_destination_progress(content):
        return True
    if is_complete_shaped_close(content):
        return True
    text = str(content or "")
    if _BLOCKER_TOKEN_RE.search(text):
        return False
    normalized = " ".join(text.casefold().split())
    return any(marker in normalized for marker in _DESTINATION_PROGRESS_MARKERS)


def extract_infra_close_error(content: str, last_error: str | None = None) -> str | None:
    """Return PLAYWRIGHT_BLOCKED / CDP ECONNREFUSED / 429 text when present."""
    blob = "\n".join(part for part in (str(content or ""), str(last_error or "")) if part)
    if not blob.strip():
        return None
    match = _INFRA_ERROR_RE.search(blob)
    if not match:
        return None
    return match.group(0).strip()[:1_000]


@dataclass(frozen=True)
class ChatCloseDecision:
    status: str
    error: str
    reason: str


def classify_chat_close_without_checkpoint(
    content: str,
    *,
    action: Any = None,
    last_error: str | None = None,
    verifier_missing: bool = False,
    action_type: str = "",
) -> ChatCloseDecision:
    """Choose FAILED / HITL / UNVERIFIED when no destination verifier ran.

    COMPLETE stays blocked elsewhere. A destination-action checkpoint plus a
    missing verifier, or a success/progress claim without evidence, stays
    UNVERIFIED. Infra that never claimed destination success is not masked as
    "no structured destination action checkpoint".
    """
    if action and verifier_missing:
        kind = str(action_type or "").strip() or "this action"
        return ChatCloseDecision(
            status="UNVERIFIED",
            error=f"no independent verifier registered for {kind}",
            reason="action checkpoint without verifier",
        )
    if claimed_destination_progress_or_complete(content):
        return ChatCloseDecision(
            status="UNVERIFIED",
            error="no structured destination action checkpoint",
            reason="claimed destination progress without evidence",
        )
    from .policy_setup_dispatch import (
        FAIL_CLOSED_MESSAGE,
        is_coverage_fill_miss,
        is_formentry_mint_miss,
        is_policy_setup_fail_closed,
    )

    if is_policy_setup_fail_closed(content) or is_policy_setup_fail_closed(
        last_error or ""
    ):
        return ChatCloseDecision(
            status="AWAITING_HUMAN_INPUT",
            error=FAIL_CLOSED_MESSAGE,
            reason="policy setup tool missing; fail-closed",
        )
    if is_formentry_mint_miss(content) or is_formentry_mint_miss(last_error or ""):
        return ChatCloseDecision(
            status="AWAITING_HUMAN_INPUT",
            error=(str(content or last_error or "")[:1_000]
                   or "Save & Continue Edit clicked; no FormEntry URL after 30s."),
            reason="formentry mint-miss; fail-closed HITL",
        )
    if is_coverage_fill_miss(content) or is_coverage_fill_miss(last_error or ""):
        return ChatCloseDecision(
            status="AWAITING_HUMAN_INPUT",
            error=(str(content or last_error or "")[:1_000]
                   or "no coverage labels were filled"),
            reason="coverage fill miss; fail-closed HITL",
        )
    infra = extract_infra_close_error(content, last_error)
    if infra:
        if _BLOCKER_TOKEN_RE.search(infra):
            return ChatCloseDecision(
                status="AWAITING_HUMAN_INPUT",
                error=infra,
                reason="structured blocker without destination claim",
            )
        return ChatCloseDecision(
            status="FAILED",
            error=infra,
            reason="infra fail without destination claim",
        )
    leftover = str(last_error or "").strip()
    if leftover and leftover != "no structured destination action checkpoint":
        return ChatCloseDecision(
            status="FAILED",
            error=leftover[:1_000],
            reason="persisted last_error without destination claim",
        )
    return ChatCloseDecision(
        status="UNVERIFIED",
        error="no structured destination action checkpoint",
        reason="generic close without destination evidence",
    )


def has_destination_action_checkpoint(store: Any, job_id: str) -> bool:
    action = store.get_checkpoint(job_id, "action")
    return bool(action)


def has_destination_verified_evidence(store: Any, job_id: str) -> bool:
    try:
        rows = store.list_evidence(job_id)
    except Exception:
        return False
    return any(item.get("verified") for item in rows or [])


def sanitize_worker_response(
    store: Any,
    job_id: str,
    content: str,
) -> dict[str, Any]:
    """Return the checkpoint payload. Success-shaped prose is rewritten."""
    from .hitl import hitl_text_is_slang_or_blame, sanitize_hitl_chat_text

    text = str(content or "")
    if hitl_text_is_slang_or_blame(text):
        return {
            "response_text": sanitize_hitl_chat_text(text, job_id=str(job_id or "")),
            "rewritten": True,
            "reason": (
                "HITL Chat tone must be dry/technical "
                "(PLAYWRIGHT_BLOCKED + path + ask)"
            ),
        }
    if has_destination_action_checkpoint(store, job_id) or has_destination_verified_evidence(
        store, job_id
    ):
        return {
            "response_text": text,
            "rewritten": False,
            "reason": "destination checkpoint or destination-verified evidence is present",
        }
    if claims_unverified_destination_progress(text):
        return {
            "response_text": UNVERIFIED_STUCK_TEXT,
            "rewritten": True,
            "reason": (
                "success-shaped worker prose without a destination-action "
                "checkpoint or destination-verified evidence"
            ),
        }
    return {
        "response_text": text,
        "rewritten": False,
        "reason": "prose does not claim an unverified fill or save",
    }
