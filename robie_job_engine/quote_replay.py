"""Local replay of a carrier-proposal Job from a real quote PDF path.

This harness never invents a PDF. When no readable quote path is provided it
stops with a blocker. COMPLETE is refused unless independent verifier evidence
is already stored. The report never claims live Test COMPLETE.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .carrier_proposal import (
    BoundedCarrierProposalWorker,
    CarrierProposalVerifier,
    MemoryProposalDestination,
)
from .chat_guard import open_chat_job
from .engine import JobEngine
from .models import JobStatus
from .store import JobStore
from .test_runtime import PRODUCTION_ENV_NAMES, ProductionGuardError, current_robie_env, require_test_environment


DEFAULT_PROPOSAL_TEXT = (
    "Create a carrier proposal from this quote and add a $350 fee"
)
RAZZA_QUOTE_FILENAME = (
    "Razza Renewal - The Hartford Workers Compensation Quote.pdf"
)
LIVE_HERMES_PREFIX = "/opt/streetsmart-hermes/"
TEST_HERMES_PREFIX = "/opt/streetsmart-hermes-test/"
LIVE_TEST_COMPLETE_CLAIM = (
    "local replay only; not live Test COMPLETE. "
    "An independent verifier must inspect stored evidence on the Test host."
)


class QuoteReplayError(RuntimeError):
    """Raised when replay cannot start or COMPLETE is not allowed."""


def resolve_quote_pdf(path: str | None) -> Path:
    """Require a real readable quote file. Never synthesize one."""
    if not path or not str(path).strip():
        raise QuoteReplayError(
            "BLOCKED: no quote PDF path provided. "
            f"The Razza file ({RAZZA_QUOTE_FILENAME}) exists only on the Hermes "
            "host and is not in Drive or Gmail. Do not invent a PDF. "
            "Pass --quote-pdf PATH when the real file is available."
        )
    quote = Path(path).expanduser()
    if not quote.is_file():
        raise QuoteReplayError(
            f"BLOCKED: quote PDF is not a readable file: {path}. "
            "Do not invent a substitute PDF."
        )
    if quote.stat().st_size <= 0:
        raise QuoteReplayError(f"BLOCKED: quote PDF is empty: {path}")
    return quote.resolve()


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def is_test_hermes_path(path: str | Path) -> bool:
    resolved = str(_resolved(path))
    return resolved == TEST_HERMES_PREFIX.rstrip("/") or resolved.startswith(
        TEST_HERMES_PREFIX
    )


def is_live_hermes_path(path: str | Path) -> bool:
    resolved = str(_resolved(path))
    if is_test_hermes_path(resolved):
        return False
    return resolved == LIVE_HERMES_PREFIX.rstrip("/") or resolved.startswith(
        LIVE_HERMES_PREFIX
    )


def refuse_production_targets(db_path: str, artifact_root: str) -> None:
    env = current_robie_env()
    if env in PRODUCTION_ENV_NAMES:
        raise ProductionGuardError(
            "refusing quote replay: ROBIE_ENV is Production"
        )
    for label, target in (("job database", db_path), ("artifact root", artifact_root)):
        if is_live_hermes_path(target):
            raise ProductionGuardError(
                f"refusing to write {label} on the live Hermes path: {target}"
            )
    if is_test_hermes_path(db_path) or is_test_hermes_path(artifact_root):
        require_test_environment()


def complete_is_allowed(job: dict[str, Any], evidence: list[dict[str, Any]]) -> bool:
    if JobStatus(job["status"]) != JobStatus.COMPLETE:
        return False
    return any(
        item.get("verified")
        and item.get("authoritative")
        and item.get("method")
        for item in evidence
    )


def refuse_complete_without_evidence(
    store: JobStore, job_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    job = store.get_job(job_id)
    evidence = store.list_evidence(job_id)
    if JobStatus(job["status"]) == JobStatus.COMPLETE and not complete_is_allowed(
        job, evidence
    ):
        raise QuoteReplayError(
            "COMPLETE refused: no stored independent verifier evidence"
        )
    return job, evidence


def build_replay_engine(
    store: JobStore, *, destination: MemoryProposalDestination | None = None
) -> tuple[JobEngine, MemoryProposalDestination]:
    dest = destination or MemoryProposalDestination()
    engine = JobEngine(
        store,
        {"carrier-proposal": BoundedCarrierProposalWorker(dest, store)},
        {"carrier.proposal": CarrierProposalVerifier(dest)},
        owner="quote-replay-harness",
    )
    return engine, dest


def build_replay_report(
    *,
    outcome: str,
    reason: str,
    job: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    quote_pdf: str | None = None,
    complete_refused: bool = False,
) -> dict[str, Any]:
    status = None
    job_id = None
    if job is not None:
        job_id = job.get("id")
        raw = job.get("status")
        status = raw.value if isinstance(raw, JobStatus) else raw
    return {
        "outcome": outcome,
        "reason": reason,
        "job_id": job_id,
        "status": status,
        "quote_pdf": quote_pdf,
        "evidence_count": len(evidence or []),
        "evidence": evidence or [],
        "complete_allowed": bool(job and complete_is_allowed(job, evidence or [])),
        "complete_refused": complete_refused,
        "live_test_complete": False,
        "claim": LIVE_TEST_COMPLETE_CLAIM,
    }


def run_quote_replay(
    *,
    quote_pdf: str | None,
    db_path: str,
    artifact_root: str,
    text: str = DEFAULT_PROPOSAL_TEXT,
    message_id: str = "quote-replay:local",
    destination: MemoryProposalDestination | None = None,
    engine: JobEngine | None = None,
) -> dict[str, Any]:
    """Create a durable Job, run worker + verifier, refuse false COMPLETE."""
    refuse_production_targets(db_path, artifact_root)
    try:
        quote = resolve_quote_pdf(quote_pdf)
    except QuoteReplayError as exc:
        return build_replay_report(outcome="BLOCKED", reason=str(exc))

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    Path(artifact_root).mkdir(parents=True, exist_ok=True, mode=0o700)

    job_id = open_chat_job(
        db_path,
        message_id,
        text,
        attachments=[(str(quote), "application/pdf")],
        expected_attachment_count=1,
        artifact_root=artifact_root,
        requested_by="quote-replay-harness",
        conversation_id="quote-replay",
    )
    store = JobStore(db_path)
    if not job_id:
        return build_replay_report(
            outcome="BLOCKED",
            reason="durable Job was not created",
            quote_pdf=str(quote),
        )
    job = store.get_job(job_id)
    if engine is None:
        engine, _destination = build_replay_engine(store, destination=destination)
    final = engine.run(job_id)
    try:
        final, evidence = refuse_complete_without_evidence(store, job_id)
    except QuoteReplayError as exc:
        return build_replay_report(
            outcome="REFUSED_COMPLETE",
            reason=str(exc),
            job=store.get_job(job_id),
            evidence=store.list_evidence(job_id),
            quote_pdf=str(quote),
            complete_refused=True,
        )
    status = JobStatus(final["status"])
    if status == JobStatus.COMPLETE:
        outcome = "COMPLETE"
        reason = (
            "local Job COMPLETE after stored independent verifier evidence; "
            + LIVE_TEST_COMPLETE_CLAIM
        )
    else:
        outcome = status.value
        reason = final.get("last_error") or (
            "Job did not COMPLETE; independent evidence is required before COMPLETE"
        )
    return build_replay_report(
        outcome=outcome,
        reason=reason,
        job=final,
        evidence=evidence,
        quote_pdf=str(quote),
        complete_refused=False,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay a carrier proposal from a real quote PDF. "
            "Does not invent a PDF and does not claim live Test COMPLETE."
        )
    )
    parser.add_argument(
        "--quote-pdf",
        default=os.environ.get("ROBIE_QUOTE_PDF"),
        help="Path to the real quote PDF. Required. The harness will not invent one.",
    )
    parser.add_argument(
        "--work-dir",
        default=os.environ.get("ROBIE_REPLAY_WORK_DIR", ".robie-local-replay"),
        help="Isolated local work directory (ignored when --db and --artifact-root are set).",
    )
    parser.add_argument("--db", default=os.environ.get("ROBIE_JOB_DB"))
    parser.add_argument(
        "--artifact-root", default=os.environ.get("ROBIE_ARTIFACT_ROOT")
    )
    parser.add_argument("--text", default=DEFAULT_PROPOSAL_TEXT)
    parser.add_argument("--message-id", default="quote-replay:local")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    work_dir = Path(args.work_dir).expanduser()
    db_path = args.db or str(work_dir / "jobs.db")
    artifact_root = args.artifact_root or str(work_dir / "artifacts")
    report = run_quote_replay(
        quote_pdf=args.quote_pdf,
        db_path=db_path,
        artifact_root=artifact_root,
        text=args.text,
        message_id=args.message_id,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["outcome"] == "BLOCKED":
        return 2
    if report["complete_refused"] or report["outcome"] == "REFUSED_COMPLETE":
        return 3
    if report["outcome"] == "COMPLETE":
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
