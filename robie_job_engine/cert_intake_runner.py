"""Step-1 runner: intake one mailbox sweep end to end (read-only).

Pipeline per message:

1. discover new message ids (Gmail query; the unread flag is never the
   ledger — the durable checkpoint is)
2. fetch the full message + attachments
3. skip when the checkpoint already saw it (exactly-once per message)
4. skip noise (bot callbacks, bounces, automatic replies, out-of-office):
   mark processed and do not build a record or an unverified entry
5. extract request facts (email + readable PDFs; scanned PDFs hold)
6. match to an applicant via the full-book index (or hold)
7. record per-step metadata in the checkpoint so an interrupted run resumes
   the missing steps instead of duplicating or dropping work

This module performs NO EZLynx writes and creates NO tasks. Its output is a
list of intake records: matched (ready for chunk-2 verification) or held
(with a reason). Filing, tasking, and issuance are later chunks.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    is_noise,
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
    # Retry/dedupe identity for THIS insured target. Multi-insured
    # emails fan out into one record per named insured; each needs its
    # own retry-ledger row so a filed sibling can't clear a held one.
    # Defaults to the source message id (single-insured case).
    ledger_key: str = ""

    # Gmail internalDate (receive timestamp, ms since epoch). Used by the
    # sweep driver to enforce the today-forward cutoff.
    internal_ms: int | None = None

    def __post_init__(self) -> None:
        if not self.ledger_key:
            self.ledger_key = self.gmail_id

    @property
    def base_gmail_id(self) -> str:
        """Source message id (ledger_key without the #N fan-out suffix)."""
        return self.ledger_key.split("#")[0]

    def summary(self) -> dict[str, Any]:
        return {
            "gmail_id": self.gmail_id,
            "ledger_key": self.ledger_key,
            "subject": self.subject,
            "insured": self.facts.insured_name,
            "policy_numbers": self.facts.policy_numbers,
            "match": self.match.status,
            "applicant_id": self.match.applicant_id,
            "held": self.held,
            "hold_reason": self.hold_reason,
        }


def _build_records_for_email(email: CertEmail, facts: RequestFacts,
                             index: Any,
                             internal_ms: int | None = None
                             ) -> list[IntakeRecord]:
    """Fan out one email's insured names into per-name intake records.

    The whole primary blob is matched first. When it MATCHED or is
    AMBIGUOUS it already hit the applicant index ("Smith and Sons LLC"
    is one client), so there is exactly one record. Only when the blob
    matched NOTHING do the and-split parts each get their own record —
    matched, verified, filed, and held independently — so the second
    company in "COIs for ABC LLC and XYZ Inc" can never be silently
    dropped. Each record carries its own ``ledger_key`` so retry
    bookkeeping for one insured can never clear or conflate another's.
    """
    def make(name: str | None, ledger_key: str) -> IntakeRecord:
        sub_facts = replace(facts, insured_name=name,
                            additional_insured_names=[])
        match = match_applicant(sub_facts, index)
        held = match.status != MATCHED or facts.pdf_unreadable
        hold_reason = ""
        if facts.pdf_unreadable:
            hold_reason = ("PDF attachment unreadable (likely scanned); "
                           "holding for OCR/human review — never guessing")
        elif held:
            hold_reason = match.hold_reason()
        return IntakeRecord(
            gmail_id=email.gmail_id,
            thread_id=email.thread_id,
            subject=email.subject,
            from_header=email.from_header,
            date=email.date,
            facts=sub_facts,
            match=match,
            held=held,
            hold_reason=hold_reason,
            attachment_count=len(email.attachments),
            ledger_key=ledger_key,
            internal_ms=internal_ms,
        )

    primary = facts.insured_name
    first = make(primary, email.gmail_id)
    if first.match.status in (MATCHED, AMBIGUOUS):
        return [first]
    parts = [p for p in facts.additional_insured_names if p != primary]
    if not parts:
        return [first]
    # The blob was only a carrier phrase ("ABC LLC and XYZ Inc"); its
    # parts carry the meaning. Each part gets its own lifecycle; the
    # first reuses the message id so single-part behavior is unchanged.
    return [make(part, email.gmail_id if i == 0 else f"{email.gmail_id}#{i + 1}")
            for i, part in enumerate(parts)]
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

    Noise (the bot's own ``[cert-task-callback]`` confirmations, delivery
    failures, mailer-daemon, automatic replies, and out-of-office) is
    checkpointed and counted in ``stats["skipped_noise"]``. It produces
    no intake record, so the sweep cannot file it or queue it unverified.
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    records: list[IntakeRecord] = []
    stats = {"discovered": 0, "processed": 0, "duplicates": 0,
             "matched": 0, "held": 0, "errors": 0,
             "skipped_pre_cutoff": 0, "skipped_noise": 0}

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

        # Callbacks, bounces, and automatic replies are not certificate
        # requests. Checkpoint them so the next sweep does not re-examine
        # them, and do not build a record (no hold, no unverified entry).
        noise, noise_reason = is_noise(email)
        if noise:
            mark_processed(email, store, {
                "skipped": "noise",
                "reason": noise_reason,
                "swept_at": stamp,
            })
            stats["skipped_noise"] += 1
            continue

        facts = extract_request_facts(
            email,
            pdf_text_extractor=pdf_text_extractor or _default_pdf_extractor(gmail),
        )
        # One record per named insured (multi-insured fan-out); the
        # message-level checkpoint below still fires exactly once.
        new_records = _build_records_for_email(email, facts, index,
                                               internal_ms=internal_ms)
        for record in new_records:
            records.append(record)
            if record.held:
                stats["held"] += 1
            else:
                stats["matched"] += 1
            stats["processed"] += 1

        mark_processed(email, store, meta={
            "stage": "intake_complete",
            "swept_at": stamp,
            # Message-level (primary record) keys — kept for checkpoint
            # readers that predate the multi-insured fan-out.
            "match": new_records[0].match.status,
            "applicant_id": new_records[0].match.applicant_id,
            "held": new_records[0].held,
            "hold_reason": new_records[0].hold_reason,
            "insured": facts.insured_name,
            "policy_numbers": facts.policy_numbers,
            # Multi-insured fan-out detail.
            "insured_names": [r.facts.insured_name for r in new_records],
            "record_count": len(new_records),
            "phones": facts.phones,
        })

    return {"records": records, "stats": stats}


def _default_pdf_extractor(gmail: Any):
    def extract(content: bytes) -> str | None:
        if isinstance(content, (bytes, bytearray)) and content:
            text = _pdf_bytes_to_text(bytes(content))
            return text or None
        return None

    return extract
