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
