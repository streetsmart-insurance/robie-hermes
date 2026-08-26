from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConfidenceAssessment:
    score: int
    level: str
    issues: tuple[str, ...]


def assess_job_confidence(job: dict[str, Any]) -> ConfidenceAssessment:
    """Calculate evidence-based confidence; never use model self-confidence."""
    status = str(job.get("status") or "")
    authoritative = int(job.get("authoritative_evidence_count") or 0)
    verified = int(job.get("verified_evidence_count") or 0)
    recording_status = str(job.get("recording_status") or "")
    recording_exempt = bool(job.get("recording_exemption"))
    issues: list[str] = []

    base = {
        "PENDING": 35,
        "NEEDS_SKILL": 30,
        "NEEDS_CLARIFICATION": 30,
        "WAITING": 40,
        "RUNNING": 50,
        "VERIFYING": 65,
        "RETRY_WAIT": 45,
        "PAUSED": 35,
        "UNVERIFIED": 20,
        "FAILED": 5,
        "COMPLETE": 75,
    }.get(status, 25)

    if status == "COMPLETE":
        if authoritative > 0 and verified > 0:
            base = 98
        else:
            base = 15
            issues.append("COMPLETE is missing authoritative verification evidence")
    elif status == "UNVERIFIED":
        issues.append("Destination state was not independently verified")
    elif status == "FAILED":
        issues.append("Job execution failed")
    elif status == "PAUSED":
        issues.append("Job is paused and needs attention")
    elif status == "RETRY_WAIT":
        issues.append("Job is waiting to retry")
    elif status == "WAITING":
        issues.append("Job is waiting on destination or human input")
    elif status == "NEEDS_CLARIFICATION":
        issues.append("Job needs clarification before execution")
    elif status == "NEEDS_SKILL":
        issues.append("Job needs a registered Skill before execution")

    if job.get("last_error"):
        issues.append(str(job["last_error"])[:300])
    if recording_status == "FAILED":
        issues.append("Diagnostic recording failed")
    elif not recording_exempt and recording_status not in {"READY", "RECORDING", "UPLOADING"}:
        issues.append("No diagnostic recording is available yet")

    score = max(0, min(100, base))
    level = "HIGH" if score >= 85 else "MEDIUM" if score >= 60 else "LOW"
    return ConfidenceAssessment(score, level, tuple(dict.fromkeys(issues)))
