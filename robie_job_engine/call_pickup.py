"""Decide which Task Check-In labels place a call.

"Robie Call" uses the free-form handler. "Robie Lead Follow Up" (and the
alias "Robie Call Follow Up") uses the lead script. The nine Splice
workflows each have their own "Robie <workflow name>" label. Those nine
are on by default on the Test server and stay off on Production until
ROBIE_SPLICE_WORKFLOWS_LIVE=1. Each note is claimed at most once per
America/New_York day.
"""
from __future__ import annotations

import hashlib
import os
import re
import socket
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Exact keys after case-folding and stripping hyphens, spaces, and
# underscores. "Robie Call" must not match as a prefix of
# "Robie Call Follow Up".
_LEAD_KEYS = frozenset({"robieleadfollowup", "robiecallfollowup"})
_CALL_KEY = "robiecall"
LEAD_WORKFLOW_ID = "lead_follow_up"
TEST_HOST = "hermes-test-01"
# Single Production switch for all nine. Unset stays off. Test does not
# read this flag.
SPLICE_PROD_FLAG = "ROBIE_SPLICE_WORKFLOWS_LIVE"
# Test proofs dial only this applicant. It must be Jake Ferrara's own
# EZLynx client account. Buster Brown and ROBIE Test LLC are refused.
SPLICE_TEST_APPLICANT_ENV = "ROBIE_SPLICE_TEST_APPLICANT_ID"
REFUSED_SPLICE_TEST_APPLICANTS = {
    "26356199": "Buster Brown",
    "220250093": "ROBIE Test LLC",
}

# Display names Carlo creates in EZLynx. Matching ignores case, spaces,
# hyphens, and underscores. The full normalized label must match, so
# "Robie audit" is not "Robie Audit Not Complete".
SPLICE_LABELS: tuple[tuple[str, str], ...] = (
    ("Robie Audit Not Complete", "audit_not_complete"),
    ("Robie Recommendations Follow-Up", "recommendations_follow_up"),
    ("Robie Returned Mail", "returned_mail"),
    ("Robie E-signature Follow-Up", "esignature_follow_up"),
    ("Robie Additional Information Follow-Up", "additional_information_follow_up"),
    ("Robie Sales Center Reviewed Status", "sales_center_reviewed"),
    ("Robie Winback Campaign", "winback_campaign"),
    ("Robie Renewal Reach Out", "renewal_reach_out"),
    ("Robie Unresponsive", "unresponsive"),
)
SPLICE_WORKFLOW_IDS = frozenset(workflow_id for _label, workflow_id in SPLICE_LABELS)


@dataclass(frozen=True)
class CallPickup:
    action: str
    workflow_id: str = ""
    reason: str = ""


def _label_parts(activity_labels: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"[,;|]", activity_labels or "")
        if part.strip()
    ]


def label_key(value: str) -> str:
    """Case-fold and drop hyphens, spaces, and underscores."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


_SPLICE_BY_KEY = {
    label_key(label): workflow_id for label, workflow_id in SPLICE_LABELS
}


def _source(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _short_host(hostname: str | None) -> str:
    if hostname is None:
        hostname = socket.gethostname()
    return str(hostname or "").split(".")[0]


def is_test_server(
    env: Mapping[str, str] | None = None, hostname: str | None = None,
) -> bool:
    """True on hermes-test-01 or when ROBIE_ENV is TEST."""
    source = _source(env)
    if str(source.get("ROBIE_ENV") or "").strip().upper() == "TEST":
        return True
    return _short_host(hostname) == TEST_HOST


def splice_workflows_enabled(
    env: Mapping[str, str] | None = None, hostname: str | None = None,
) -> bool:
    """The nine Splice labels dial on Test by default.

    Production stays off until ROBIE_SPLICE_WORKFLOWS_LIVE=1. Any other
    value, including unset, stays off. The two live labels do not read
    this flag.
    """
    source = _source(env)
    if is_test_server(source, hostname):
        return True
    return str(source.get(SPLICE_PROD_FLAG) or "").strip() == "1"


def splice_test_account_reason(
    applicant_id: str,
    *,
    env: Mapping[str, str] | None = None,
    hostname: str | None = None,
) -> str | None:
    """Why a Test splice call must not dial, or None when it may.

    Test proofs run on Jake Ferrara's own EZLynx client account, named by
    ROBIE_SPLICE_TEST_APPLICANT_ID. Buster Brown (26356199) and ROBIE Test
    LLC (220250093) are refused. Production does not use this gate.
    """
    if not is_test_server(env, hostname):
        return None
    source = _source(env)
    configured = str(source.get(SPLICE_TEST_APPLICANT_ENV) or "").strip()
    if not configured:
        return (
            "Jake Ferrara's EZLynx applicant id is not configured "
            f"({SPLICE_TEST_APPLICANT_ENV})"
        )
    refused = REFUSED_SPLICE_TEST_APPLICANTS.get(configured)
    if refused:
        return (
            f"configured test applicant is {refused}, not Jake Ferrara's "
            "own client account"
        )
    if str(applicant_id or "").strip() != configured:
        return "task is not on Jake Ferrara's configured test account"
    return None


def splice_task_predates_enablement(created_raw: str, enabled_at: str) -> bool:
    """True when this task existed before the nine were enabled.

    An unreadable Created Date fails closed (treated as already existing)
    once an enablement timestamp is set. No timestamp means the check
    does not apply.
    """
    if not str(enabled_at or "").strip():
        return False
    from .report_clock import report_created_et

    created = report_created_et(created_raw)
    enabled = report_created_et(enabled_at)
    if created is None or enabled is None:
        return True
    return created < enabled


def classify_call_request(
    activity_labels: str,
    note_text: str = "",
    *,
    env: Mapping[str, str] | None = None,
    hostname: str | None = None,
) -> CallPickup:
    """Resolve a check-in row to a script, a free-form call, or neither.

    The live label is "Robie lead follow-up". "Robie Lead Follow Up" and
    "Robie Call Follow Up" are the same lead script. A label matches only
    when the full normalized text is exactly that label, so "Robie Call"
    does not swallow "Robie Call Follow Up" or a Splice label.

    The nine Splice labels select their scripts only when
    ``splice_workflows_enabled`` is true. Otherwise they stay unlabeled,
    the same as a short name such as "Robie audit".

    ``note_text`` is ignored. A title or description that says "call",
    "Do not call", or "[CALLBACK REQUIRED]" does not dial.
    """
    del note_text
    keys = [label_key(part) for part in _label_parts(activity_labels)]
    if any(key in _LEAD_KEYS for key in keys):
        return CallPickup("workflow", workflow_id=LEAD_WORKFLOW_ID)
    if any(key == _CALL_KEY for key in keys):
        return CallPickup("freeform")
    if splice_workflows_enabled(env, hostname):
        for key in keys:
            workflow_id = _SPLICE_BY_KEY.get(key)
            if workflow_id:
                return CallPickup("workflow", workflow_id=workflow_id)
    return CallPickup("not_labeled")


def note_dedupe_key(
    applicant_id: str, discussion_id: str, note_text: str, task_id: str,
) -> str:
    basis = (note_text or "").strip().casefold() or (task_id or "").strip()
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return f"{applicant_id}:{discussion_id}:{digest}"


def calling_day(moment: datetime) -> str:
    """Eastern calendar day.

    ``moment`` is the call clock or a Created Date that has already been
    converted from America/Chicago. A naive value is Eastern, not Central.
    Report strings go through report_created_et before they reach here.
    """
    zone = ZoneInfo("America/New_York")
    if moment.tzinfo is None:
        current = moment.replace(tzinfo=zone)
    else:
        current = moment.astimezone(zone)
    return current.date().isoformat()


class CallDedupeStore:
    """One claim per note per America/New_York day."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS call_note_dedupe (
                    note_key TEXT NOT NULL,
                    call_day TEXT NOT NULL,
                    PRIMARY KEY (note_key, call_day)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def already_called(self, note_key: str, day: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM call_note_dedupe WHERE note_key = ? AND call_day = ?",
                (note_key, day),
            ).fetchone()
        return row is not None

    def record(self, note_key: str, day: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO call_note_dedupe (note_key, call_day)
                VALUES (?, ?)
                ON CONFLICT(note_key, call_day) DO NOTHING
                """,
                (note_key, day),
            )
