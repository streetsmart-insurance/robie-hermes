"""Applicant index for certificates intake.

Fast-path matcher: resolve an intake email to an EZLynx applicant using the
phone-accountability full-book applicant export (Account Name, Applicant ID,
Email - Primary, phones).

The workbook is client PII and is NEVER committed to the repo. The builder
reads it from a runtime path (``CERT_APPLICANT_REPORT_PATH``); unit tests use
tiny synthetic row fixtures.

Carlo's rules enforced here:

- The index is a fast path, not the source of truth. A miss means HOLD for an
  EZLynx lookup (chunk 2) — never "no applicant". The export is dated and is
  known to omit rows that exist in EZLynx (DIAMOND T EXPRESS LLC, 2026-09-25).
- Ambiguity means HOLD. One normalized name mapping to 2+ applicant IDs is
  never resolved by guessing.
- The certificate HOLDER is never a match key: holders are third parties. A
  holder name that looks like one of our clients must not route the filing.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

REPORT_PATH_ENV = "CERT_APPLICANT_REPORT_PATH"

MATCHED = "MATCHED"
NO_MATCH = "NO_MATCH"
AMBIGUOUS = "AMBIGUOUS"


def normalize_account_name(name: str | None) -> str:
    """Collapse an account name to a match key.

    Strips whitespace/case/punctuation: ``'   Fonseca General Contractor LLC'``
    and ``'fonseca general contractor llc'`` become the same key, as do
    ``'MAR Engineering, P.C.'`` and ``'MAR Engineering PC'``.
    """
    if not name:
        return ""
    key = name.strip().lower()
    key = re.sub(r"\s+", " ", key)
    key = re.sub(r"[^a-z0-9]", "", key)
    return key


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def normalize_phone(phone: str | None) -> str:
    """Digits only; also yields the last-10 form for US numbers."""
    digits = re.sub(r"\D", "", phone or "")
    return digits


def phone_keys(phone: str | None) -> list[str]:
    digits = normalize_phone(phone)
    if not digits:
        return []
    keys = [digits]
    if len(digits) == 11 and digits.startswith("1"):
        keys.append(digits[1:])
    return keys


@dataclass
class ApplicantIndex:
    """In-memory lookup built from the full-book export."""

    by_name: dict[str, list[int]] = field(default_factory=dict)
    by_email: dict[str, int] = field(default_factory=dict)
    by_phone: dict[str, int] = field(default_factory=dict)
    row_count: int = 0
    source_path: str = ""
    source_mtime: str = ""
    built_at: str = ""

    def stats(self) -> dict[str, Any]:
        collisions = sum(1 for ids in self.by_name.values() if len(ids) > 1)
        return {
            "rows": self.row_count,
            "unique_names": len(self.by_name),
            "unique_emails": len(self.by_email),
            "unique_phones": len(self.by_phone),
            "ambiguous_names": collisions,
            "source_path": self.source_path,
            "source_mtime": self.source_mtime,
            "built_at": self.built_at,
        }


def build_index(rows: list[dict[str, Any]], source_path: str = "") -> ApplicantIndex:
    """Build an index from row dicts.

    Row keys: ``account_name``, ``applicant_id``, ``email_primary``,
    ``phones`` (list of raw phone strings). Malformed rows are skipped,
    never fatal: a bad row must not take down the whole index.
    """
    index = ApplicantIndex(source_path=source_path)
    for row in rows:
        try:
            applicant_id = int(row.get("applicant_id") or 0)
        except (TypeError, ValueError):
            continue
        if applicant_id <= 0:
            continue
        index.row_count += 1
        name_key = normalize_account_name(row.get("account_name"))
        if name_key:
            bucket = index.by_name.setdefault(name_key, [])
            if applicant_id not in bucket:
                bucket.append(applicant_id)
        email_key = normalize_email(row.get("email_primary"))
        if email_key and "@" in email_key and email_key not in index.by_email:
            index.by_email[email_key] = applicant_id
        for raw_phone in row.get("phones") or []:
            for key in phone_keys(raw_phone):
                if key not in index.by_phone:
                    index.by_phone[key] = applicant_id
    try:
        mtime = os.path.getmtime(source_path) if source_path else 0
        if mtime:
            index.source_mtime = datetime.fromtimestamp(
                mtime, tz=timezone.utc
            ).isoformat()
    except OSError:
        pass
    index.built_at = datetime.now(timezone.utc).isoformat()
    return index


def load_index_from_workbook(path: str = "") -> ApplicantIndex:
    """Build the index from the full-book .xlsx export.

    Expected header row: Account Name | Applicant ID | Applicant Type |
    Phone - Home | Phone - Cell | Email - Primary | Assigned Producer |
    Phone - Business | Phone - Work
    """
    try:
        import openpyxl  # lazy: only the runtime needs it
    except ImportError as exc:
        raise RuntimeError(
            "openpyxl is required to read the applicant report; "
            "install it on the worker host"
        ) from exc
    path = path or os.environ.get(REPORT_PATH_ENV, "")
    if not path or not os.path.exists(path):
        raise RuntimeError(
            f"applicant report not found at {path!r}; set {REPORT_PATH_ENV}"
        )
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    headers = [str(c.value or "").strip() for c in next(ws.iter_rows(min_row=1, max_row=1))]

    def col(name: str) -> int | None:
        for i, h in enumerate(headers):
            if h.lower() == name.lower():
                return i
        return None

    i_name = col("Account Name")
    i_id = col("Applicant ID")
    i_email = col("Email - Primary")
    phone_cols = [c for c in (col("Phone - Home"), col("Phone - Cell"),
                              col("Phone - Business"), col("Phone - Work"))
                  if c is not None]
    if i_name is None or i_id is None:
        raise RuntimeError(f"report headers unusable: {headers}")

    rows: list[dict[str, Any]] = []
    for values in ws.iter_rows(min_row=2, values_only=True):
        rows.append({
            "account_name": values[i_name] if i_name is not None else "",
            "applicant_id": values[i_id] if i_id is not None else 0,
            "email_primary": values[i_email] if i_email is not None else "",
            "phones": [values[c] for c in phone_cols if values[c]],
        })
    return build_index(rows, source_path=path)


@dataclass
class MatchResult:
    status: str  # MATCHED | NO_MATCH | AMBIGUOUS
    applicant_id: int | None = None
    evidence: str = ""
    candidates: list[int] = field(default_factory=list)
    requester_also_client: bool = False

    def hold_reason(self) -> str:
        if self.status == MATCHED:
            return ""
        if self.status == AMBIGUOUS:
            return (
                "applicant name matches %d records (%s); holding for human "
                "disambiguation — never guessing"
                % (len(self.candidates), ", ".join(map(str, self.candidates)))
            )
        return (
            "no applicant in the full-book report; holding for EZLynx lookup "
            "(report is dated and known to omit rows)"
        )


def match_applicant(
    facts: Any, index: ApplicantIndex, *, phones: list[str] | None = None
) -> MatchResult:
    """Resolve request facts to one applicant, or hold.

    Precedence is deliberate:

    1. ``insured_name`` — the certificate is FOR the insured; the insured is
       the client even when the sender is a third party (GC, holder, town).
    2. ``dba`` — same as the insured name.
    3. ``requester_email`` — only when no insured/dba matched; covers the
       common case of the client writing from their own address without
       naming themselves.
    4. phone numbers — optional extra signal.

    ``holder_names`` are NEVER match keys (holders are third parties).
    """
    insured_key = normalize_account_name(getattr(facts, "insured_name", None))
    if insured_key:
        ids = index.by_name.get(insured_key, [])
        if len(ids) == 1:
            return _with_requester_flag(facts, index, MatchResult(
                MATCHED, ids[0], f"insured name {facts.insured_name!r}"))
        if len(ids) > 1:
            return MatchResult(AMBIGUOUS, None,
                               f"insured name {facts.insured_name!r}",
                               candidates=list(ids))

    dba_key = normalize_account_name(getattr(facts, "dba", None))
    if dba_key:
        ids = index.by_name.get(dba_key, [])
        if len(ids) == 1:
            return _with_requester_flag(facts, index, MatchResult(
                MATCHED, ids[0], f"dba {facts.dba!r}"))
        if len(ids) > 1:
            return MatchResult(AMBIGUOUS, None,
                               f"dba {facts.dba!r}", candidates=list(ids))

    email_key = normalize_email(getattr(facts, "requester_email", None))
    if email_key and email_key in index.by_email:
        return MatchResult(MATCHED, index.by_email[email_key],
                           f"sender email {email_key}")

    for raw in phones or []:
        for key in phone_keys(raw):
            if key in index.by_phone:
                return MatchResult(MATCHED, index.by_phone[key],
                                   f"phone {raw}")

    return MatchResult(NO_MATCH, None, "no name/email/phone hit in report")


def _with_requester_flag(facts: Any, index: ApplicantIndex,
                         result: MatchResult) -> MatchResult:
    """Flag when the sender is ALSO a client (third-party-shaped edge)."""
    email_key = normalize_email(getattr(facts, "requester_email", None))
    other = index.by_email.get(email_key) if email_key else None
    if other and other != result.applicant_id:
        result.requester_also_client = True
        result.evidence += f"; note: sender is also applicant {other}"
    return result
