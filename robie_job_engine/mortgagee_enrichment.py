"""4372 mortgagee enrichment scaffold (Document API + structured fields).

Carlo / Ralph
==============
Report 4372 (Mortgagee Verification Queue - ROBIE) does not export lender
or loan number. Today ``plan_4372`` flags every open item as
"lender/loan not on file." This module is the **read-only enrichment
scaffold** that sits in front of that planner.

It is NOT live production delivery. Portal upload and Bland dials stay
out of this PR. Dry-run is the default. No live EZLynx write is issued
unless a later live-prove step injects a real DiscussionApi /
DocumentApi write client **and** an operator enables it.

How it plugs in
---------------
``verification_workers.run_worker`` (report 4372) calls
:func:`enrich_work_item` for each **open** work item (closed tasks are
already excluded). The result is stored on the item and passed to
``plan_4372``:

* ``ready`` — every mortgage has a unique structured lender + loan from
  agreeing sources. Planner still holds delivery (portal/Bland TODO).
* ``hitl`` — declaration structured fields disagree with policy fields.
  Halt. Do not pick a side.
* ``proven_zero`` — both structured sources returned an **explicit**
  empty mortgage collection. Skip is allowed only then.
* ``incomplete`` — lookup missing, failed, or no unique structured
  match. Same as today's "lender/loan not on file" — never a silent skip.

Rules this scaffold enforces
----------------------------
1. Look up current declarations via EZLynx DocumentApi search (metadata
   + numeric ``document_id`` read-back). Look up structured policy /
   mortgagee fields the codebase already has (PolicyApi search shape).
2. Emit **one record per mortgage**. Never merge two loans into one.
3. Never invent or guess. Never scrape a PDF / OCR blob into fields
   unless a **unique structured field name** matches (``MortgageeName``,
   ``LoanNumber``, …). Document *title* is not a lender name.
4. If declaration structured fields disagree with policy fields → HITL.
5. Zero mortgages must be **proven** (explicit empty collection on a
   successful read). A missing key or failed lookup is incomplete.
6. Never use SSN / full SSN (field names or ``NNN-NN-NNNN`` values).
7. Notes/docs writes stay API-only. Playwright/CDP helpers call
   ``refuse_playwright_note_or_doc`` and cannot authorize COMPLETE.

TODO — next live-prove step (not this PR)
-----------------------------------------
* Bind a real ``EzlynxApiClient`` on Test only; prove DocumentApi search
  + PolicyApi search against the ROBIE Test applicant.
* Confirm which PolicyApi / DocumentApi metadata keys actually carry
  mortgagee name and loan number (do not guess new keys in prod).
* Wire producer-gate + lender-of-record portal lookup (existing
  ``mortgagee_verification_worker``) **after** enrichment is READY.
* Portal agent-section delivery and Bland mortgage-company dials.
* DiscussionApi note of enrichment outcome with ``note_id`` read-back.
* No Production zip / deploy from this scaffold.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

# Unique structured field names only. Folded: lowercase, no separators.
# "lender" is included because EZLynx / portal payloads already use it as
# the servicer display name; the value must still be a non-empty string.
_LENDER_KEYS = frozenset({
    "mortgageename",
    "mortgagee",
    "lendername",
    "mortgagecompany",
    "mortgagecompanyname",
    "lender",
    "servicer",
    "servicername",
})
_LOAN_KEYS = frozenset({
    "loannumber",
    "mortgageloannumber",
    "loanno",
    "mortgageloanno",
    "loan",
})
# Collections that are themselves lists of mortgage objects.
_LIST_KEYS = frozenset({
    "mortgagees",
    "mortgages",
    "additionalinterests",
    "additionalinterest",
    "lenders",
})
# Envelope keys PolicyApi / DocumentApi already wrap results in.
_ENVELOPE_KEYS = frozenset({
    "data",
    "results",
    "records",
    "items",
    "documents",
    "policies",
})
_SSN_KEYS = frozenset({
    "ssn",
    "fullssn",
    "socialsecurity",
    "socialsecuritynumber",
    "taxpayerssn",
    "ssnfull",
    "socialsecurityno",
})
# Values that would invent fields from a scrape. Never read these as lender/loan.
_SCRAPE_KEYS = frozenset({
    "ocr",
    "ocrtext",
    "pdftext",
    "pdfscrape",
    "extractedtext",
    "fulltext",
    "rawtext",
    "pagetext",
    "textextract",
})
_FORBIDDEN_OUTPUT_KEYS = frozenset({"ssn", "full_ssn", "social_security_number",
                                    "social_security", "ocr_text", "pdf_text"})

_SSN_VALUE_RE = re.compile(r"^\d{3}-\d{2}-\d{4}$")
_DEC_TYPE_VALUES = frozenset({
    "declaration",
    "declarations",
    "dec page",
    "declarations page",
    "declaration page",
})

STATUS_READY = "ready"
STATUS_HITL = "hitl"
STATUS_PROVEN_ZERO = "proven_zero"
STATUS_INCOMPLETE = "incomplete"

REASON_LENDER_NOT_ON_FILE = "lender/loan not on file — structured enrichment incomplete"
REASON_HITL_CONFLICT = (
    "HITL: declaration structured fields disagree with policy fields — "
    "not picking a side"
)
REASON_PROVEN_ZERO = "zero mortgages proven — explicit empty structured result"
REASON_PDF_SCRAPE = (
    "refused: will not scrape a PDF/OCR blob into lender or loan fields"
)
REASON_NO_INVENT = "refused: no unique structured field match — will not invent values"
REASON_SSN = "refused: SSN / full SSN is never used for mortgagee enrichment"


class MortgageeEnrichmentError(ValueError):
    """Fail-closed enrichment refusal (SSN, PDF scrape, Playwright write)."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MortgageRecord:
    """One mortgage on the policy. Never contains SSN or scraped PDF text."""

    lender_name: str
    loan_number: str
    source: str  # policy | declaration | agreed
    document_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _looks_like_ssn(self.lender_name) or _looks_like_ssn(self.loan_number):
            raise MortgageeEnrichmentError(REASON_SSN)
        for key in ("ssn", "full_ssn"):
            if key in (self.lender_name.casefold(), self.loan_number.casefold()):
                raise MortgageeEnrichmentError(REASON_SSN)


@dataclass(frozen=True)
class DeclarationDoc:
    """Current declarations document from DocumentApi search (metadata only)."""

    document_id: str
    name: str
    read_back: bool = False

    def __post_init__(self) -> None:
        if not str(self.document_id).isdigit():
            raise MortgageeEnrichmentError(
                "DocumentApi row without a numeric document_id cannot be used"
            )


@dataclass
class SourceSnapshot:
    """One structured source after a successful or failed lookup."""

    name: str
    available: bool
    # True only when the payload contained the mortgage collection key
    # and that collection was explicitly empty (or a successful DocumentApi
    # search returned zero declaration docs AND no structured mortgage fields
    # on any other row — that last case is NOT proven-empty mortgages).
    explicit_empty: bool
    mortgages: list[MortgageRecord] = field(default_factory=list)
    declaration_docs: list[DeclarationDoc] = field(default_factory=list)
    conflict: str = ""
    error: str = ""
    read_back: dict[str, Any] = field(default_factory=dict)


@dataclass
class EnrichmentResult:
    """Per-policy enrichment outcome for an open 4372 work item."""

    policy_number: str
    applicant_id: str
    status: str
    reason: str
    dry_run: bool
    mortgages: list[MortgageRecord] = field(default_factory=list)
    declaration_docs: list[DeclarationDoc] = field(default_factory=list)
    policy_snapshot: dict[str, Any] = field(default_factory=dict)
    declaration_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "policy_number": self.policy_number,
            "applicant_id": self.applicant_id,
            "status": self.status,
            "reason": self.reason,
            "dry_run": self.dry_run,
            "mortgages": [asdict(m) for m in self.mortgages],
            "declaration_docs": [asdict(d) for d in self.declaration_docs],
            "policy_snapshot": dict(self.policy_snapshot),
            "declaration_snapshot": dict(self.declaration_snapshot),
        }
        _assert_no_forbidden_keys(payload)
        return payload


# ---------------------------------------------------------------------------
# Ports (inject fakes in tests; thin live clients later)
# ---------------------------------------------------------------------------


class PolicyMortgageePort(Protocol):
    """Read structured policy / mortgagee fields. Never writes."""

    def search_policy_by_number(self, policy_number: str) -> dict[str, Any]:
        """PolicyApi search payload (or a test double of that shape)."""


class DeclarationDocumentPort(Protocol):
    """Read-only DocumentApi search. Must not download PDFs for field fill."""

    def search_applicant_documents(self, applicant_id: str) -> dict[str, Any]:
        """DocumentApi document-search payload."""


@dataclass
class EnrichmentPorts:
    """Optional bound clients. Missing ports → incomplete, not proven-zero."""

    policy: PolicyMortgageePort | None = None
    documents: DeclarationDocumentPort | None = None


class ThinDocumentApiLookup:
    """Thin DocumentApi search + numeric id read-back.

    Downloads are intentionally not used for lender/loan extraction.
    """

    def __init__(self, client: Any):
        self._client = client

    def search_applicant_documents(self, applicant_id: str) -> dict[str, Any]:
        applicant = str(applicant_id or "").strip()
        if not applicant:
            raise MortgageeEnrichmentError("applicant id is required for DocumentApi search")
        search = getattr(self._client, "search_applicant_documents", None)
        if search is None:
            raise MortgageeEnrichmentError("DocumentApi client cannot search documents")
        payload = search(applicant)
        if not isinstance(payload, dict):
            payload = {"results": payload}
        # Read-back: every usable row must carry a numeric results[].id.
        ids = [row.document_id for row in _declaration_docs_from_search(payload)]
        if ids and not _document_ids_present(payload, ids):
            raise MortgageeEnrichmentError(
                f"DocumentApi read-back did not confirm document_id(s) {ids}"
            )
        return payload


class ThinPolicyApiLookup:
    """Thin PolicyApi search wrapper."""

    def __init__(self, client: Any):
        self._client = client

    def search_policy_by_number(self, policy_number: str) -> dict[str, Any]:
        number = str(policy_number or "").strip()
        if not number:
            raise MortgageeEnrichmentError("policy number is required for PolicyApi search")
        search = getattr(self._client, "search_policy_by_number", None)
        if search is None:
            raise MortgageeEnrichmentError("PolicyApi client cannot search policies")
        payload = search(number)
        return payload if isinstance(payload, dict) else {"data": payload}


# ---------------------------------------------------------------------------
# Key / value guards
# ---------------------------------------------------------------------------


def _fold_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key or "").casefold())


def _looks_like_ssn(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text and _SSN_VALUE_RE.match(text))


def _assert_no_ssn_keys(mapping: dict[str, Any]) -> None:
    for key in mapping:
        if _fold_key(key) in _SSN_KEYS:
            raise MortgageeEnrichmentError(REASON_SSN)


def _assert_no_forbidden_keys(obj: Any) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            folded = _fold_key(key)
            if folded in _SSN_KEYS or str(key).casefold() in _FORBIDDEN_OUTPUT_KEYS:
                raise MortgageeEnrichmentError(REASON_SSN)
            _assert_no_forbidden_keys(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _assert_no_forbidden_keys(item)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_lender_name(name: Any) -> str:
    """Compare-only normalization (case, punctuation). Never invents a name."""
    text = re.sub(r"[^a-z0-9 ]", " ", _clean_text(name).casefold())
    return " ".join(text.split())


def normalize_loan_number(value: Any) -> str:
    return _clean_text(value)


def looks_like_declaration(row: dict[str, Any]) -> bool:
    """True when a DocumentApi row is uniquely a declarations document.

    Uses document type / category structured fields, or a name that
    contains ``declaration`` / a ``dec`` word. Does not open the PDF.
    """
    if not isinstance(row, dict):
        return False
    for key in ("documentType", "DocumentType", "category", "Category",
                "documentCategory", "type", "Type"):
        typed = _clean_text(row.get(key)).casefold()
        if typed in _DEC_TYPE_VALUES:
            return True
    name = _clean_text(
        row.get("documentName")
        or row.get("DocumentName")
        or row.get("name")
        or row.get("Name")
        or row.get("fileName")
        or row.get("FileName")
        or ""
    )
    folded = name.casefold()
    if "declaration" in folded:
        return True
    return bool(re.search(r"\bdecs?\b", folded))


# ---------------------------------------------------------------------------
# Structured extraction (unique field match only)
# ---------------------------------------------------------------------------


def _unique_field(mapping: dict[str, Any], allowed: frozenset[str]) -> tuple[str, str]:
    """Return (value, conflict). Conflict if two allowed keys disagree."""
    found: list[str] = []
    for key, raw in mapping.items():
        folded = _fold_key(key)
        if folded in _SSN_KEYS:
            raise MortgageeEnrichmentError(REASON_SSN)
        if folded in _SCRAPE_KEYS:
            # Present scrape blob: refuse the whole record, do not read it.
            raise MortgageeEnrichmentError(REASON_PDF_SCRAPE)
        if folded not in allowed:
            continue
        if isinstance(raw, (dict, list, tuple, bool)):
            continue
        text = _clean_text(raw)
        if not text:
            continue
        if _looks_like_ssn(text):
            raise MortgageeEnrichmentError(REASON_SSN)
        found.append(text)
    unique = list(dict.fromkeys(found))
    if len(unique) > 1:
        return "", (
            "HITL: multiple structured field names disagree inside one record "
            f"({unique!r}) — not picking a side"
        )
    if len(unique) == 1:
        return unique[0], ""
    return "", ""


def extract_structured_mortgages(
    payload: Any,
    *,
    source: str,
    document_id: str = "",
) -> tuple[list[MortgageRecord], str, bool]:
    """Pull mortgages from unique structured fields only.

    Returns ``(mortgages, conflict_reason, saw_explicit_collection)``.

    ``saw_explicit_collection`` is True when a known list key was present
    (even if empty). A missing key is not proof of zero mortgages.
    """
    mortgages: list[MortgageRecord] = []
    saw_explicit = False

    def _from_mapping(mapping: dict[str, Any], doc_id: str) -> str:
        nonlocal saw_explicit
        _assert_no_ssn_keys(mapping)
        for key, raw in mapping.items():
            if _fold_key(key) in _SCRAPE_KEYS and _clean_text(raw):
                raise MortgageeEnrichmentError(REASON_PDF_SCRAPE)
        # Nested collections first so a parent object with both a list and
        # leftover scalar keys does not invent a duplicate mortgage.
        saw_list_on_this_mapping = False
        for key, raw in mapping.items():
            folded = _fold_key(key)
            if folded in _LIST_KEYS:
                saw_explicit = True
                saw_list_on_this_mapping = True
                if raw is None:
                    continue
                if isinstance(raw, list):
                    if not raw:
                        continue
                    for item in raw:
                        if isinstance(item, dict):
                            conflict = _from_mapping(item, doc_id)
                            if conflict:
                                return conflict
                        # A bare string in the list is not a unique field match.
                elif isinstance(raw, dict):
                    conflict = _from_mapping(raw, doc_id)
                    if conflict:
                        return conflict
        if saw_list_on_this_mapping:
            return ""
        lender, lender_conflict = _unique_field(mapping, _LENDER_KEYS)
        if lender_conflict:
            return lender_conflict
        loan, loan_conflict = _unique_field(mapping, _LOAN_KEYS)
        if loan_conflict:
            return loan_conflict
        if lender or loan:
            if not lender or not loan:
                return (
                    f"{REASON_NO_INVENT} (have "
                    f"{'lender' if lender else 'no lender'}, "
                    f"{'loan' if loan else 'no loan'})"
                )
            mortgages.append(
                MortgageRecord(
                    lender_name=lender,
                    loan_number=loan,
                    source=source,
                    document_ids=(doc_id,) if doc_id else (),
                )
            )
        return ""

    def _walk(node: Any, doc_id: str) -> str:
        if isinstance(node, dict):
            # Document rows: keep their own id for read-back, never treat
            # the document title as a lender name (extract_structured uses
            # only the allowlisted keys; name/title are not in that set).
            nested_id = doc_id
            raw_id = node.get("id")
            if raw_id is not None and str(raw_id).strip().isdigit():
                nested_id = str(raw_id).strip()
            conflict = _from_mapping(node, nested_id)
            if conflict:
                return conflict
            for key, raw in node.items():
                if _fold_key(key) in _ENVELOPE_KEYS:
                    conflict = _walk(raw, nested_id)
                    if conflict:
                        return conflict
            return ""
        if isinstance(node, list):
            for item in node:
                conflict = _walk(item, doc_id)
                if conflict:
                    return conflict
        return ""

    conflict = _walk(payload, document_id)
    return mortgages, conflict, saw_explicit


def _document_search_rows(payload: Any) -> list[dict[str, Any]]:
    try:
        from .ezlynx_api import extract_document_api_results
    except ImportError:  # pragma: no cover - script-style import
        from ezlynx_api import extract_document_api_results  # type: ignore

    # Keep full metadata for structured field extraction; ids come from
    # the proven helper so documentUrl is never an identifier.
    proven = {row["id"]: row["name"] for row in extract_document_api_results(payload)}
    raw_rows: list[Any]
    if isinstance(payload, list):
        raw_rows = payload
    elif isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list):
            raw_rows = results
        else:
            data = payload.get("data")
            if isinstance(data, dict) and isinstance(data.get("results"), list):
                raw_rows = data["results"]
            elif isinstance(data, list):
                raw_rows = data
            else:
                raw_rows = []
    else:
        raw_rows = []
    out: list[dict[str, Any]] = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        raw_id = row.get("id")
        document_id = str(raw_id).strip() if raw_id is not None else ""
        if document_id not in proven:
            continue
        merged = dict(row)
        merged["id"] = document_id
        merged.setdefault("name", proven[document_id])
        out.append(merged)
    return out


def _declaration_docs_from_search(payload: Any) -> list[DeclarationDoc]:
    docs: list[DeclarationDoc] = []
    for row in _document_search_rows(payload):
        if not looks_like_declaration(row):
            continue
        docs.append(
            DeclarationDoc(
                document_id=str(row["id"]),
                name=_clean_text(row.get("name") or row.get("documentName") or ""),
                read_back=True,
            )
        )
    return docs


def _document_ids_present(payload: Any, ids: list[str]) -> bool:
    seen = {row["id"] for row in _document_search_rows(payload)}
    return all(doc_id in seen for doc_id in ids)


# ---------------------------------------------------------------------------
# Source reads
# ---------------------------------------------------------------------------


def _snapshot(name: str, **kwargs: Any) -> SourceSnapshot:
    return SourceSnapshot(name=name, **kwargs)


def read_policy_source(
    ports: EnrichmentPorts,
    policy_number: str,
) -> SourceSnapshot:
    number = _clean_text(policy_number)
    if not number:
        return _snapshot(
            "policy", available=False, explicit_empty=False,
            error="policy number missing — cannot look up structured fields",
        )
    if ports.policy is None:
        return _snapshot(
            "policy", available=False, explicit_empty=False,
            error="no PolicyApi client bound (dry-run scaffold)",
        )
    try:
        payload = ports.policy.search_policy_by_number(number)
    except Exception as exc:
        return _snapshot(
            "policy", available=False, explicit_empty=False,
            error=f"PolicyApi search failed: {type(exc).__name__}: {exc}",
        )
    try:
        mortgages, conflict, saw = extract_structured_mortgages(payload, source="policy")
    except MortgageeEnrichmentError as exc:
        return _snapshot(
            "policy", available=True, explicit_empty=False,
            conflict=str(exc), error=str(exc),
            read_back={"search": "PolicyApi/policy/v1/search", "policy_number": number},
        )
    return _snapshot(
        "policy",
        available=True,
        explicit_empty=bool(saw and not mortgages and not conflict),
        mortgages=mortgages,
        conflict=conflict,
        read_back={
            "search": "PolicyApi/policy/v1/search",
            "policy_number": number,
            "explicit_collection": saw,
            "mortgage_count": len(mortgages),
        },
    )


def read_declaration_source(
    ports: EnrichmentPorts,
    applicant_id: str,
) -> SourceSnapshot:
    applicant = _clean_text(applicant_id)
    if not applicant:
        return _snapshot(
            "declaration", available=False, explicit_empty=False,
            error="applicant id missing — cannot search DocumentApi",
        )
    if ports.documents is None:
        return _snapshot(
            "declaration", available=False, explicit_empty=False,
            error="no DocumentApi client bound (dry-run scaffold)",
        )
    try:
        payload = ports.documents.search_applicant_documents(applicant)
    except Exception as exc:
        return _snapshot(
            "declaration", available=False, explicit_empty=False,
            error=f"DocumentApi search failed: {type(exc).__name__}: {exc}",
        )
    docs = _declaration_docs_from_search(payload)
    # Structured mortgages come only from unique fields on the declaration
    # rows (or a top-level mortgagees collection). PDF bytes are never read.
    try:
        mortgages, conflict, saw = extract_structured_mortgages(
            {"results": [row for row in _document_search_rows(payload)
                         if looks_like_declaration(row)]},
            source="declaration",
        )
        # A top-level explicit collection on the search envelope also counts.
        extra, extra_conflict, extra_saw = extract_structured_mortgages(
            payload, source="declaration",
        )
    except MortgageeEnrichmentError as exc:
        return _snapshot(
            "declaration", available=True, explicit_empty=False,
            declaration_docs=docs, conflict=str(exc), error=str(exc),
            read_back={
                "search": "DocumentApi/documents/v1/account/{id}/document-search",
                "applicant_id": applicant,
                "declaration_count": len(docs),
            },
        )
    if extra_conflict and not conflict:
        conflict = extra_conflict
    # Prefer mortgages attached to declaration docs; envelope-level records
    # are included only when they are not already represented.
    seen = {(normalize_loan_number(m.loan_number), normalize_lender_name(m.lender_name))
            for m in mortgages}
    for item in extra:
        key = (normalize_loan_number(item.loan_number), normalize_lender_name(item.lender_name))
        if key not in seen:
            mortgages.append(item)
            seen.add(key)
    saw_any = saw or extra_saw
    return _snapshot(
        "declaration",
        available=True,
        explicit_empty=bool(saw_any and not mortgages and not conflict),
        mortgages=mortgages,
        declaration_docs=docs,
        conflict=conflict,
        read_back={
            "search": "DocumentApi/documents/v1/account/{id}/document-search",
            "applicant_id": applicant,
            "declaration_count": len(docs),
            "document_ids": [d.document_id for d in docs],
            "explicit_collection": saw_any,
            "mortgage_count": len(mortgages),
            "read_back": True,
        },
    )


# ---------------------------------------------------------------------------
# Reconcile — never pick a side
# ---------------------------------------------------------------------------


def _index_by_loan(records: list[MortgageRecord]) -> dict[str, list[MortgageRecord]]:
    index: dict[str, list[MortgageRecord]] = {}
    for record in records:
        index.setdefault(normalize_loan_number(record.loan_number), []).append(record)
    return index


def reconcile_sources(
    policy: SourceSnapshot,
    declaration: SourceSnapshot,
) -> tuple[str, str, list[MortgageRecord]]:
    """Compare structured sources. Conflict → HITL, never a guessed winner."""
    if policy.conflict:
        return STATUS_HITL, policy.conflict, []
    if declaration.conflict:
        return STATUS_HITL, declaration.conflict, []

    policy_ok = policy.available
    dec_ok = declaration.available

    if not policy_ok and not dec_ok:
        return STATUS_INCOMPLETE, REASON_LENDER_NOT_ON_FILE, []

    # Both succeeded with explicit empty collections → proven zero.
    if policy_ok and dec_ok and policy.explicit_empty and declaration.explicit_empty:
        return STATUS_PROVEN_ZERO, REASON_PROVEN_ZERO, []

    # One side proven empty, the other has mortgages → they disagree.
    if policy_ok and declaration.available:
        if policy.explicit_empty and declaration.mortgages:
            return STATUS_HITL, REASON_HITL_CONFLICT, []
        if declaration.explicit_empty and policy.mortgages:
            return STATUS_HITL, REASON_HITL_CONFLICT, []

    if policy.mortgages and declaration.mortgages:
        agreed: list[MortgageRecord] = []
        policy_idx = _index_by_loan(policy.mortgages)
        dec_idx = _index_by_loan(declaration.mortgages)
        if set(policy_idx) != set(dec_idx):
            return STATUS_HITL, REASON_HITL_CONFLICT, []
        for loan, policy_rows in policy_idx.items():
            dec_rows = dec_idx[loan]
            policy_lenders = {normalize_lender_name(r.lender_name) for r in policy_rows}
            dec_lenders = {normalize_lender_name(r.lender_name) for r in dec_rows}
            if policy_lenders != dec_lenders:
                return STATUS_HITL, REASON_HITL_CONFLICT, []
            if len(policy_rows) != 1 or len(dec_rows) != 1:
                return STATUS_HITL, (
                    "HITL: duplicate loan numbers across mortgages — "
                    "not picking a side"
                ), []
            left, right = policy_rows[0], dec_rows[0]
            agreed.append(
                MortgageRecord(
                    lender_name=left.lender_name,
                    loan_number=left.loan_number,
                    source="agreed",
                    document_ids=tuple(
                        dict.fromkeys((*left.document_ids, *right.document_ids))
                    ),
                )
            )
        return STATUS_READY, (
            f"enriched {len(agreed)} mortgage(s) — policy and declaration "
            "structured fields agree"
        ), agreed

    # Only one source produced mortgages. The other did not prove empty
    # (missing collection key) — fail closed, do not invent agreement.
    if policy.mortgages and policy_ok and not dec_ok:
        return STATUS_INCOMPLETE, (
            f"{REASON_LENDER_NOT_ON_FILE} (policy fields present; "
            f"DocumentApi not confirmed: {declaration.error or 'unbound'})"
        ), []
    if declaration.mortgages and dec_ok and not policy_ok:
        return STATUS_INCOMPLETE, (
            f"{REASON_LENDER_NOT_ON_FILE} (declaration fields present; "
            f"PolicyApi not confirmed: {policy.error or 'unbound'})"
        ), []
    if policy.mortgages and policy_ok and dec_ok and not declaration.explicit_empty:
        # Dec search ran but had no structured mortgage fields (PDF only).
        # Do not treat the PDF as a source and do not silently accept policy.
        return STATUS_INCOMPLETE, (
            f"{REASON_NO_INVENT} (declaration docs listed; no unique "
            "structured mortgagee fields on those docs — PDF not scraped)"
        ), []
    if declaration.mortgages and dec_ok and policy_ok and not policy.explicit_empty:
        return STATUS_INCOMPLETE, (
            f"{REASON_NO_INVENT} (declaration structured fields present; "
            "policy payload had no explicit mortgage collection)"
        ), []

    if policy_ok and dec_ok and not policy.mortgages and not declaration.mortgages:
        # Both reads succeeded but neither proved an empty collection.
        return STATUS_INCOMPLETE, (
            f"{REASON_LENDER_NOT_ON_FILE} (lookups succeeded; no explicit "
            "empty mortgage collection — will not treat as zero)"
        ), []

    return STATUS_INCOMPLETE, REASON_LENDER_NOT_ON_FILE, []


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def enrich_work_item(
    *,
    policy_number: str,
    applicant_id: str = "",
    row: dict[str, Any] | None = None,
    ports: EnrichmentPorts | None = None,
    dry_run: bool = True,
) -> EnrichmentResult:
    """Enrich one open 4372 work item. Default is dry-run (no live writes)."""
    del row  # reserved: future row-level structured fields; never SSN.
    bound = ports or EnrichmentPorts()
    policy = read_policy_source(bound, policy_number)
    declaration = read_declaration_source(bound, applicant_id)
    status, reason, mortgages = reconcile_sources(policy, declaration)
    result = EnrichmentResult(
        policy_number=_clean_text(policy_number),
        applicant_id=_clean_text(applicant_id),
        status=status,
        reason=reason,
        dry_run=bool(dry_run),
        mortgages=mortgages,
        declaration_docs=list(declaration.declaration_docs),
        policy_snapshot={
            "available": policy.available,
            "explicit_empty": policy.explicit_empty,
            "mortgage_count": len(policy.mortgages),
            "conflict": policy.conflict,
            "error": policy.error,
            "read_back": policy.read_back,
        },
        declaration_snapshot={
            "available": declaration.available,
            "explicit_empty": declaration.explicit_empty,
            "mortgage_count": len(declaration.mortgages),
            "conflict": declaration.conflict,
            "error": declaration.error,
            "read_back": declaration.read_back,
        },
    )
    result.to_dict()  # fail closed if a forbidden key slipped in
    return result


def plan_from_enrichment(
    result: EnrichmentResult,
    *,
    due_txt: str,
) -> tuple[str, str, str, str, str]:
    """Map enrichment to planner (kind, detail, target, status, reason)."""
    if result.status == STATUS_HITL:
        return (
            "verify",
            f"{result.reason} ({due_txt}). Do not deliver.",
            "HITL",
            "blocked",
            result.reason,
        )
    if result.status == STATUS_PROVEN_ZERO:
        return (
            "wait",
            f"{REASON_PROVEN_ZERO}; no lender delivery ({due_txt})",
            "",
            "waiting",
            REASON_PROVEN_ZERO,
        )
    if result.status == STATUS_READY and result.mortgages:
        lines = []
        for index, mortgage in enumerate(result.mortgages, start=1):
            lines.append(
                f"mortgage {index}: lender={mortgage.lender_name!r} "
                f"loan={mortgage.loan_number!r}"
            )
        detail = (
            "Structured enrichment ready — handle each mortgage separately: "
            + "; ".join(lines)
            + ". Portal / Bland delivery is out of this scaffold "
            f"(never SSN). ({due_txt})"
        )
        return (
            "verify",
            detail,
            result.mortgages[0].lender_name,
            "blocked",
            "enriched; portal/Bland delivery not in this scaffold",
        )
    return (
        "verify",
        "Lender name and loan number are NOT in the export — "
        "DocumentApi / PolicyApi structured enrichment did not produce a "
        "unique match (never invent; never scrape a PDF; never SSN). "
        f"({due_txt})",
        "lender TBD",
        "blocked",
        result.reason or REASON_LENDER_NOT_ON_FILE,
    )


# ---------------------------------------------------------------------------
# Notes / docs — API only, Playwright fail-closed
# ---------------------------------------------------------------------------


def refuse_playwright_enrichment_write(where: str = "mortgagee_enrichment") -> None:
    """Playwright must never file EZLynx notes or documents from this path."""
    try:
        from .ezlynx_api_only_writes import refuse_playwright_note_or_doc
    except ImportError:  # pragma: no cover
        from ezlynx_api_only_writes import refuse_playwright_note_or_doc  # type: ignore
    refuse_playwright_note_or_doc(where)


def file_enrichment_note(
    *,
    applicant_id: str,
    note_text: str,
    via: str,
    discussion_title: str | None = None,
    discussion_client: Any | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """File an enrichment note. Playwright/CDP is refused.

    Dry-run records intent only. A live file still requires DiscussionApi
    ``note_id`` read-back — this scaffold does not call live EZLynx.
    """
    channel = _clean_text(via).casefold()
    if channel in {"playwright", "cdp", "browser", "file_chooser", "add_note"}:
        refuse_playwright_enrichment_write(
            f"mortgagee_enrichment.file_enrichment_note via={via}"
        )
    if channel not in {"discussion_api", "api", "notes_api"}:
        raise MortgageeEnrichmentError(
            f"enrichment notes must use DiscussionApi (got via={via!r})"
        )
    if dry_run or discussion_client is None:
        return {
            "status": "dry_run",
            "applicant_id": _clean_text(applicant_id),
            "discussion_title": discussion_title or "",
            "note_text": note_text,
            "executed": False,
            "read_back": False,
        }
    try:
        from .ezlynx_api_only_writes import add_note_to_discussion
    except ImportError:  # pragma: no cover
        from ezlynx_api_only_writes import add_note_to_discussion  # type: ignore
    filed = add_note_to_discussion(
        applicant_id,
        note_text,
        discussion_title=discussion_title,
        discussion_client=discussion_client,
        dry_run=False,
    )
    if not str(filed.get("note_id") or filed.get("ezlynx_note_id") or "").strip():
        raise MortgageeEnrichmentError(
            "DiscussionApi note write requires a read-back note_id"
        )
    return filed
