"""The job receipt -- the minimum bar a job must clear to be called done.

The point of this module: make "done" a thing a machine checks, not a thing an
agent claims. An agent that says PASS without evidence attached gets its receipt
downgraded to UNVERIFIED automatically. That is the whole design.

Evidence rule (CR-4): a gate is only green if the receipt carries a value that
was RE-FETCHED from the destination system after the write, and quoted
literally. The agent's own memory of having acted counts for nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# The seven universal gates. Six apply to all four ops workflows; G6 is the
# only one whose definition changes per workflow.
# ---------------------------------------------------------------------------

GATES: dict[str, dict[str, str]] = {
    "G1_SUBJECT": {
        "name": "Subject verified",
        "asks": "Is this one real record, still active, still inside the working window?",
        "evidence": "Applicant ID + policy number + status + expiration re-read from EZLynx.",
    },
    "G2_SOURCE": {
        "name": "Source authentic",
        "asks": "Is this the right artifact, for the right term, from the carrier?",
        "evidence": "Document classification + term dates quoted from inside the PDF, not the filename.",
    },
    "G3_FILED": {
        "name": "Filed and associated",
        "asks": "Is the artifact in the right folder with the right label, tied to the policy?",
        "evidence": "Document library re-listed after upload; the entry quoted back.",
    },
    "G4_NOTED": {
        "name": "Note posted on the right card",
        "asks": "Policy header, literal values, ROBIE signature, existing discussion reused?",
        "evidence": "Discussion re-fetched; the posted note body quoted back.",
    },
    "G5_HANDOFF": {
        "name": "Handoff is a task, not a sentence",
        "asks": "If a human must act, does an assigned EZLynx task exist for that person?",
        "evidence": "Task ID + assignee + due date re-read from EZLynx.",
    },
    "G6_WRITE": {
        "name": "The workflow's own write",
        "asks": "Exactly one, correct scope. See per-workflow definition.",
        "evidence": "Destination state re-read; the written values quoted back.",
    },
    "G7_REVERIFIED": {
        "name": "Independently re-verified",
        "asks": "Was destination state re-fetched fresh, and does it match what was intended?",
        "evidence": "Proof JSON written, with the fetch timestamp after the write timestamp.",
    },
}

# G6 per workflow -- the one gate that differs.
G6_BY_WORKFLOW: dict[str, str] = {
    "manual_renewal":
        "Exactly one pending RWL shell on the correct account, premium matching the "
        "offer PDF, writing company set, producer/CSR = Carlo Ferrara, bind=false. "
        "Never Add Policy from a Quote ID.",
    "audit_verification":
        "Audit status recorded on the policy with the carrier's own figure (return or "
        "additional premium) quoted, term-verified to the current audit period. "
        "Never a prior-term final.",
    "policy_change":
        "Three-way match green: original request vs carrier endorsement/dec vs the "
        "EZLynx policy record. Zero exceptions before the task is closed; any "
        "discrepancy holds the task open.",
    "mortgagee_verification":
        "Mortgagee clause and escrow billing status written from the dec PDF (PDF wins "
        "over typed EZLynx fields), lender confirmation recorded, loan number quoted.",
}

TERMINAL = ("PASS", "UNVERIFIED", "BLOCKED", "CATASTROPHE")


@dataclass
class Evidence:
    """Proof for one gate. Must be re-fetched, must quote a literal value."""

    quoted_value: str
    refetched_at: str
    source: str                     # e.g. "ezlynx_api:get_applicant_policies"
    note: str = ""

    def is_valid(self, acted_at: str | None = None) -> tuple[bool, str]:
        if not self.quoted_value or not str(self.quoted_value).strip():
            return False, "no literal value quoted"
        if not self.refetched_at:
            return False, "no refetch timestamp"
        if not self.source or self.source.lower() in ("agent", "memory", "self", "assumed"):
            return False, f"source '{self.source}' is the agent's own claim, not the destination system"
        if acted_at and self.refetched_at < acted_at:
            return False, f"refetched_at {self.refetched_at} precedes the write at {acted_at} -- stale"
        return True, "ok"


@dataclass
class JobReceipt:
    job_id: str
    workflow: str
    applicant_id: str | None = None
    policy_number: str | None = None
    started_at: str = field(default_factory=_now)
    acted_at: str | None = None            # when the last write happened
    gates: dict[str, Evidence] = field(default_factory=dict)
    not_applicable: set[str] = field(default_factory=set)
    blocked_reason: str | None = None
    guard_blocks: list[dict[str, Any]] = field(default_factory=list)
    cardinal_violation: dict[str, Any] | None = None

    # -- recording ---------------------------------------------------------
    def gate(self, gate_id: str, quoted_value: str, source: str, note: str = "") -> "JobReceipt":
        if gate_id not in GATES:
            raise KeyError(f"unknown gate {gate_id}")
        self.gates[gate_id] = Evidence(quoted_value=str(quoted_value),
                                       refetched_at=_now(), source=source, note=note)
        return self

    def skip(self, gate_id: str, why: str) -> "JobReceipt":
        """Mark a gate not applicable. Requires a reason; 'n/a' alone is refused."""
        if len(why.strip()) < 12:
            raise ValueError("skipping a gate needs a real reason, not a placeholder")
        self.not_applicable.add(gate_id)
        return self

    def mark_acted(self) -> "JobReceipt":
        self.acted_at = _now()
        return self

    def block(self, reason: str) -> "JobReceipt":
        self.blocked_reason = reason
        return self

    # -- the verdict -------------------------------------------------------
    def outcome(self) -> tuple[str, list[str]]:
        """Returns (outcome, problems). This is the only thing allowed to say PASS."""
        if self.cardinal_violation:
            return "CATASTROPHE", [f"cardinal violation: {self.cardinal_violation.get('rule_id')}"]
        if self.blocked_reason:
            return "BLOCKED", [self.blocked_reason]

        problems: list[str] = []
        for gate_id in GATES:
            if gate_id in self.not_applicable:
                continue
            ev = self.gates.get(gate_id)
            if ev is None:
                problems.append(f"{gate_id} ({GATES[gate_id]['name']}): no evidence recorded")
                continue
            # G7 is the post-write recheck, so it alone must postdate the write.
            ok, why = ev.is_valid(self.acted_at if gate_id == "G7_REVERIFIED" else None)
            if not ok:
                problems.append(f"{gate_id} ({GATES[gate_id]['name']}): {why}")

        if problems:
            return "UNVERIFIED", problems
        return "PASS", []

    # -- output ------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        outcome, problems = self.outcome()
        return {
            "job_id": self.job_id,
            "workflow": self.workflow,
            "applicant_id": self.applicant_id,
            "policy_number": self.policy_number,
            "started_at": self.started_at,
            "acted_at": self.acted_at,
            "outcome": outcome,
            "problems": problems,
            "g6_definition": G6_BY_WORKFLOW.get(self.workflow, "UNDEFINED WORKFLOW"),
            "gates": {
                k: {"quoted_value": v.quoted_value, "refetched_at": v.refetched_at,
                    "source": v.source, "note": v.note}
                for k, v in self.gates.items()
            },
            "not_applicable": sorted(self.not_applicable),
            "guard_blocks": self.guard_blocks,
            "cardinal_violation": self.cardinal_violation,
            "written_at": _now(),
        }

    def write(self, directory: str | Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.workflow}_{self.job_id}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path

    def summary_line(self) -> str:
        outcome, problems = self.outcome()
        head = f"[{outcome}] {self.workflow} {self.policy_number or self.applicant_id or self.job_id}"
        if problems:
            return head + " -- " + "; ".join(problems[:3])
        return head
