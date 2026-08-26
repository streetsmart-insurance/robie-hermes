from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .operations import OperationsStore


UTC = timezone.utc
ATTENTION_STATUSES = {
    "UNVERIFIED",
    "FAILED",
    "PAUSED",
    "WAITING",
    "NEEDS_CLARIFICATION",
    "NEEDS_SKILL",
}


@dataclass(frozen=True)
class StatusDigest:
    text: str
    summary: dict[str, Any]
    report_run: dict[str, Any]


def prepare_status_digest(
    db_path: str,
    *,
    destination: str,
    window_hours: int = 2,
    now: datetime | None = None,
    artifact_root: str | None = None,
) -> StatusDigest:
    """Prepare an idempotent operational report without invoking a model."""
    if window_hours < 1 or window_hours > 24:
        raise ValueError("report window must be between 1 and 24 hours")
    end = now or datetime.now(UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    start = end - timedelta(hours=window_hours)
    ops = OperationsStore(db_path, artifact_root)
    jobs = []
    for job in ops.dashboard_rows()["jobs"]:
        updated = datetime.fromisoformat(str(job["updated_at"]).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        if start <= updated <= end:
            jobs.append(job)
    counts: dict[str, int] = {}
    for job in jobs:
        status = str(job.get("status") or "UNKNOWN")
        counts[status] = counts.get(status, 0) + 1
    attention = [job for job in jobs if str(job.get("status")) in ATTENTION_STATUSES]
    input_tokens = sum(int(job.get("input_tokens") or 0) for job in jobs)
    output_tokens = sum(int(job.get("output_tokens") or 0) for job in jobs)
    cache_read_tokens = sum(int(job.get("cache_read_tokens") or 0) for job in jobs)
    estimated_cost = sum(float(job.get("estimated_cost_usd") or 0) for job in jobs)
    summary = {
        "window_hours": window_hours,
        "jobs_updated": len(jobs),
        "status_counts": counts,
        "needs_attention": len(attention),
        "attention_job_ids": [job["id"] for job in attention[:20]],
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "total_tokens": input_tokens + output_tokens,
        "estimated_cost_usd": round(estimated_cost, 6),
    }
    status_text = ", ".join(
        f"{name}: {count}" for name, count in sorted(counts.items())
    ) or "no Job updates"
    text = (
        f"ROBIE {window_hours}-hour status report\n"
        f"Jobs updated: {len(jobs)} ({status_text})\n"
        f"Needs attention: {len(attention)}\n"
        f"Model usage: {summary['total_tokens']} tokens; "
        f"estimated ${summary['estimated_cost_usd']:.6f}"
    )
    report = ops.record_report_run(
        report_type=f"STATUS_{window_hours}H",
        destination=destination,
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        status="PREPARED",
        summary={**summary, "text": text},
    )
    return StatusDigest(text=text, summary=summary, report_run=report)
