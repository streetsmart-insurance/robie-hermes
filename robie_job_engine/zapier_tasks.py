"""Fire EZLynx follow-up tasks via direct Discussion API (TaskCreationNote).

Primary path is the direct EZLynx Discussion API (no Zapier needed).
Falls back to the Zapier catch-hook if the direct API is unavailable.

The hook URL lives in the Secure Vault as ``custom.zapier-webhook``; this
module never sees it.  Firing goes through the zapier skill's
``bin/zap-trigger`` script, which swaps the real URL in on approved egress.
Use ``dry_run=True`` to validate a payload without firing.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# Legacy host-install location. Still searched, but no longer the only place.
ZAP_TRIGGER_SCRIPT = os.path.expanduser("~/workspace/skills/zapier/bin/zap-trigger")

# Explicit override. When set, it is the ONLY path tried (a typo fails loudly
# instead of silently falling through to a different copy).
ZAP_TRIGGER_ENV = "ROBIE_ZAP_TRIGGER"

_SKILL_RELATIVE = Path("skills") / "zapier" / "bin" / "zap-trigger"

# In-tree skill shipped with the release: <release_root>/skills/zapier/bin/zap-trigger
RELEASE_ZAP_TRIGGER = Path(__file__).resolve().parents[1] / _SKILL_RELATIVE

HERMES_ROOTS = ("/opt/streetsmart-hermes-test", "/opt/streetsmart-hermes")

DOCUMENT_RETRIEVAL_SOURCE = "document-retrieval"
DOCUMENT_RETRIEVAL_ASSIGNEE = "SSNicole"

# due_date is required by standing agency rule: every EZLynx follow-up task
# must carry an ISO YYYY-MM-DD due date, and fire_task rejects payloads
# without a valid one.
REQUIRED_PAYLOAD_KEYS = ("applicant_id", "task_title", "assignee", "source", "due_date")


def validate_due_date(value: Any) -> str:
    """Validate an ISO YYYY-MM-DD due date; return the normalized date string."""
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValueError(
            f"Zapier task due_date must be ISO YYYY-MM-DD, got {value!r}"
        ) from None


def zap_trigger_candidates() -> list[str]:
    """Ordered zap-trigger locations to try. Paths only; never a webhook URL."""
    override = (os.environ.get(ZAP_TRIGGER_ENV) or "").strip()
    if override:
        return [os.path.expanduser(override)]
    candidates = [str(RELEASE_ZAP_TRIGGER), ZAP_TRIGGER_SCRIPT]
    for root in HERMES_ROOTS:
        candidates.append(f"{root}/.hermes/{_SKILL_RELATIVE}")
        candidates.append(f"{root}/workspace/{_SKILL_RELATIVE}")
    seen: set[str] = set()
    ordered: list[str] = []
    for path in candidates:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def resolve_zap_trigger(candidates: Iterable[str] | None = None) -> str:
    """Return the first existing zap-trigger script, or raise listing every path tried."""
    tried = list(candidates) if candidates is not None else zap_trigger_candidates()
    for path in tried:
        if os.path.isfile(path):
            return path
    raise RuntimeError("zap-trigger script not found; tried: " + ", ".join(tried))


def normalize_assignee(value: Any) -> str:
    """Map a display name to its EZLynx login using confirmation_notify's map.

    ``"Nicole Segovia"`` / ``"nicole"`` -> ``"SSNicole"``. A value that is
    already a login, or is unknown, is returned unchanged (whitespace
    collapsed). An unknown display name is left as-is so zap-trigger refuses
    it rather than a task being misassigned.
    """
    text = " ".join(str(value or "").split())
    if not text:
        return text
    try:
        from .confirmation_notify import requester_login
    except ImportError:  # pragma: no cover - fail closed at zap-trigger
        return text
    return requester_login(text) or text


def document_retrieval_task_payload(
    *,
    applicant_id: str,
    carrier: str,
    doc_type: str,
    insured: str,
    policy_number: str,
    due_date: str,
    assignee: str = DOCUMENT_RETRIEVAL_ASSIGNEE,
) -> dict[str, Any]:
    """Build the Document Retrieval missing-WF review task payload (validated)."""
    parts = {
        "carrier": carrier,
        "doc_type": doc_type,
        "insured": insured,
        "policy_number": policy_number,
    }
    blank = [name for name, val in parts.items() if not str(val or "").strip()]
    if blank:
        raise ValueError("Document Retrieval task missing: " + ", ".join(blank))
    clean = {name: " ".join(str(val).split()) for name, val in parts.items()}
    payload: dict[str, Any] = {
        "applicant_id": str(applicant_id or "").strip(),
        "assignee": normalize_assignee(assignee),
        "source": DOCUMENT_RETRIEVAL_SOURCE,
        "due_date": due_date,
        "task_title": (
            f"Document Retrieval review — {clean['carrier']} {clean['doc_type']}"
            f" — {clean['insured']} — {clean['policy_number']}"
        ),
    }
    validate_task_payload(payload)
    return payload


def validate_task_payload(payload: dict[str, Any]) -> None:
    """Raise ValueError when the Zap payload is missing required keys."""
    if not isinstance(payload, dict):
        raise ValueError("Zapier task payload must be a JSON object")
    missing = [key for key in REQUIRED_PAYLOAD_KEYS if not payload.get(key)]
    if missing:
        raise ValueError(
            "Zapier task payload missing required keys: " + ", ".join(missing)
        )
    # Normalize in place so the fired payload always carries a canonical date.
    payload["due_date"] = validate_due_date(payload.get("due_date"))


def fire_task(payload: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    """Create an EZLynx task via direct Discussion API, falling back to Zapier.

    Primary path: Direct TaskCreationNote via Discussion API (no Zapier).
    Fallback: Zapier catch hook if direct API fails.
    
    Returns dict with {"ok": bool, "method": "direct"|"zapier", ...}.
    Raises ValueError on a bad payload.
    """
    if isinstance(payload, dict) and payload.get("assignee"):
        payload["assignee"] = normalize_assignee(payload["assignee"])
    validate_task_payload(payload)
    
    if dry_run:
        # Validate the Zapier fallback is configured even in dry-run,
        # so misconfiguration fails fast (test_missing_script_raises).
        # Direct API is primary, but a broken fallback should not be silent.
        resolve_zap_trigger()
        return {"ok": True, "dry_run": True, "method": "direct"}
    
    # Try direct API first
    try:
        return _fire_via_direct_api(payload)
    except Exception as e:
        # Fall back to Zapier
        import logging
        logging.getLogger(__name__).warning(
            "Direct task API failed (%s), falling back to Zapier", e
        )
        return _fire_via_zapier(payload, dry_run=dry_run)


def _fire_via_direct_api(payload: dict[str, Any]) -> dict[str, Any]:
    """Create task via direct EZLynx Discussion API TaskCreationNote."""
    from .ezlynx_task_api import create_task
    
    # Map payload fields to direct API
    # Payload has: applicant_id, task_title, assignee, source, due_date
    # Direct API wants: applicant_id, title, assigned_user_name, due_date, etc.
    result = create_task(
        applicant_id=payload["applicant_id"],
        title=payload["task_title"],
        description=payload.get("task_description", ""),
        assigned_user_name=payload.get("assignee"),
        due_date=payload.get("due_date"),
        due_time=payload.get("due_time"),  # Optional HH:MM
        priority=payload.get("priority", "Medium"),
    )
    return {
        "ok": True,
        "method": "direct",
        "task": result,
    }


def _fire_via_zapier(payload: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    script = resolve_zap_trigger()
    command = ["python3", script, "--payload", json.dumps(payload)]
    if dry_run:
        command.append("--dry-run")
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=90
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"zap-trigger failed to run: {exc}") from exc
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"zap-trigger returned non-JSON output: {completed.stdout[:200]}"
        ) from exc
    if completed.returncode not in (0, 1, 2):
        raise RuntimeError(f"zap-trigger exited {completed.returncode}: {result}")
    return result
