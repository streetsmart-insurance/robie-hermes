"""Same-day cancellation / non-pay workflow for hello@ intake (Step 3).

The 6-month analysis caught the failure mode: PHLY policy PHPK2734817-000
(Hearts For Home Healthcare, LLC) received pending-cancel notices on
June 1, July 1, July 6, August 31, and September 18, 2026 — five notices,
no visible action trail. The loop: every notice became a new task (or no
task), nobody owned the through-line, and reinstatement notices never
closed anything.

Contract:
- A cancellation/non-pay notice creates ONE client-call task,
  due same business day, that stays OPEN until the payment/cure is
  confirmed or the cancellation is confirmed.
- A later notice for the same policy attaches to the open task — it
  never creates a second one (the CancellationLedger + task dedupe).
- A reinstatement notice for the policy files its own note and proposes
  closing the open task (a human confirms the close).
- Non-renewal / DNOC notices get the same-day task but close on
  "replacement bound or confirmed cancelled".

Pure logic + an append-only JSONL ledger. No EZLynx writes here — the
filing plan (hello_filing) performs them through injected interfaces.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime
from typing import Any

from .hello_filing import (
    TASK_CURE_OR_CANCEL,
    build_filing_plan,
    filing_category_for,
)
from .hello_triage import notice_type_of

# ---------------------------------------------------------------------------
# Notice subtyping (cancellation vs reinstatement vs renewal vs other)
# ---------------------------------------------------------------------------


def notice_subtype(subject: str | None, body: str | None) -> str:
    """Classify the carrier notice: cancellation | reinstatement |
    non_renewal | renewal_notice | other.

    Non-pay is part of "cancellation" (pending-cancel for non-payment is
    the common hello@ shape). Reinstatement is its own subtype because
    it closes the loop instead of opening one. Both the subject and the
    body are scanned — carrier subjects are often vague
    ("Document from PHLY") while the body carries the notice type.
    """
    notice = notice_type_of(f"{subject or ''}\n{body or ''}")
    if "dnoc" in f"{subject or ''}\n{body or ''}".lower():
        # DNOC = direct notice of cancellation: same workflow as a
        # cancellation, even though the triage keyword list is narrower.
        return "cancellation"
    if notice == "non-renewal":
        return "non_renewal"
    if notice == "cancellation":
        return "cancellation"
    if notice == "reinstatement":
        return "reinstatement"
    if notice in ("renewal", "expiration"):
        return "renewal_notice"
    return "other"


def extract_amount_to_cure(subject: str | None,
                           body: str | None) -> str | None:
    """Pull the amount-to-cure out of a cancel notice, or None.

    Only returns a value when the text explicitly ties an amount to the
    cure/payment ("amount to cure", "pay $X", "balance due $X"). Never
    guesses from a bare premium figure.
    """
    import re
    text = f"{subject or ''}\n{body or ''}"
    patterns = [
        re.compile(r"\bamount (to cure|due)\b.{0,40}\$\s?([\d,]+\.\d{2})",
                   re.IGNORECASE),
        re.compile(r"\bpay\b.{0,30}\$\s?([\d,]+\.\d{2}).{0,30}\b(cure|"
                   r"avoid.{0,10}cancel|reinstate)", re.IGNORECASE),
        re.compile(r"\bbalance due\b.{0,30}\$\s?([\d,]+\.\d{2})",
                   re.IGNORECASE),
    ]
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            groups = [g for g in match.groups() if g and "$" not in g
                      and not g.lower().startswith(("cure", "avoid",
                                                    "reinstate"))]
            amount = groups[-1] if groups else match.group(0)
            return f"${amount}"
    return None


# ---------------------------------------------------------------------------
# Cancellation / reinstatement filing plans
# ---------------------------------------------------------------------------

def build_cancellation_plan(context: dict[str, Any]) -> dict[str, Any]:
    """Filing plan for a cancellation/non-pay notice.

    context: {"applicant_id", "summary", "policy_number",
      "cancel_effective_date", "amount_to_cure", "documents",
      "identity": {...}}.
    The task kind is cure_or_cancel: same-business-day client call, and
    it stays open until the ledger records cure or confirmed cancel.
    """
    subtype = notice_subtype(context.get("subject"), context.get("body"))
    category = filing_category_for("carrier_notice",
                                   {"notice_subtype": subtype})
    policy = context.get("policy_number") or "unknown policy"
    effective = context.get("cancel_effective_date")
    amount = context.get("amount_to_cure") or extract_amount_to_cure(
        context.get("subject"), context.get("body"))
    task_title = (f"Same-day client call — cancellation notice, policy "
                  f"{policy}")
    if effective:
        task_title += f" (effective {effective})"
    plan = build_filing_plan(category, {
        "applicant_id": context.get("applicant_id"),
        "summary": context.get("summary") or "",
        "policy_number": context.get("policy_number"),
        "documents": context.get("documents"),
        "task_title": task_title,
        "task": {
            "stays_open": True,
            "close_conditions": ["cure_confirmed", "cancel_confirmed"],
            "amount_to_cure": amount,
            "cancel_effective_date": effective,
            "ledger_kind": "cancellation",
        },
        "identity": context.get("identity"),
    })
    plan["notice_subtype"] = subtype
    plan["amount_to_cure"] = amount
    return plan


def build_reinstatement_plan(context: dict[str, Any]) -> dict[str, Any]:
    """Filing plan for a reinstatement notice.

    Files the notice, then proposes closing the policy's open
    cure-or-cancel task. The close is a proposal — a human confirms the
    payment actually cured before the ledger marks it closed.
    """
    plan = build_filing_plan("carrier_reinstatement", {
        "applicant_id": context.get("applicant_id"),
        "summary": context.get("summary") or "",
        "policy_number": context.get("policy_number"),
        "documents": context.get("documents"),
        "task": {
            "task_kind": "close_cure_or_cancel",
            "proposed_close": True,
            "ledger_kind": "cancellation",
        },
        "identity": context.get("identity"),
    })
    plan["notice_subtype"] = "reinstatement"
    return plan


# ---------------------------------------------------------------------------
# Cancellation ledger: the open-task through-line per policy
# ---------------------------------------------------------------------------

class CancellationLedger:
    """Append-only JSONL ledger of cancellation/cure work per policy.

    One open entry per (policy_number) at a time. A new notice for a
    policy with an open entry attaches to it instead of opening a
    second task — this is the PHLY-loop fix. Reinstatement marks the
    entry proposed_closed; a human confirmation marks it closed with
    the close condition recorded.

    Entries: {"ledger_id", "applicant_id", "policy_number",
      "kind": "cancellation"|"non_renewal", "status": "open"|
      "proposed_closed"|"closed", "close_condition": None|"cure_confirmed"|
      "cancel_confirmed"|"replaced", "opened_at", "updated_at",
      "task_id", "notes": [...]}.
    """

    def __init__(self, path: str):
        self.path = path

    def _read_all(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        entries: list[dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

    def _rewrite(self, entries: list[dict[str, Any]]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")
        os.replace(tmp, self.path)

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def find_open(self, policy_number: str | None) -> dict[str, Any] | None:
        """Return the open ledger entry for a policy, or None."""
        policy = (policy_number or "").strip()
        if not policy:
            return None
        for entry in self._read_all():
            if (entry.get("policy_number") == policy
                    and entry.get("status") == "open"):
                return entry
        return None

    def open_entry(self, applicant_id: str | None,
                   policy_number: str,
                   kind: str = "cancellation",
                   task_id: str | None = None) -> dict[str, Any]:
        """Open a ledger entry — unless one is already open (dedupe).

        Returns (entry, was_created): was_created=False means the caller
        should attach to the existing entry, never open a second task.
        """
        existing = self.find_open(policy_number)
        if existing is not None:
            return existing, False
        entry = {
            "ledger_id": f"cx-{uuid.uuid4().hex[:12]}",
            "applicant_id": applicant_id,
            "policy_number": policy_number,
            "kind": kind,
            "status": "open",
            "close_condition": None,
            "opened_at": self._now(),
            "updated_at": self._now(),
            "task_id": task_id,
            "notes": [],
        }
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        return entry, True

    def attach_notice(self, policy_number: str,
                      notice_summary: str) -> dict[str, Any] | None:
        """Record another notice against the open entry (no new task)."""
        entry = self.find_open(policy_number)
        if entry is None:
            return None
        entries = self._read_all()
        for row in entries:
            if row.get("ledger_id") == entry["ledger_id"]:
                row.setdefault("notes", []).append(
                    f"{date.today().isoformat()}: {notice_summary}")
                row["updated_at"] = self._now()
        self._rewrite(entries)
        entry = self.find_open(policy_number)
        return entry

    def propose_close(self, policy_number: str,
                      reason: str) -> dict[str, Any] | None:
        """A reinstatement arrived: propose closing the open entry."""
        entries = self._read_all()
        for row in entries:
            if (row.get("policy_number") == (policy_number or "").strip()
                    and row.get("status") == "open"):
                row["status"] = "proposed_closed"
                row.setdefault("notes", []).append(
                    f"{date.today().isoformat()}: proposed close — {reason}")
                row["updated_at"] = self._now()
                self._rewrite(entries)
                return row
        return None

    def confirm_close(self, policy_number: str,
                      close_condition: str) -> dict[str, Any] | None:
        """Human-confirmed close. close_condition: cure_confirmed |
        cancel_confirmed | replaced. Only these values are accepted."""
        if close_condition not in ("cure_confirmed", "cancel_confirmed",
                                   "replaced"):
            raise ValueError(
                f"invalid close_condition {close_condition!r}: must be "
                "cure_confirmed, cancel_confirmed, or replaced")
        entries = self._read_all()
        for row in entries:
            if (row.get("policy_number") == (policy_number or "").strip()
                    and row.get("status") in ("open", "proposed_closed")):
                row["status"] = "closed"
                row["close_condition"] = close_condition
                row["updated_at"] = self._now()
                self._rewrite(entries)
                return row
        return None

    def is_done(self, policy_number: str) -> bool:
        """True only when no open/proposed entry remains for the policy."""
        policy = (policy_number or "").strip()
        return not any(
            e.get("policy_number") == policy
            and e.get("status") in ("open", "proposed_closed")
            for e in self._read_all())


# ---------------------------------------------------------------------------
# One-call workflow: notice -> plan + ledger entry
# ---------------------------------------------------------------------------

def process_cancellation_notice(context: dict[str, Any],
                                ledger: CancellationLedger
                                ) -> dict[str, Any]:
    """Process one carrier notice through the cancellation workflow.

    Returns {"subtype", "plan", "ledger_entry", "ledger_created",
    "action"} where action is one of:
      "new_task"       — first notice; open entry + same-day task plan
      "attach_to_open" — notice for a policy with an open entry; the
                         plan files the note only (no duplicate task)
      "propose_close"  — reinstatement; plan files + proposes close
      "file_only"      — other notice subtypes; plain filing plan.
    """
    subtype = notice_subtype(context.get("subject"), context.get("body"))
    policy = (context.get("policy_number") or "").strip()
    if subtype == "reinstatement":
        plan = build_reinstatement_plan(context)
        entry = ledger.propose_close(
            policy, "reinstatement notice received") if policy else None
        return {"subtype": subtype, "plan": plan, "ledger_entry": entry,
                "ledger_created": False, "action": "propose_close"}
    if subtype in ("cancellation", "non_renewal"):
        existing = ledger.find_open(policy) if policy else None
        if existing is not None:
            # The PHLY-loop fix: no second task. File the notice, attach
            # to the open entry.
            plan = build_filing_plan(
                filing_category_for("carrier_notice",
                                    {"notice_subtype": subtype}), {
                    "applicant_id": context.get("applicant_id"),
                    "summary": context.get("summary") or "",
                    "policy_number": context.get("policy_number"),
                    "documents": context.get("documents"),
                    "identity": context.get("identity"),
                    # The open cure-or-cancel task already exists: file
                    # the notice, create nothing.
                    "suppress_task": True,
                })
            ledger.attach_notice(policy, context.get("summary", "")[:120])
            return {"subtype": subtype, "plan": plan,
                    "ledger_entry": ledger.find_open(policy),
                    "ledger_created": False, "action": "attach_to_open"}
        plan = build_cancellation_plan(context)
        entry, created = (ledger.open_entry(
            context.get("applicant_id"), policy,
            kind="non_renewal" if subtype == "non_renewal" else
            "cancellation") if policy else (None, False))
        return {"subtype": subtype, "plan": plan, "ledger_entry": entry,
                "ledger_created": created, "action": "new_task"}
    # Other subtypes: plain filing, no ledger.
    plan = build_filing_plan(
        filing_category_for("carrier_notice", {"notice_subtype": subtype}),
        {
            "applicant_id": context.get("applicant_id"),
            "summary": context.get("summary") or "",
            "policy_number": context.get("policy_number"),
            "documents": context.get("documents"),
            "identity": context.get("identity"),
        })
    return {"subtype": subtype, "plan": plan, "ledger_entry": None,
            "ledger_created": False, "action": "file_only"}


# Re-export for convenience.
__all__ = [
    "TASK_CURE_OR_CANCEL",
    "CancellationLedger",
    "build_cancellation_plan",
    "build_reinstatement_plan",
    "extract_amount_to_cure",
    "notice_subtype",
    "process_cancellation_notice",
]
