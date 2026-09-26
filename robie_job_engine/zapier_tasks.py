"""Fire EZLynx follow-up tasks through the agency's Zapier catch-hook Zap.

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
    """POST the payload to the Zapier catch hook (or dry-run validate it).

    Returns the script's parsed result dict: {"ok": bool, ...}.  Raises
    ValueError on a bad payload and RuntimeError when the script itself
    cannot run.
    """
    if isinstance(payload, dict) and payload.get("assignee"):
        payload["assignee"] = normalize_assignee(payload["assignee"])
    validate_task_payload(payload)
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
