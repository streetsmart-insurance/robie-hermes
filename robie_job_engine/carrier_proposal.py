from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .chat_policy import forbidden_tool_request
from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult
from .store import JobStore


DEFAULT_AGENCY_FEE_USD = 350
FEE_RE = re.compile(r"\$\s*350(?:\.00)?\b")


class ProposalDestination(Protocol):
    def write(self, proposal_id: str, document: dict[str, Any]) -> None: ...
    def read_fresh(self, proposal_id: str) -> dict[str, Any] | None: ...


class MemoryProposalDestination:
    """In-memory destination used by tests and the bounded Test worker."""

    def __init__(self) -> None:
        self._documents: dict[str, dict[str, Any]] = {}

    def write(self, proposal_id: str, document: dict[str, Any]) -> None:
        self._documents[proposal_id] = json.loads(json.dumps(document))

    def read_fresh(self, proposal_id: str) -> dict[str, Any] | None:
        document = self._documents.get(proposal_id)
        if document is None:
            return None
        return json.loads(json.dumps(document))


def count_agency_fee(text: str, fee_usd: int = DEFAULT_AGENCY_FEE_USD) -> int:
    pattern = re.compile(rf"\$\s*{fee_usd}(?:\.00)?\b")
    return len(pattern.findall(text or ""))


def _quote_text(payload: dict[str, Any]) -> str:
    return str(payload.get("quote_text") or payload.get("quote_pdf_text") or "")


def _requested_fee(payload: dict[str, Any], text: str) -> int:
    raw = payload.get("agency_fee_usd")
    if raw is not None:
        return int(raw)
    match = re.search(r"\$\s*(\d+)(?:\.00)?", text)
    if match:
        return int(match.group(1))
    return DEFAULT_AGENCY_FEE_USD


def _page_count(payload: dict[str, Any], quote_text: str, request_text: str = "") -> int:
    if payload.get("page_count"):
        return int(payload["page_count"])
    if payload.get("expected_page_count"):
        return int(payload["expected_page_count"])
    match = re.search(r"(\d+)\s*-?\s*page", request_text)
    if match:
        return int(match.group(1))
    if quote_text and "\f" in quote_text:
        return quote_text.count("\f") + 1
    return 10


class BoundedCarrierProposalWorker:
    """Build a carrier proposal without terminal, raw-file, or code execution."""

    def __init__(self, destination: ProposalDestination, store: JobStore | None = None) -> None:
        self.destination = destination
        self.store = store

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        payload = dict(job["payload"])
        if self.store is not None:
            ingestion = self.store.get_checkpoint(job["id"], "ingestion") or {}
            artifacts = ingestion.get("artifacts") or []
            if artifacts:
                payload["artifacts"] = artifacts
                first = artifacts[0]
                if not payload.get("quote_text") and first.get("stored_path"):
                    payload["quote_text"] = Path(first["stored_path"]).read_bytes().decode(
                        "utf-8", "replace"
                    )
                payload.setdefault("quote_sha256", first.get("sha256"))
        text = str(payload.get("text") or "")
        forbidden = forbidden_tool_request(payload, text)
        if forbidden:
            return WorkerResult(
                False, job["action_type"], {}, retryable=False, error=forbidden
            )
        quote_text = _quote_text(payload)
        artifacts = payload.get("artifacts") or []
        if not quote_text and not artifacts:
            return WorkerResult(
                False,
                "carrier.proposal",
                {},
                retryable=False,
                error="carrier proposal requires a staged quote attachment",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        fee = _requested_fee(payload, text)
        if fee != DEFAULT_AGENCY_FEE_USD:
            return WorkerResult(
                False,
                "carrier.proposal",
                {},
                retryable=False,
                error=f"unsupported agency fee {fee}; only ${DEFAULT_AGENCY_FEE_USD} is allowed",
            )
        if count_agency_fee(quote_text, fee):
            return WorkerResult(
                False,
                "carrier.proposal",
                {},
                retryable=False,
                error="quote already contains the agency fee; refusing to duplicate it",
            )
        proposal_id = payload.get("proposal_id") or f"proposal:{idempotency_key}"
        pages = _page_count(payload, quote_text, text)
        body = quote_text.rstrip() + f"\n\nAgency fee: ${fee}.00\n"
        document = {
            "proposal_id": proposal_id,
            "page_count": pages,
            "agency_fee_usd": fee,
            "agency_fee_occurrences": 1,
            "source_quote_sha256": payload.get("quote_sha256")
            or hashlib.sha256(quote_text.encode()).hexdigest(),
            "body": body,
            "idempotency_key": idempotency_key,
        }
        self.destination.write(proposal_id, document)
        return WorkerResult(
            True,
            "carrier.proposal",
            {
                "proposal_id": proposal_id,
                "page_count": pages,
                "agency_fee_usd": fee,
                "agency_fee_occurrences": 1,
            },
            {"idempotency_key": idempotency_key},
        )


class CarrierProposalVerifier:
    """Read the destination proposal independently and store evidence."""

    def __init__(self, destination: ProposalDestination) -> None:
        self.destination = destination

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        expected = {
            "proposal_id": action.get("destination", {}).get("proposal_id"),
            "page_count": int(
                job["payload"].get("expected_page_count")
                or action.get("destination", {}).get("page_count")
                or 10
            ),
            "agency_fee_usd": DEFAULT_AGENCY_FEE_USD,
            "agency_fee_occurrences": 1,
        }
        observed_doc = self.destination.read_fresh(expected["proposal_id"]) if expected["proposal_id"] else None
        if observed_doc is None:
            evidence = VerificationEvidence(
                method="FRESH_PROPOSAL_READBACK",
                source="proposal-destination",
                expected=expected,
                observed={},
                authoritative=True,
                captured_at=datetime.now(timezone.utc).isoformat(),
                locator=expected["proposal_id"],
            )
            return VerificationResult(
                False,
                evidence,
                retryable=True,
                error="proposal destination is not available yet",
                hold_status=JobStatus.WAITING,
            )
        observed = {
            "proposal_id": observed_doc.get("proposal_id"),
            "page_count": int(observed_doc.get("page_count") or 0),
            "agency_fee_usd": int(observed_doc.get("agency_fee_usd") or 0),
            "agency_fee_occurrences": count_agency_fee(
                str(observed_doc.get("body") or ""), DEFAULT_AGENCY_FEE_USD
            ),
        }
        evidence = VerificationEvidence(
            method="FRESH_PROPOSAL_READBACK",
            source="proposal-destination",
            expected=expected,
            observed=observed,
            authoritative=True,
            captured_at=datetime.now(timezone.utc).isoformat(),
            locator=expected["proposal_id"],
        )
        if observed["agency_fee_occurrences"] != 1:
            return VerificationResult(
                False,
                evidence,
                retryable=False,
                error=(
                    "agency fee $350 must appear exactly once; "
                    f"observed {observed['agency_fee_occurrences']}"
                ),
            )
        verified = all(observed.get(key) == value for key, value in expected.items())
        return VerificationResult(
            verified,
            evidence,
            retryable=not verified,
            error=None if verified else "proposal destination state does not match",
        )
