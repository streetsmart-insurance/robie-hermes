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
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _fold_unicode(text: str | None) -> str:
    """Transliterate accented characters to ASCII (é -> e, ñ -> n).

    "José García" and "Jose Garcia" are the same client for matching.
    """
    return unicodedata.normalize("NFKD", text or "").encode(
        "ascii", "ignore").decode("ascii")

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
    key = _fold_unicode(name).strip().lower()
    key = re.sub(r"\s+", " ", key)
    key = re.sub(r"[^a-z0-9]", "", key)
    return key


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def normalize_policy_number(policy: str | None) -> str:
    """Fold a policy number to a match key: uppercase, no whitespace.

    Alpha prefixes and dashes are significant ("ADMP000656-02" is not
    "00065602"); only case and whitespace are folded so the email's
    "Policy #: 9300216995" meets the book's "9300216995".
    """
    if not policy:
        return ""
    return re.sub(r"\s+", "", policy.strip().upper())


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
    # DBA-fragment runs: "AMOUR BUSINESS GROUP LLC DBA ABG TRANSPORTATION"
    # also answers to "abg transportation" (contiguous token runs of the
    # DBA portion, length >= 2).
    by_dba_run: dict[str, list[int]] = field(default_factory=dict)
    # Carrier policy numbers from the book's policy_numbers column
    # (semicolon-separated): "9300216995" answers to the applicant that
    # carries it. Normalized uppercase, whitespace stripped.
    by_policy: dict[str, list[int]] = field(default_factory=dict)
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
            "unique_policies": len(self.by_policy),
            "dba_runs": len(self.by_dba_run),
            "ambiguous_names": collisions,
            "source_path": self.source_path,
            "source_mtime": self.source_mtime,
            "built_at": self.built_at,
        }

    def all_applicant_ids(self) -> list[int]:
        """Every applicant ID present in the index.

        Used by the certificate sweep to register the index as its write
        allowlist: any client in the directory is a legitimate filing
        destination. Collects from every lookup map so rows missing a name,
        email, or phone are still covered.
        """
        ids: set[int] = set()
        for bucket in self.by_name.values():
            ids.update(bucket)
        for bucket in self.by_dba_run.values():
            ids.update(bucket)
        ids.update(self.by_email.values())
        ids.update(self.by_phone.values())
        for bucket in self.by_policy.values():
            ids.update(bucket)
        return sorted(ids)


def _name_tokens(name: str | None) -> list[str]:
    """Lowercase word tokens; punctuation becomes a separator (spaces kept).

    Distinct from normalize_account_name (which collapses everything) —
    DBA-fragment matching needs word boundaries. Apostrophes are removed
    (not split on) so "Tony's Pizza" and "Tonys Pizza" tokenize alike.
    """
    cleaned = re.sub(r"['\u2019]", "", _fold_unicode(name).lower())
    return re.sub(r"[^a-z0-9]+", " ", cleaned).split()


def _dba_runs(account_name: str | None) -> list[str]:
    """Contiguous token runs (length >= 2) of the DBA portion of a name.

    Only the part after the first "dba" token is used: "abg transportation"
    from "AMOUR BUSINESS GROUP LLC DBA ABG TRANSPORTATION". Runs shorter
    than 2 tokens are too generic to match on ("abg" alone could be
    anything).
    """
    tokens = _name_tokens(account_name)
    try:
        dba_at = tokens.index("dba")
    except ValueError:
        return []
    dba_tokens = tokens[dba_at + 1:]
    runs = []
    for length in range(2, len(dba_tokens) + 1):
        for i in range(len(dba_tokens) - length + 1):
            runs.append(" ".join(dba_tokens[i:i + length]))
    return runs


def _dba_run_key(name: str | None) -> str:
    """Lookup key for DBA-fragment matching: space-joined word tokens."""
    return " ".join(_name_tokens(name))


def build_index(rows: list[dict[str, Any]], source_path: str = "") -> ApplicantIndex:
    """Build an index from row dicts.

    Row keys: ``account_name``, ``applicant_id``, ``email_primary``,
    ``phones`` (list of raw phone strings), ``policy_numbers`` (semicolon-
    separated string or list of raw policy numbers). Malformed rows are
    skipped, never fatal: a bad row must not take down the whole index.
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
        for run in _dba_runs(row.get("account_name")):
            bucket = index.by_dba_run.setdefault(run, [])
            if applicant_id not in bucket:
                bucket.append(applicant_id)
        email_key = normalize_email(row.get("email_primary"))
        if email_key and "@" in email_key and email_key not in index.by_email:
            index.by_email[email_key] = applicant_id
        raw_policies = row.get("policy_numbers") or []
        if isinstance(raw_policies, str):
            raw_policies = raw_policies.split(";")
        for raw_pol in raw_policies:
            pol_key = normalize_policy_number(raw_pol)
            if pol_key:
                bucket = index.by_policy.setdefault(pol_key, [])
                if applicant_id not in bucket:
                    bucket.append(applicant_id)
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
    3. DBA-fragment — trade name from the book's legal name.
    4. ``policy_numbers`` — the carrier policy the email names. Strong key,
       but never overrides a name hit: name steps return first, so a
       policy pointing elsewhere cannot reroute a name-matched filing.
    5. ``requester_email`` — only when no insured/dba/policy matched; covers
       the common case of the client writing from their own address without
       naming themselves.
    6. phone numbers — optional extra signal.

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

    # DBA-fragment: the email names the trade name ("Abg Transportation")
    # while the book carries the legal name ("AMOUR BUSINESS GROUP LLC DBA
    # ABG TRANSPORTATION"). Weaker than an exact name hit, so a sender
    # email pointing at a DIFFERENT applicant makes it ambiguous -> hold.
    for label, raw_name in (("insured name", getattr(facts, "insured_name", None)),
                            ("dba", getattr(facts, "dba", None))):
        run_key = _dba_run_key(raw_name)
        if not run_key or len(run_key.split()) < 2:
            continue
        ids = index.by_dba_run.get(run_key, [])
        if len(ids) == 1:
            email_key = normalize_email(getattr(facts, "requester_email", None))
            email_hit = index.by_email.get(email_key) if email_key else None
            if email_hit and email_hit != ids[0]:
                return MatchResult(
                    AMBIGUOUS, None,
                    f"{label} DBA-fragment {raw_name!r} -> applicant {ids[0]} "
                    f"but sender email maps to applicant {email_hit}",
                    candidates=[ids[0], email_hit])
            return _with_requester_flag(facts, index, MatchResult(
                MATCHED, ids[0],
                f"{label} DBA-fragment {raw_name!r}"))
        if len(ids) > 1:
            return MatchResult(AMBIGUOUS, None,
                               f"{label} DBA-fragment {raw_name!r}",
                               candidates=list(ids))

    # Policy number: the email names the carrier policy the certificate
    # evidences ("Policy #: 9300216995") and the book carries the same
    # carrier data. A single hit routes; a policy shared by several rows
    # holds as ambiguous — never guessing. Name steps above return first,
    # so a policy number can never override an explicit name match.
    policy_hits: list[int] = []
    policy_evidence = ""
    for raw_pol in getattr(facts, "policy_numbers", None) or []:
        pol_key = normalize_policy_number(raw_pol)
        if not pol_key:
            continue
        ids = index.by_policy.get(pol_key, [])
        if ids and not policy_evidence:
            policy_evidence = f"policy number {raw_pol.strip()!r}"
        for pid in ids:
            if pid not in policy_hits:
                policy_hits.append(pid)
    if len(policy_hits) == 1:
        return _with_requester_flag(facts, index, MatchResult(
            MATCHED, policy_hits[0], policy_evidence))
    if len(policy_hits) > 1:
        return MatchResult(AMBIGUOUS, None, policy_evidence,
                           candidates=list(policy_hits))

    email_key = normalize_email(getattr(facts, "requester_email", None))
    if email_key and email_key in index.by_email:
        return MatchResult(MATCHED, index.by_email[email_key],
                           f"sender email {email_key}")

    for raw in phones or []:
        for key in phone_keys(raw):
            if key in index.by_phone:
                return MatchResult(MATCHED, index.by_phone[key],
                                   f"phone {raw}")

    return MatchResult(NO_MATCH, None, "no name/policy/email/phone hit in report")


def _with_requester_flag(facts: Any, index: ApplicantIndex,
                         result: MatchResult) -> MatchResult:
    """Flag when the sender is ALSO a client (third-party-shaped edge)."""
    email_key = normalize_email(getattr(facts, "requester_email", None))
    other = index.by_email.get(email_key) if email_key else None
    if other and other != result.applicant_id:
        result.requester_also_client = True
        result.evidence += f"; note: sender is also applicant {other}"
    return result
