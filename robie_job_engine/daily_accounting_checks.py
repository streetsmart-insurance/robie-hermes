"""Read-only accounting evidence and grading core.

No integration writes, inferred bank deposits, or implicit coverage claims. Source
adapters provide item evidence and an explicit completeness assertion. A missing
adapter always produces incomplete coverage, not a zero-event assertion.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence

REQUIRED_SOURCES = ("Ascend", "Applied Pay", "EZLynx Accounting Team")


@dataclass(frozen=True)
class Evidence:
    source: str
    source_id: str
    observed_at: str
    event_date: str
    amount: str
    currency: str
    status: str
    account: str
    field: str
    url: str = ""
    due_date: str = ""
    detail: str = ""
    match_ref: str = ""

    def __post_init__(self):
        if not self.source or not self.source_id or not self.observed_at:
            raise ValueError("source, source ID and observation time required")


@dataclass(frozen=True)
class SourceSnapshot:
    source: str
    items: Sequence[Evidence]
    complete: bool
    error: str = ""
    as_of: str = ""
    provenance: str = ""


@dataclass(frozen=True)
class Verdict:
    status: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class Grade:
    verdict: str
    subject: Evidence
    independent_check: Optional[Mapping[str, Any]]


def changes_since(before: Optional[SourceSnapshot], after: SourceSnapshot) -> list[dict]:
    """First observation is baseline; never infer changes from truncated snapshots."""
    if before is None or not before.complete or not after.complete:
        return []
    old = {(e.source_id, e.field): e for e in before.items}
    def material(e):
        return (e.status, e.amount, e.currency, e.event_date, e.account, e.due_date, e.detail)
    return [{"kind": "new" if old.get((e.source_id, e.field)) is None else "changed",
             "before": asdict(old[(e.source_id, e.field)]) if (e.source_id, e.field) in old else None,
             "after": asdict(e)}
            for e in after.items if (e.source_id, e.field) not in old
            or material(old[(e.source_id, e.field)]) != material(e)]


def _matching(a: Evidence, b: Evidence) -> bool:
    try:
        same_amount = Decimal(a.amount) == Decimal(b.amount)
    except InvalidOperation:
        return False
    return (same_amount and bool(a.currency) and a.currency == b.currency
            and bool(a.account) and a.account == b.account
            and bool(a.match_ref) and a.match_ref == b.match_ref
            and a.event_date == b.event_date)


def assess_clearing(finance: Evidence, bank: Optional[Evidence],
                    qbo: Optional[Evidence] = None, *, qbo_fresh: bool = True) -> Verdict:
    """Bank-posted proof is required; same amount alone is never proof of identity."""
    evidence = (finance,) + ((bank,) if bank else ()) + ((qbo,) if qbo else ())
    if bank is None:
        return Verdict("bank clearing not proven", evidence)
    if bank.source == finance.source or not _matching(finance, bank) or bank.status.lower() not in {"posted", "cleared"}:
        return Verdict("mismatch - human review", evidence)
    if not qbo:
        return Verdict("bank posted", evidence)
    if not qbo_fresh:
        return Verdict("bank posted, QBO stale", evidence)
    if not _matching(finance, qbo):
        return Verdict("mismatch - human review", evidence)
    return Verdict("bank posted, corroborated by QBO", evidence)


def grade_item(subject: Evidence, independent_check: Optional[Mapping[str, Any]]) -> Grade:
    if not independent_check or not all(independent_check.get(k) for k in ("source", "source_id", "checked_at")):
        return Grade("unknown", subject, independent_check)
    if independent_check.get("source") == subject.source and independent_check.get("source_id") == subject.source_id:
        return Grade("unknown", subject, independent_check)
    match = independent_check.get("matched")
    return Grade("correct" if match is True else "incorrect" if match is False else "unknown",
                 subject, independent_check)


def scorecard(grades: Sequence[Grade]) -> dict:
    """Unknown grades stay in denominator; no self-reported evidence becomes accurate."""
    correct = sum(g.verdict == "correct" for g in grades)
    wrong = sum(g.verdict == "incorrect" for g in grades)
    unknown = len(grades) - correct - wrong
    return {"correct": correct, "incorrect": wrong, "unknown": unknown,
            "accuracy": str(Decimal(correct) / Decimal(len(grades))) if grades else None,
            "checks": [asdict(g) for g in grades]}


def build_daily_report(snapshots: Mapping[str, SourceSnapshot],
                       previous: Mapping[str, SourceSnapshot] | None = None,
                       grades: Sequence[Grade] = ()) -> dict:
    previous = previous or {}
    coverage = {}
    findings = []
    for source in REQUIRED_SOURCES:
        snap = snapshots.get(source)
        if snap is None:
            coverage[source] = {"complete": False, "error": "source not connected/read", "as_of": ""}
            continue
        complete = bool(snap.complete and snap.source == source and not snap.error)
        coverage[source] = {"complete": complete, "error": snap.error or ("" if complete else "unverified coverage"),
                            "as_of": snap.as_of, "provenance": snap.provenance}
        if complete:
            findings.extend(changes_since(previous.get(source), snap))
            if source == "EZLynx Accounting Team":
                findings.extend({"kind": "open task", "after": asdict(e)} for e in snap.items)
    return {"coverage": coverage, "findings": findings, "scorecard": scorecard(grades),
            "briefing_ready": all(v["complete"] for v in coverage.values()),
            "settlement_caveat": "Bank clearing not proven without matching bank-feed evidence."}
