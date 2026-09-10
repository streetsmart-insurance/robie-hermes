"""Default-deny authorization for ROBIE writes to a system of record.

The cardinal rules in `destructive_guard` answer "is this action destructive?".
This module answers a different question: "is ROBIE authorized to change
anything at all on this run?"

The default is NO. A run that has not been explicitly authorized may read,
retrieve, compare and report, but any attempt to post a note, confirm a change
request, apply a download, or otherwise mutate EZLynx raises
`WriteNotAuthorized`.

Authorization is deliberately awkward to grant: it needs both an explicit
opt-in and a named human, so it cannot be switched on by a stray default or a
copied command line.

Call sites:

    from robie_guard import assert_write_allowed
    assert_write_allowed("post_note", applicant_id=aid, policy_number=pol)

CLI:

    from robie_guard import add_write_gate_args, resolve_write_gate
    add_write_gate_args(parser)
    ...
    resolve_write_gate(parser.parse_args())

Environment (for scheduled runs):

    ROBIE_ALLOW_WRITES=1
    ROBIE_WRITE_AUTHORIZED_BY="carlo@streetsmart.insurance"
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Every mutation ROBIE is capable of. Anything not listed is still gated -
# an unknown intent is denied for being unknown, never allowed by omission.
KNOWN_WRITE_INTENTS = frozenset({
    "post_note",
    "add_note_to_discussion",
    "create_discussion",
    "confirm_change_request",
    "apply_download",
    "upload_document",
    "update_policy",
    "close_task",
    "send_email",
    "place_call",
})


class WriteNotAuthorized(RuntimeError):
    """Raised when a run that was never authorized to write tries to write."""


@dataclass
class WriteAuthorization:
    enabled: bool = False
    authorized_by: Optional[str] = None
    reason: Optional[str] = None
    intents: frozenset = field(default_factory=frozenset)  # empty == all intents

    def covers(self, intent: str) -> bool:
        if not self.enabled:
            return False
        return not self.intents or intent in self.intents


_AUTH = WriteAuthorization()


def enable_writes(authorized_by: str, reason: str, intents=None) -> WriteAuthorization:
    """Authorize writes for this process. Both arguments are required."""
    if not authorized_by or not str(authorized_by).strip():
        raise ValueError("enable_writes() requires a named authorizer")
    if not reason or not str(reason).strip():
        raise ValueError("enable_writes() requires a reason")
    global _AUTH
    _AUTH = WriteAuthorization(
        enabled=True,
        authorized_by=str(authorized_by).strip(),
        reason=str(reason).strip(),
        intents=frozenset(intents or ()),
    )
    scope = ", ".join(sorted(_AUTH.intents)) if _AUTH.intents else "ALL write intents"
    logger.warning("[WRITE_GATE] OPEN by %s (%s) - scope: %s",
                   _AUTH.authorized_by, _AUTH.reason, scope)
    return _AUTH


def disable_writes() -> None:
    global _AUTH
    _AUTH = WriteAuthorization()
    logger.info("[WRITE_GATE] closed - run is read-only")


def current_authorization() -> WriteAuthorization:
    return _AUTH


def writes_enabled(intent: str = "post_note") -> bool:
    return _AUTH.covers(intent)


def enable_writes_from_env() -> bool:
    """Authorize from environment, for scheduled runs. Needs both variables."""
    if os.environ.get("ROBIE_ALLOW_WRITES", "").strip() not in ("1", "true", "yes"):
        return False
    who = os.environ.get("ROBIE_WRITE_AUTHORIZED_BY", "").strip()
    if not who:
        logger.error("[WRITE_GATE] ROBIE_ALLOW_WRITES set without "
                     "ROBIE_WRITE_AUTHORIZED_BY - refusing to open the gate.")
        return False
    enable_writes(authorized_by=who, reason="ROBIE_ALLOW_WRITES environment opt-in")
    return True


def assert_write_allowed(intent: str, *, applicant_id=None, policy_number=None,
                         detail: Optional[str] = None) -> None:
    """Raise WriteNotAuthorized unless this run may perform `intent`."""
    where = " ".join(filter(None, [
        f"applicant={applicant_id}" if applicant_id else "",
        f"policy={policy_number}" if policy_number else "",
        detail or "",
    ])).strip()

    if intent not in KNOWN_WRITE_INTENTS:
        raise WriteNotAuthorized(
            f"Unknown write intent '{intent}' is denied for being unknown. "
            f"Add it to robie_guard.write_gate.KNOWN_WRITE_INTENTS if it is legitimate. "
            f"[{where}]")

    if not _AUTH.covers(intent):
        raise WriteNotAuthorized(
            f"'{intent}' blocked: this run is not authorized to write. "
            f"Pass --write-notes / --allow-writes with --authorized-by, or set "
            f"ROBIE_ALLOW_WRITES=1 and ROBIE_WRITE_AUTHORIZED_BY. [{where}]")

    logger.info("[WRITE_GATE] '%s' permitted (authorized by %s) %s",
                intent, _AUTH.authorized_by, where)


# --- argparse helpers -------------------------------------------------------

def add_write_gate_args(parser) -> None:
    parser.add_argument("--write-notes", "--allow-writes", dest="allow_writes",
                        action="store_true",
                        help="Authorize this run to write to EZLynx (post notes, confirm "
                             "changes). OFF by default. Requires --authorized-by.")
    parser.add_argument("--authorized-by", default=None,
                        help="Who authorized writes for this run (e.g. carlo@streetsmart.insurance).")
    parser.add_argument("--write-reason", default=None,
                        help="Why writes are authorized for this run.")


def resolve_write_gate(args) -> bool:
    """Apply argparse results. Returns True if the gate is open."""
    if getattr(args, "allow_writes", False):
        who = getattr(args, "authorized_by", None)
        if not who:
            raise WriteNotAuthorized(
                "--write-notes requires --authorized-by so the audit trail names a human.")
        enable_writes(authorized_by=who,
                      reason=getattr(args, "write_reason", None) or "CLI --write-notes")
        return True
    return enable_writes_from_env()
