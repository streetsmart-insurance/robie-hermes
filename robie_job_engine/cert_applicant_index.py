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

import json
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
            "dba_runs": len(self.by_dba_run),
            "ambiguous_names": collisions,
            "source_path": self.source_path,
            "source_mtime": self.source_mtime,
            "built_at": self.built_at,
        }


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
        for run in _dba_runs(row.get("account_name")):
            bucket = index.by_dba_run.setdefault(run, [])
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


# Agency-internal sender domains. An internal address on an applicant
# record is a staff contact, never the client — matching on it alone
# verified internal ops digests as certificate requests (2026-09-26).
_INTERNAL_SENDER_DOMAINS = ("streetsmart.insurance", "ssinj.com")


def _sender_is_internal(email: str | None) -> bool:
    if not email or "@" not in email:
        return False
    domain = email.split("@", 1)[1].lower()
    return any(domain == d or domain.endswith("." + d)
               for d in _INTERNAL_SENDER_DOMAINS)


# Vendor/compliance-system sender domains. Renewal notices from these
# systems name the insured IN THE MESSAGE; the sender address is never
# the client and must never be used as an email match key (sender !=
# insured). The insured-name match above is the client signal.
_VENDOR_SENDER_DOMAINS = (
    "registrymonitoring.com",  # RMIS
    "truckstop.com",           # RMIS support
    "highway.com",             # Highway
    "gohighway.com",
    "certs.highway.com",
    "mycoisolution.com",       # myCOI
    "mycoitracking.com",
    "certificial.com",         # Certificial
    "trustlayer.io",           # TrustLayer compliance requests name the
                               # insured after "for" in the subject
    "operfi.com",              # OperFi compliance requests name the insured
                               # in the subject
    "nextinsurance.com",       # Next Insurance
    "assurant.com",            # Assurant vendor notices
)


def _sender_is_vendor_system(email: str | None) -> bool:
    if not email or "@" not in email:
        return False
    domain = email.split("@", 1)[1].lower()
    return any(domain == d or domain.endswith("." + d)
               for d in _VENDOR_SENDER_DOMAINS)


# ---------------------------------------------------------------------------
# Human-verified sender aliases (2026-09-26/27 held-record research).
#
# Senders the report's email column doesn't know but a human resolved to a
# client (e.g. mela@seciinc.com -> Seci Construction Inc, 78540038: "I need
# a COI for Town of Berlin" names only the holder). Loaded from
# robie_job_engine/data/cert_sender_aliases.json; the workbook is the
# fast path, this file is the memory.
#
# Vendor/compliance-system, broker, holder, lender, and agency-internal
# senders are NEVER aliased — they represent many insureds and resolve per
# message. The loader drops any such entry even if the data file grew one
# (defense in depth; the research file already excludes them).
# ---------------------------------------------------------------------------

_SENDER_ALIASES_ENV = "CERT_SENDER_ALIASES_PATH"
_sender_alias_cache: dict[str, dict[str, Any]] | None = None
_sender_alias_cache_path: str | None = None


def _default_aliases_path() -> str:
    env = os.environ.get(_SENDER_ALIASES_ENV)
    if env:
        return env
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "data", "cert_sender_aliases.json")


def load_sender_aliases(path: str | None = None) -> dict[str, dict[str, Any]]:
    """Normalized sender email -> {applicant_id, confidence, ...}.

    Confidence is "strong" or "medium" (medium = resolved but worth a
    human glance; the record carries alias_confidence="medium" so review
    tooling can surface it). Malformed entries are skipped, never fatal.
    """
    global _sender_alias_cache, _sender_alias_cache_path
    path = path or _default_aliases_path()
    if _sender_alias_cache is not None and _sender_alias_cache_path == path:
        return _sender_alias_cache
    aliases: dict[str, dict[str, Any]] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    entries = data.get("aliases") if isinstance(data, dict) else []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        email = normalize_email(entry.get("sender"))
        try:
            applicant_id = int(entry.get("applicant_id") or 0)
        except (TypeError, ValueError):
            continue
        confidence = (entry.get("confidence") or "").strip().lower()
        if not email or "@" not in email or applicant_id <= 0:
            continue
        if confidence not in ("strong", "medium"):
            continue
        if _sender_is_internal(email) or _sender_is_vendor_system(email):
            # Vendor/internal senders are never the client, even if an
            # entry claimed otherwise — drop it rather than misfile.
            continue
        aliases[email] = {
            "applicant_id": applicant_id,
            "confidence": confidence,
            "account_name": entry.get("account_name") or "",
            "evidence": entry.get("evidence") or "",
        }
    _sender_alias_cache = aliases
    _sender_alias_cache_path = path
    return aliases


def clear_sender_alias_cache() -> None:
    """Test hook: forget the cached alias table."""
    global _sender_alias_cache, _sender_alias_cache_path
    _sender_alias_cache = None
    _sender_alias_cache_path = None


def _sender_signal(email: str | None, index: ApplicantIndex
                   ) -> tuple[int | None, dict[str, Any] | None]:
    """Resolve a sender email to (applicant_id, alias_entry|None).

    The human-verified alias table is consulted first; the report's email
    column is the fallback. Agency-internal and vendor-system senders never
    produce a signal (they are staff contacts or name-the-insured systems,
    never the client). Returns (None, None) when there is no signal.
    """
    email_key = normalize_email(email)
    if not email_key or "@" not in email_key:
        return None, None
    if _sender_is_internal(email_key) or _sender_is_vendor_system(email_key):
        return None, None
    alias = load_sender_aliases().get(email_key)
    if alias:
        return alias["applicant_id"], alias
    return index.by_email.get(email_key), None


@dataclass
class MatchResult:
    status: str  # MATCHED | NO_MATCH | AMBIGUOUS
    applicant_id: int | None = None
    evidence: str = ""
    candidates: list[int] = field(default_factory=list)
    requester_also_client: bool = False
    # "medium" when the match came from a medium-confidence sender alias
    # (human-resolved but worth a human glance). Review tooling surfaces
    # these; the match itself still resolves.
    alias_confidence: str | None = None

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
       naming themselves. The human-verified sender-alias table
       (robie_job_engine/data/cert_sender_aliases.json) is consulted
       first — a person resolved these senders to clients on 2026-09-26/27;
       medium-confidence aliases resolve but flag alias_confidence so
       review tooling can surface them. NEVER for agency-internal senders
       (@streetsmart.insurance, @ssinj.com): an internal address on an
       applicant record is a contact, not the client, and matching it
       verified internal ops digests as certificate requests (2026-09-26).
       NEVER for vendor-system senders (RMIS, Highway, myCOI, Certificial,
       TrustLayer, OperFi, Next, ...): they name the insured in the message
       and are never the insured themselves (sender != insured).
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
            email_hit, _ = _sender_signal(
                getattr(facts, "requester_email", None), index)
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

    # Sender signal: the human-verified alias table first (a person resolved
    # these senders to clients on 2026-09-26/27), then the report's email
    # column. Agency-internal senders are staff contacts and vendor-system
    # senders (RMIS/Highway/myCOI/Certificial/TrustLayer/OperFi/Next/...)
    # name the insured in the message — neither is ever the client, so
    # neither verifies on email alone. The insured name above is the client
    # signal; without it this is NO_MATCH, never a match to whoever the
    # sender is filed under.
    sender_hit, alias = _sender_signal(
        getattr(facts, "requester_email", None), index)
    if sender_hit:
        if alias:
            result = _with_requester_flag(facts, index, MatchResult(
                MATCHED, sender_hit,
                f"sender alias {normalize_email(getattr(facts, 'requester_email', None))} "
                f"(human-verified 2026-09-26/27): "
                f"{alias['account_name'] or 'applicant'} {sender_hit} "
                f"[{alias['confidence']} confidence]"))
            if alias["confidence"] == "medium":
                result.alias_confidence = "medium"
            return result
        return MatchResult(MATCHED, sender_hit,
                           f"sender email {normalize_email(getattr(facts, 'requester_email', None))}")

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
