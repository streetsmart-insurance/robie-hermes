"""Default-deny guard for destructive actions in EZLynx.

Design premise: the agent's stated intent is not evidence. The 2026-09 incident
was an agent with the CORRECT intent ("remove the duplicate renewal transaction")
resolving that intent onto the WRONG target (the policy record). So this guard
never asks "what did you mean" -- it asks "prove what this click or request will
hit, and prove the damage did not happen afterwards".

Three enforcement points, all default-deny:
  1. classify_intent()  -- Python call sites, before any action
  2. SafePage.safe_click / route interception -- the browser layer (safe_page.py)
  3. browser_shim.js -- inside the page, for free-form agents driving via CDP

Nothing here is a warning. Everything raises CardinalViolation.
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

# Where observed-and-approved destructive request signatures live. Ships EMPTY.
DEFAULT_CONFIG_PATH = Path(__file__).with_name("guard_config.json")

# EZLynx hosts we police. Anything off-host is out of scope for the HTTP guard
# (a carrier portal delete is still caught by intent/click classification).
EZLYNX_HOST_PATTERN = re.compile(r"(^|\.)ezlynx\.com$", re.I)

DESTRUCTIVE_METHODS = frozenset({"DELETE"})

# URL path fragments that mean "this request destroys something".
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


# ---------------------------------------------------------------------------


@dataclass
class ActionIntent:
    """A single action an agent is about to take, described in evidence terms.

    Every field except `kind` is optional so that an under-described action is
    DENIED for being under-described, never allowed by omission.
    """

    kind: str                              # "click" | "http" | "api" | "cli"
    intent: str | None = None              # declared purpose, e.g. delete_renewal_transaction
    object_class: str | None = None        # policy | document | policy_transaction | ...
    target_text: str | None = None         # accessible text of the control
    target_attrs: dict[str, str] = field(default_factory=dict)
    dom_ancestry: list[str] = field(default_factory=list)  # outermost -> innermost
    url: str | None = None
    method: str | None = None
    applicant_id: str | None = None
    policy_number: str | None = None
    transaction_type: str | None = None    # RWL | NEW | END | CAN | ...
    transaction_status: str | None = None  # Pending | Active | ...
    matched_row_count: int | None = None   # rows the selector resolved to
    duplicate_count: int | None = None     # how many pending shells exist
    created_by: str | None = None          # SSRobie | human username
    scope_confirmed: bool = False          # caller proved target is the tx row
    unlock_token: str | None = None        # issued by guarded_transaction_delete

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


# ---------------------------------------------------------------------------


def _contains_any(haystack: str, tokens: Iterable[str]) -> str | None:
    for tok in tokens:
        if tok in haystack:
            return tok
    return None


def looks_destructive(intent: ActionIntent) -> bool:
    """True if this action could destroy something. Deliberately over-inclusive."""
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
        # A broken allowlist must not become a permissive allowlist.
        logger.error("guard_config unreadable (%s) -- treating allowlist as EMPTY", exc)
        return []
    rules = data.get("allowed_destructive_requests") or []
    return [r for r in rules if r.get("approved_by") and r.get("approved_at")]


def _kill_switch_engaged() -> str | None:
    """Re-read every call. Never cached. CR-7."""
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


# ---------------------------------------------------------------------------


def classify_intent(intent: ActionIntent, config_path: Path | None = None) -> Decision:
    """The single decision function. Default-deny.

    Returns a Decision; call .raise_if_denied() at the call site.
    """
    payload = asdict(intent)

    halt = _kill_switch_engaged()
    if halt and looks_destructive(intent):
        return Decision(False, "CR-7", f"Kill switch engaged: {halt}", payload)

    # --- CR-2: categorically forbidden intents, destructive or not -----------
    if intent.intent in FORBIDDEN_INTENTS:
        return Decision(False, "CR-2", f"Intent '{intent.intent}' is categorically forbidden", payload)

    if not looks_destructive(intent):
        return Decision(True, "CR-1", "Non-destructive action", payload)

    hay = intent.haystack()

    # --- CR-1a: phrases that can never be right ------------------------------
    hit = _contains_any(hay, NEVER_ALLOWED_TOKENS)
    if hit:
        return Decision(
            False, "CR-1",
            f"Control/endpoint matches never-allowed phrase '{hit}'. "
            f"ROBIE does not delete policies, applicants, documents or contacts.",
            payload,
        )

    # --- CR-1b: protected object classes --------------------------------------
    if intent.object_class in PROTECTED_OBJECT_CLASSES:
        return Decision(
            False, "CR-1",
            f"Destructive action on protected object class '{intent.object_class}'. "
            f"Raise an assigned EZLynx task for a human instead.",
            payload,
        )

    # --- CR-1c: the ONE narrow exception --------------------------------------
    if intent.object_class == DELETABLE_OBJECT_CLASS:
        return _classify_transaction_delete(intent, payload, config_path)

    # --- CR-1d: destructive but the caller did not say what it is -------------
    return Decision(
        False, "CR-1",
        f"Destructive action with object_class={intent.object_class!r}. "
        f"An under-described destructive action is denied, not assumed safe.",
        payload,
    )


def _classify_transaction_delete(
    intent: ActionIntent, payload: dict[str, Any], config_path: Path | None
) -> Decision:
    """Deleting a duplicate pending renewal shell -- the only permitted deletion.

    Every condition below is a thing that was NOT true during the 2026-09
    incident. They exist so that scope is proven, not assumed.
    """
    if intent.intent != "delete_renewal_transaction":
        return Decision(False, "CR-1",
                        "Transaction deletion requires intent='delete_renewal_transaction'", payload)

    if not intent.unlock_token:
        return Decision(False, "CR-1",
                        "Transaction deletion must run inside guarded_transaction_delete()", payload)

    if not intent.scope_confirmed:
        return Decision(False, "CR-1",
                        "scope_confirmed is False -- caller has not proven the target is the "
                        "transaction row rather than the policy record. THIS is the 2026-09 failure.",
                        payload)

    if intent.matched_row_count != 1:
        return Decision(False, "CR-1",
                        f"Selector resolved to {intent.matched_row_count} rows; exactly 1 required. "
                        f"An ambiguous selector is how a policy-level control gets clicked.",
                        payload)

    if (intent.transaction_type or "").upper() != "RWL":
        return Decision(False, "CR-1",
                        f"transaction_type={intent.transaction_type!r}; only RWL may be deleted", payload)

    if (intent.transaction_status or "").lower() != "pending":
        return Decision(False, "CR-1",
                        f"transaction_status={intent.transaction_status!r}; only a Pending shell "
                        f"may be deleted. An Active/bound term is never touched.",
                        payload)

    if not intent.policy_number or not intent.applicant_id:
        return Decision(False, "CR-1",
                        "policy_number and applicant_id are both required to scope the deletion", payload)

    if (intent.duplicate_count or 0) < 2:
        return Decision(False, "CR-1",
                        f"duplicate_count={intent.duplicate_count}; there is no duplicate to remove. "
                        f"Deleting the only pending shell is not a cleanup, it is data loss.",
                        payload)

    if (intent.created_by or "").lower() not in ("ssrobie", "robie"):
        return Decision(False, "CR-1",
                        f"created_by={intent.created_by!r}; ROBIE only removes shells it created "
                        f"itself. A human's shell is a human's to remove.",
                        payload)

    # HTTP-level actions additionally need an observed, human-approved signature.
    if intent.kind in ("http", "api"):
        allowlist = _load_allowlist(config_path)
        if not allowlist:
            return Decision(
                False, "CR-1",
                "No approved destructive request signature on file. The real EZLynx "
                "delete-transaction endpoint has never been observed, and it will not be "
                "guessed. Run scripts/observe_destructive_request.py once while a human "
                "performs the deletion, then have Carlo approve the captured signature "
                "into guard_config.json.",
                payload,
            )
        for rule in allowlist:
            if _signature_matches(rule, intent):
                return Decision(True, "CR-1-EXCEPTION",
                                f"Permitted duplicate pending RWL deletion "
                                f"(signature '{rule.get('id')}' approved by {rule.get('approved_by')})",
                                payload)
        return Decision(False, "CR-1",
                        "Request does not match any approved destructive signature", payload)

    return Decision(True, "CR-1-EXCEPTION",
                    "Permitted duplicate pending RWL transaction deletion "
                    "(scope confirmed, single row, ROBIE-created)", payload)


def _signature_matches(rule: dict[str, Any], intent: ActionIntent) -> bool:
    method = (rule.get("method") or "").upper()
    if method and method != (intent.method or "").upper():
        return False
    pattern = rule.get("url_pattern")
    if not pattern:
        return False
    try:
        if not re.search(pattern, intent.url or ""):
            return False
    except re.error:
        return False
    return True


# ---------------------------------------------------------------------------


class guarded_transaction_delete:
    """Context manager that issues a short-lived unlock token.

    Outside this block, no deletion is possible at all. Inside it, the
    conditions in _classify_transaction_delete still apply -- the token only
    proves the deletion was deliberate, never that it is safe.

    On exit it REQUIRES the caller to have recorded a post-condition proving
    the policy record survived. No proof -> CATASTROPHE.
    """

    def __init__(self, *, applicant_id: str, policy_number: str, verifier):
        self.applicant_id = applicant_id
        self.policy_number = policy_number
        self.verifier = verifier          # callable() -> dict with policy state
        self.token: str | None = None
        self.before: dict[str, Any] | None = None
        self.after: dict[str, Any] | None = None

    def __enter__(self) -> "guarded_transaction_delete":
        self.before = self.verifier()
        if not self.before.get("policy_exists"):
            raise CardinalViolation("CR-1", "Policy not found before deletion -- refusing to act",
                                    {"policy_number": self.policy_number})
        self.token = f"tx-del-{self.applicant_id}-{datetime.now(timezone.utc).timestamp()}"
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.token = None
        if exc_type is not None:
            return False
        self.after = self.verifier()
        if not self.after.get("policy_exists"):
            raise CardinalViolation(
                "CR-1", "CATASTROPHE: policy record no longer exists after transaction deletion. "
                        "Halt every job and escalate to Carlo immediately.",
                {"policy_number": self.policy_number,
                 "before": self.before, "after": self.after},
            )
        before_n = (self.before or {}).get("transaction_count")
        after_n = self.after.get("transaction_count")
        if isinstance(before_n, int) and isinstance(after_n, int) and after_n != before_n - 1:
            raise CardinalViolation(
                "CR-1", f"Transaction count went {before_n} -> {after_n}; expected exactly one "
                        f"row removed. Treat as CATASTROPHE until a human confirms.",
                {"policy_number": self.policy_number},
            )
        return False


def assert_allowed(intent: ActionIntent, config_path: Path | None = None) -> Decision:
    """One-liner for call sites: raises CardinalViolation unless allowed."""
    return classify_intent(intent, config_path).raise_if_denied()
