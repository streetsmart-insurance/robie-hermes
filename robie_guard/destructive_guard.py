"""Default-deny guard for destructive actions in EZLynx.

The agent's stated intent is not evidence. The 2026-09 incident was an agent
with the CORRECT intent ("remove the duplicate renewal transaction") resolving
it onto the WRONG target (the policy record). So this guard never asks "what
did you mean" -- it asks "prove what this click or request will hit, and prove
the damage did not happen afterwards".
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .cardinal import (
    CardinalViolation,
    DELETABLE_OBJECT_CLASS,
    DESTRUCTIVE_TOKENS,
    FORBIDDEN_INTENTS,
    NEVER_ALLOWED_TOKENS,
    PROTECTED_OBJECT_CLASSES,
)

logger = logging.getLogger("robie.guard")

DEFAULT_CONFIG_PATH = Path(__file__).with_name("guard_config.json")

EZLYNX_HOST_PATTERN = re.compile(r"(^|\.)ezlynx\.com$", re.I)

DESTRUCTIVE_METHODS = frozenset({"DELETE"})

DESTRUCTIVE_URL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"/delete(policy|applicant|account|document|contact|note|task)?\b",
        r"/remove(policy|applicant|document|contact|driver|vehicle)?\b",
        r"/(purge|destroy|erase|trash)\b",
        r"[?&]action=(delete|remove|void)\b",
        r"\bvoidtransaction\b",
    )
)


@dataclass
class ActionIntent:
    kind: str
    intent: str | None = None
    object_class: str | None = None
    target_text: str | None = None
    target_attrs: dict[str, str] = field(default_factory=dict)
    dom_ancestry: list[str] = field(default_factory=list)
    url: str | None = None
    method: str | None = None
    applicant_id: str | None = None
    policy_number: str | None = None
    transaction_type: str | None = None
    transaction_status: str | None = None
    matched_row_count: int | None = None
    duplicate_count: int | None = None
    created_by: str | None = None
    scope_confirmed: bool = False
    unlock_token: str | None = None

    def haystack(self) -> str:
        parts: list[str] = [self.target_text or "", self.url or "", self.intent or ""]
        parts.extend(f"{k}={v}" for k, v in sorted(self.target_attrs.items()))
        return " ".join(parts).lower()


@dataclass
class Decision:
    allowed: bool
    rule_id: str
    reason: str
    intent: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def raise_if_denied(self) -> "Decision":
        if not self.allowed:
            raise CardinalViolation(self.rule_id, self.reason, self.intent)
        return self


def _contains_any(haystack: str, tokens: Iterable[str]) -> str | None:
    for tok in tokens:
        if tok in haystack:
            return tok
    return None


def looks_destructive(intent: ActionIntent) -> bool:
    hay = intent.haystack()
    if _contains_any(hay, DESTRUCTIVE_TOKENS):
        return True
    if (intent.method or "").upper() in DESTRUCTIVE_METHODS:
        return True
    if intent.url and any(p.search(intent.url) for p in DESTRUCTIVE_URL_PATTERNS):
        return True
    return False


def _load_allowlist(config_path: Path | None = None) -> list[dict[str, Any]]:
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("guard_config unreadable (%s) -- treating allowlist as EMPTY", exc)
        return []
    rules = data.get("allowed_destructive_requests") or []
    return [r for r in rules if r.get("approved_by") and r.get("approved_at")]


def _kill_switch_engaged() -> str | None:
    for var, label in (
        ("ROBIE_HALT", "ROBIE_HALT env var set"),
        ("ROBIE_READ_ONLY", "ROBIE_READ_ONLY env var set"),
    ):
        if os.environ.get(var, "").strip() not in ("", "0", "false", "False"):
            return label
    for candidate in ("DISABLED", ".robie-halt"):
        if Path(candidate).exists():
            return f"{candidate} file present in working directory"
    return None
