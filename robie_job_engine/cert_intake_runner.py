"""Step-1 runner: intake one mailbox sweep end to end (read-only).

Pipeline per message:

1. discover new message ids (Gmail query; the unread flag is never the
   ledger — the durable checkpoint is)
2. fetch the full message + attachments
3. skip when the checkpoint already saw it (exactly-once per message)
4. extract request facts (email + readable PDFs; scanned PDFs hold)
5. match to an applicant via the full-book index (or hold)
6. record per-step metadata in the checkpoint so an interrupted run resumes
   the missing steps instead of duplicating or dropping work

This module performs NO EZLynx writes and creates NO tasks. Its output is a
list of intake records: matched (ready for chunk-2 verification) or held
(with a reason). Filing, tasking, and issuance are later chunks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .cert_applicant_index import (
    AMBIGUOUS,
    MATCHED,
    NO_MATCH,
    MatchResult,
    match_applicant,
)
from .cert_intake import (
    CertEmail,
    RequestFacts,
    dedupe_keys,
    discover_messages,
    extract_request_facts,
    is_duplicate,
    mark_processed,
)


@dataclass
class IntakeRecord:
    gmail_id: str
    thread_id: str
    subject: str
    from_header: str
    date: str
    facts: RequestFacts
    match: MatchResult
    held: bool = False
    hold_reason: str = ""
    attachment_count: int = 0
    # Gmail internalDate (receive timestamp, ms since epoch). Used by the
    # sweep driver to enforce the today-forward cutoff.
    internal_ms: int | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "gmail_id": self.gmail_id,
            "subject": self.subject,
            "insured": self.facts.insured_name,
            "policy_numbers": self.facts.policy_numbers,
            "match": self.match.status,
            "applicant_id": self.match.applicant_id,
            "held": self.held,
            "hold_reason": self.hold_reason,
        }


def message_internal_ms(payload: dict[str, Any] | None) -> int | None:
    """Gmail ``internalDate`` (receive time, ms since epoch), or None.

    This is the authoritative message timestamp for the today-forward
    cutoff — the Date: header is sender-controlled and can lie.
    """
    if not isinstance(payload, dict):
        return None
    try:
        ms = int(payload.get("internalDate") or 0)
    except (TypeError, ValueError):
        return None
    return ms or None


def _pdf_text_extractor(fetcher: Any, gmail_id: str):
    def extract(attachment_id: str) -> bytes | None:
        try:
            return fetcher(gmail_id, attachment_id)
        except Exception:
            return None

    return extract


def _pdf_bytes_to_text(content: bytes) -> str:
    if not content:
        return ""
    # Text-based PDFs expose their strings; scanned PDFs expose nothing.
    # A real OCR step (chunk 2) replaces this heuristic.
    try:
        raw = content.decode("latin-1")
    except Exception:
        return ""
    import re

    strings = re.findall(r"\((?:[^()\\]|\\.){3,}\)", raw)
    cleaned = " ".join(
        s[1:-1].replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
        for s in strings
    )
    words = [w for w in cleaned.split() if len(w) > 2]
    return " ".join(words[:2000])


def run_intake_once(
    gmail: Any,
    store: Any,
    index: Any,
    *,
    query: str,
    pdf_text_extractor: Any = None,
    now: Any = None,
    cutoff_ms: int | None = None,
) -> dict[str, Any]:
    """Run one intake sweep. Returns records, holds, and stats.

    ``gmail`` implements the intake port (``list_message_ids``,
    ``get_full_message``). ``store`` is the durable checkpoint
    (``seen``/``mark``). ``index`` is an ``ApplicantIndex``.

    ``cutoff_ms`` is the today-forward cutoff (Gmail internalDate, ms):
    messages received before it are skipped before intake — they are NOT
    checkpointed, NOT matched, and NOT added to the retry ledger. They
    count in ``stats["skipped_pre_cutoff"]``. ``None`` disables the
    cutoff (legacy behavior).
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    records: list[IntakeRecord] = []
    stats = {"discovered": 0, "processed": 0, "duplicates": 0,
             "matched": 0, "held": 0, "errors": 0,
             "skipped_pre_cutoff": 0}

    try:
        ids = discover_messages(gmail, query)
    except Exception as exc:
        return {"records": [], "stats": stats,
                "error": f"discovery failed: {exc}"}
    stats["discovered"] = len(ids)

    for gmail_id in ids:
        try:
            payload = gmail.get_full_message(gmail_id)
        except Exception as exc:
            stats["errors"] += 1
            continue

        if cutoff_ms is not None:
            internal_ms = message_internal_ms(payload)
            if internal_ms is not None and internal_ms < cutoff_ms:
                # Pre-cutoff: evaluated and excluded by policy. Not
                # checkpointed (a cutoff change could revisit it), never
                # ledgered.
                stats["skipped_pre_cutoff"] += 1
                continue
        else:
            internal_ms = message_internal_ms(payload)

        def fetcher(mid: str, aid: str, _g=gmail) -> bytes:
            return _g.get_attachment_bytes(mid, aid)

        try:
            email = CertEmail.from_gmail_api(payload, attachment_fetcher=fetcher)
        except Exception:
            stats["errors"] += 1
            continue

        duplicate, reason = is_duplicate(email, store)
        if duplicate:
            stats["duplicates"] += 1
            continue

        facts = extract_request_facts(
            email,
            pdf_text_extractor=pdf_text_extractor or _default_pdf_extractor(gmail),
        )
        match = match_applicant(facts, index)
        held = match.status != MATCHED or facts.pdf_unreadable
        hold_reason = ""
        if facts.pdf_unreadable:
            hold_reason = ("PDF attachment unreadable (likely scanned); "
                           "holding for OCR/human review — never guessing")
        elif held:
            hold_reason = match.hold_reason()

        record = IntakeRecord(
            gmail_id=email.gmail_id,
            thread_id=email.thread_id,
            subject=email.subject,
            from_header=email.from_header,
            date=email.date,
            facts=facts,
            match=match,
            held=held,
            hold_reason=hold_reason,
            attachment_count=len(email.attachments),
            internal_ms=internal_ms,
        )
        records.append(record)
        if held:
            stats["held"] += 1
        else:
            stats["matched"] += 1
        stats["processed"] += 1

        mark_processed(email, store, meta={
            "stage": "intake_complete",
            "swept_at": stamp,
            "match": match.status,
            "applicant_id": match.applicant_id,
            "held": held,
            "hold_reason": hold_reason,
            "insured": facts.insured_name,
            "policy_numbers": facts.policy_numbers,
        })

    return {"records": records, "stats": stats}


def _default_pdf_extractor(gmail: Any):
    def extract(content: bytes) -> str | None:
        if isinstance(content, (bytes, bytearray)) and content:
            text = _pdf_bytes_to_text(bytes(content))
            return text or None
        return None

    return extract
