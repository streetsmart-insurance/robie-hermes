"""Manual Renewals discussion targeting + COMPLETE gate.

Note targeting (LOB-agnostic, Benli HO 2026-09-07):

1. Prefer an existing titled ``{LOB} Renewal`` card (and close variants
   such as ``{LOB} Renewal Offer``) for that line of business.
2. Only create/use ``Manual {LOB} Renewal`` and ``Renewal Update {LOB}``
   when no matching existing LOB renewal discussion exists.
3. Never invent untitled discussions. Never match Email Automation /
   Automation Center. Every note still ends with ``Robie was here``.

Docs-only is not COMPLETE. Renewal Offer PDFs need the **Renewal Offer**
label and the Documents folder named **Renewal Offer**. Application /
Bound Quote / Renewal Application prints are never a Renewal Offer.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.ezlynx.api_client import DISQUALIFIED_DISCUSSION_PATTERNS

RENEWAL_OFFER_LABEL = "Renewal Offer"
RENEWAL_OFFER_FOLDER = "Renewal Offer"
STATUS_COMPLETE = "COMPLETE"
STATUS_PARTIAL = "PARTIAL"
STATUS_BLOCKED = "BLOCKED"

DOC_KIND_RENEWAL_OFFER = "renewal_offer"
DOC_KIND_APPLICATION = "application"
DOC_KIND_BOUND_QUOTE = "bound_quote"
DOC_KIND_UNKNOWN = "unknown"

# Bound Quote / Application prints (Benli HO QHONJ2026080215) are not offers.
_APPLICATION_DOC_HINTS = (
    "bound quote application",
    "bound quote",
    "renewal application",
    "quote application",
    "application print",
    "application for insurance",
    "acord application",
)
# True offer / dec / firmed quote — required for COMPLETE as Renewal Offer filed.
_RENEWAL_OFFER_DOC_HINTS = (
    "renewal offer",
    "renewal declaration",
    "declarations page",
    "declaration of insurance",
    "policy declarations",
    "policy declaration",
    "renewal dec",
    "dec pages",
    "dec page",
    "policy dec",
    "quote proposal (firmed)",
    "firmed quote",
    "firmed proposal",
    "renewal quote",
)
# Portal quote/application numbers like QHONJ2026080215 (not the policy HONJ…).
_BOUND_QUOTE_NUMBER_RE = re.compile(r"\bq[a-z]{2,8}\d{6,}\b", re.I)

# Canonical Documents-tab folder plus accepted existing aliases.
RENEWAL_OFFER_FOLDER_ALIASES = (
    "Renewal Offer",
    "Renewal Offers",
    "Renewal Offers/Declarations",
)

_MANUAL_LOB_RENEWAL_RE = re.compile(r"^manual .+ renewal$", re.I)
_RENEWAL_UPDATE_RE = re.compile(r"^renewal update .+$", re.I)

# Titles that caused the Paulette misfire (note 1123385828 → Email Automation).
AUTOMATION_TITLE_PATTERNS = (
    "email automation",
    "automation center",
    "email sent by automation",
)

_LOB_DISPLAY = {
    "homeowners": "Homeowners",
    "homeowner": "Homeowners",
    "ho": "Homeowners",
    "workers compensation": "Workers Comp",
    "workers comp": "Workers Comp",
    "worker's compensation": "Workers Comp",
    "wc": "Workers Comp",
    "general liability": "General Liability",
    "commercial general liability": "General Liability",
    "cgl": "General Liability",
    "gl": "General Liability",
    "commercial auto": "Commercial Auto",
    "ca": "Commercial Auto",
    "inland marine": "Inland Marine",
    "im": "Inland Marine",
    "business owners": "Business Owners",
    "bop": "Business Owners",
    "excess": "Excess",
    "umbrella": "Excess",
    "flood": "Flood",
    "commercial package": "Commercial Package",
    "dwelling fire": "Dwelling Fire",
    "dwelling": "Dwelling Fire",
    "dp": "Dwelling Fire",
    "employee practices liability": "Employee Practices Liability",
    "employment practices liability": "Employee Practices Liability",
    "empl practices liab": "Employee Practices Liability",
    "epli": "Employee Practices Liability",
    "cyber liability": "Cyber Liability",
    "cyber": "Cyber Liability",
    "errors and omissions": "Professional Liability",
    "e&o": "Professional Liability",
    "professional liability": "Professional Liability",
    "directors and officers": "Directors and Officers",
    "d&o": "Directors and Officers",
    "bonds": "Bonds",
    "surety": "Bonds",
    "bonds miscellaneous": "Bonds",
}

# Extra {LOB} Renewal title stems for the same line (never hardcoded to HO).
_LOB_RENEWAL_ALIASES = {
    "Homeowners": ("Homeowners", "Homeowner", "HO"),
    "Workers Comp": ("Workers Comp", "Workers Compensation", "Worker's Compensation", "WC"),
    "General Liability": ("General Liability", "Commercial General Liability", "CGL", "GL"),
    "Commercial Auto": ("Commercial Auto", "Commercial Automobile", "CA"),
    "Inland Marine": ("Inland Marine", "IM"),
    "Business Owners": ("Business Owners", "BOP"),
    "Excess": ("Excess", "Umbrella"),
    "Flood": ("Flood",),
    "Commercial Package": ("Commercial Package",),
    "Dwelling Fire": ("Dwelling Fire", "Dwelling", "DP"),
    "Employee Practices Liability": ("Employee Practices Liability", "Empl Practices Liab", "Employment Practices Liability", "EPLI"),
    "Cyber Liability": ("Cyber Liability", "Cyber"),
    "Professional Liability": ("Professional Liability", "Errors and Omissions", "E&O"),
    "Directors and Officers": ("Directors and Officers", "D&O"),
    "Bonds": ("Bonds", "Surety", "Bonds Miscellaneous"),
}


def _norm_title(title: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (title or "").strip()).lower()


def canonical_lob_display(line_of_business: Optional[str]) -> str:
    raw = re.sub(r"\s+", " ", (line_of_business or "").strip())
    if not raw:
        return ""
    mapped = _LOB_DISPLAY.get(raw.lower())
    if mapped:
        return mapped
    if raw.islower() or raw.isupper():
        return raw.title()
    return raw


def manual_lob_renewal_title(line_of_business: Optional[str]) -> str:
    display = canonical_lob_display(line_of_business)
    if not display:
        raise ValueError("line_of_business is required to build Manual {LOB} Renewal")
    return f"Manual {display} Renewal"


def renewal_update_lob_title(line_of_business: Optional[str]) -> str:
    display = canonical_lob_display(line_of_business)
    if not display:
        raise ValueError("line_of_business is required to build Renewal Update {LOB}")
    return f"Renewal Update {display}"


def candidate_manual_lob_titles(line_of_business: Optional[str]) -> List[str]:
    titles: List[str] = []
    display = canonical_lob_display(line_of_business)
    raw = re.sub(r"\s+", " ", (line_of_business or "").strip())
    if display:
        titles.append(f"Manual {display} Renewal")
    if raw and f"Manual {raw} Renewal" not in titles:
        titles.append(f"Manual {raw} Renewal")
    return titles


def candidate_renewal_update_titles(line_of_business: Optional[str]) -> List[str]:
    titles: List[str] = []
    display = canonical_lob_display(line_of_business)
    raw = re.sub(r"\s+", " ", (line_of_business or "").strip())
    if display:
        titles.append(f"Renewal Update {display}")
    if raw and f"Renewal Update {raw}" not in titles:
        titles.append(f"Renewal Update {raw}")
    return titles


def is_manual_lob_renewal_title(title: Optional[str]) -> bool:
    return bool(_MANUAL_LOB_RENEWAL_RE.match((title or "").strip()))


def is_renewal_update_title(title: Optional[str]) -> bool:
    return bool(_RENEWAL_UPDATE_RE.match((title or "").strip()))


def lob_renewal_aliases(line_of_business: Optional[str]) -> List[str]:
    """Display name plus known aliases for the same LOB. Order is stable."""
    display = canonical_lob_display(line_of_business)
    raw = re.sub(r"\s+", " ", (line_of_business or "").strip())
    aliases: List[str] = []
    for name in (display, raw):
        if name and name not in aliases:
            aliases.append(name)
    extras_key = display or raw
    for extra in _LOB_RENEWAL_ALIASES.get(extras_key, ()):
        if extra not in aliases:
            aliases.append(extra)
    if display:
        for key, mapped in _LOB_DISPLAY.items():
            if mapped != display:
                continue
            pretty = key.upper() if len(key) <= 3 else key.title()
            if pretty not in aliases:
                aliases.append(pretty)
    return aliases


def candidate_existing_lob_renewal_titles(line_of_business: Optional[str]) -> List[str]:
    """Exact and close-variant existing workflow titles: ``{LOB} Renewal``."""
    titles: List[str] = []
    for alias in lob_renewal_aliases(line_of_business):
        for variant in (f"{alias} Renewal", f"{alias} Renewal Offer", f"{alias} Manual Renewal"):
            if variant not in titles:
                titles.append(variant)
    return titles


def is_existing_lob_renewal_title(
    title: Optional[str],
    line_of_business: Optional[str] = None,
) -> bool:
    """True for ``{LOB} Renewal`` / close variants. Never Manual/Update/untitled."""
    if is_disqualified_discussion_title(title) or is_manual_lob_renewal_title(title):
        return False
    if is_renewal_update_title(title):
        return False
    t_norm = _norm_title(title)
    if not t_norm:
        return False
    aliases = lob_renewal_aliases(line_of_business) if line_of_business else None
    if aliases:
        for alias in aliases:
            stem = _norm_title(f"{alias} Renewal")
            manual_stem = _norm_title(f"{alias} Manual Renewal")
            if not stem:
                continue
            if t_norm == stem or t_norm == f"{stem} offer" or (manual_stem and t_norm == manual_stem):
                return True
            if t_norm.startswith(f"{stem} ") or t_norm.startswith(f"{stem}/") or t_norm.startswith(f"{stem}("):
                rest = t_norm[len(stem):].strip()
                if rest.startswith("offer"):
                    return True
                if rest.startswith("(") or rest.startswith("/"):
                    return True
                if rest[:4].isdigit():
                    return True
            if manual_stem and (t_norm.startswith(f"{manual_stem} ") or t_norm.startswith(f"{manual_stem}(")):
                return True
        return False
    return bool(re.match(r"^.+ renewal( offer)?$", t_norm))


def is_renewal_offer_folder(name: Optional[str]) -> bool:
    n = _norm_title(name)
    if not n:
        return False
    return n in {_norm_title(alias) for alias in RENEWAL_OFFER_FOLDER_ALIASES}


def resolve_renewal_offer_folder(
    existing_folders: Sequence[str],
    *,
    create_if_missing: bool = True,
) -> "FolderResolveResult":
    """Select the Documents folder named Renewal Offer; create it if missing.

    Policy-number folders (e.g. HONJ2025100027) are never treated as the
    destination. Existing aliases such as ``Renewal Offers`` still match.
    """
    preferred = list(RENEWAL_OFFER_FOLDER_ALIASES)
    found = None
    for want in preferred:
        for raw in existing_folders:
            if titles_match_exact(raw, want):
                found = (raw or "").strip()
                break
        if found:
            break
    if found:
        return FolderResolveResult(folder=found, created=False, action="matched")
    if not create_if_missing:
        raise ValueError(f"Missing Documents folder '{RENEWAL_OFFER_FOLDER}'")
    return FolderResolveResult(folder=RENEWAL_OFFER_FOLDER, created=True, action="create")


def _doc_haystack(*parts: Optional[str]) -> str:
    return " ".join(re.sub(r"\s+", " ", (p or "").replace("_", " ").strip()) for p in parts if p).strip()


def classify_renewal_document(
    *,
    name: Optional[str] = None,
    text: Optional[str] = None,
    kind: Optional[str] = None,
) -> str:
    """Classify a portal/library PDF. Application / Bound Quote never wins as offer.

    Filename labels like ``HONJ… Renewal Offer.pdf`` are not enough when the
    print is a Bound Quote Application (Benli ``QHONJ2026080215``).
    """
    explicit = (kind or "").strip().lower().replace(" ", "_")
    hay = _norm_title(_doc_haystack(name, text, kind))
    if any(hint in hay for hint in _APPLICATION_DOC_HINTS) or _BOUND_QUOTE_NUMBER_RE.search(hay):
        if "bound quote" in hay or _BOUND_QUOTE_NUMBER_RE.search(hay):
            return DOC_KIND_BOUND_QUOTE
        return DOC_KIND_APPLICATION
    if explicit in {DOC_KIND_APPLICATION, DOC_KIND_BOUND_QUOTE}:
        return explicit
    if explicit == DOC_KIND_RENEWAL_OFFER:
        return DOC_KIND_RENEWAL_OFFER
    if any(hint in hay for hint in _RENEWAL_OFFER_DOC_HINTS):
        return DOC_KIND_RENEWAL_OFFER
    if explicit == DOC_KIND_UNKNOWN:
        return DOC_KIND_UNKNOWN
    return DOC_KIND_UNKNOWN


def is_true_renewal_offer_document(
    *,
    name: Optional[str] = None,
    text: Optional[str] = None,
    kind: Optional[str] = None,
) -> bool:
    return classify_renewal_document(name=name, text=text, kind=kind) == DOC_KIND_RENEWAL_OFFER


def is_application_or_bound_quote_document(
    *,
    name: Optional[str] = None,
    text: Optional[str] = None,
    kind: Optional[str] = None,
) -> bool:
    return classify_renewal_document(name=name, text=text, kind=kind) in {
        DOC_KIND_APPLICATION,
        DOC_KIND_BOUND_QUOTE,
    }


def peek_pdf_text(path: Optional[Any], *, limit: int = 4000, pages: int = 2) -> str:
    """Best-effort first-page text for classification. Never invents content."""
    if path is None:
        return ""
    try:
        pdf_path = Path(path)
    except TypeError:
        return ""
    if not pdf_path.is_file():
        return ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        chunks: List[str] = []
        for page in reader.pages[: max(1, pages)]:
            chunks.append(page.extract_text() or "")
        return " ".join(chunks)[:limit]
    except Exception:
        return ""


def is_untitled_discussion_title(title: Optional[str]) -> bool:
    cleaned = (title or "").strip()
    return not cleaned or cleaned.lower() in {"untitled", "(untitled)", "new discussion"}


def is_automation_discussion_title(title: Optional[str]) -> bool:
    t_low = _norm_title(title)
    if not t_low:
        return False
    if any(pat in t_low for pat in AUTOMATION_TITLE_PATTERNS):
        return True
    if t_low == "automation" or t_low.startswith("automation "):
        return True
    return False


def is_disqualified_discussion_title(title: Optional[str]) -> bool:
    t_low = _norm_title(title)
    if is_automation_discussion_title(title) or is_untitled_discussion_title(title):
        return True
    return any(pat in t_low for pat in DISQUALIFIED_DISCUSSION_PATTERNS)


def extract_cli_discussion_id(record: Optional[Dict[str, Any]]) -> Optional[str]:
    """CLI-parity id only. Never generic ``id`` / ``Id`` (Board-IN stale-id lesson)."""
    if not isinstance(record, dict):
        return None
    for key in ("discussionId", "DiscussionId"):
        val = record.get(key)
        if val is None or str(val).strip() == "":
            continue
        return str(val).strip()
    return None


def titles_match_exact(left: Optional[str], right: Optional[str]) -> bool:
    a, b = _norm_title(left), _norm_title(right)
    return bool(a) and a == b


def activity_card_matches_exact_title(card_title: Optional[str], want: Optional[str]) -> bool:
    """DOM / API card match: exact title only. Never substring / first-card."""
    if is_disqualified_discussion_title(card_title):
        return False
    return titles_match_exact(card_title, want)


def find_exact_titled_discussion(
    discussions: Sequence[Dict[str, Any]],
    want_title: str,
) -> Optional[Dict[str, Any]]:
    """Return the card whose title equals ``want_title`` (normalized). Never automation."""
    if is_disqualified_discussion_title(want_title):
        return None
    for d in discussions:
        title = (d.get("title") or d.get("Title") or "").strip()
        if is_disqualified_discussion_title(title):
            continue
        if titles_match_exact(title, want_title):
            discussion_id = extract_cli_discussion_id(d)
            return {
                "discussionId": discussion_id,
                "title": title,
                "raw": d,
            }
    return None


def find_exact_titled_discussion_any(
    discussions: Sequence[Dict[str, Any]],
    want_titles: Iterable[str],
) -> Optional[Dict[str, Any]]:
    for want in want_titles:
        found = find_exact_titled_discussion(discussions, want)
        if found:
            return found
    return None


@dataclass
class DiscussionResolveResult:
    title: str
    discussion_id: Optional[str] = None
    created: bool = False
    action: str = "matched"
    kind: str = "fallback"  # existing_lob_renewal | manual_lob | renewal_update

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FolderResolveResult:
    folder: str
    created: bool = False
    action: str = "matched"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RenewalNoteTargets:
    """Cards to receive the shell note. Existing {LOB} Renewal wins when present."""

    targets: List[DiscussionResolveResult] = field(default_factory=list)
    used_existing_lob_renewal: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "targets": [t.to_dict() for t in self.targets],
            "used_existing_lob_renewal": self.used_existing_lob_renewal,
        }


def resolve_manual_lob_discussion(
    discussions: Sequence[Dict[str, Any]],
    line_of_business: Optional[str],
    *,
    create_if_missing: bool = True,
) -> DiscussionResolveResult:
    """Target exact ``Manual {LOB} Renewal``. Create that title if missing.

    Never returns Email Automation / Automation Center / untitled.
    """
    want = manual_lob_renewal_title(line_of_business)
    if is_disqualified_discussion_title(want):
        raise ValueError(f"Refusing disqualified Manual LOB title '{want}'")
    found = find_exact_titled_discussion_any(
        discussions, candidate_manual_lob_titles(line_of_business)
    )
    if found:
        title = found["title"]
        if is_disqualified_discussion_title(title):
            raise ValueError(f"Refusing disqualified discussion '{title}'")
        return DiscussionResolveResult(
            title=title,
            discussion_id=found.get("discussionId"),
            created=False,
            action="matched",
            kind="manual_lob",
        )
    if not create_if_missing:
        raise ValueError(f"Missing exact titled discussion '{want}'")
    return DiscussionResolveResult(
        title=want,
        discussion_id=None,
        created=True,
        action="create",
        kind="manual_lob",
    )


def resolve_renewal_update_discussion(
    discussions: Sequence[Dict[str, Any]],
    line_of_business: Optional[str],
    *,
    create_if_missing: bool = True,
) -> DiscussionResolveResult:
    """Target exact ``Renewal Update {LOB}``. Create if missing (notes on both)."""
    want = renewal_update_lob_title(line_of_business)
    if is_disqualified_discussion_title(want):
        raise ValueError(f"Refusing disqualified Renewal Update title '{want}'")
    found = find_exact_titled_discussion_any(
        discussions, candidate_renewal_update_titles(line_of_business)
    )
    if found:
        title = found["title"]
        if is_disqualified_discussion_title(title):
            raise ValueError(f"Refusing disqualified discussion '{title}'")
        return DiscussionResolveResult(
            title=title,
            discussion_id=found.get("discussionId"),
            created=False,
            action="matched",
            kind="renewal_update",
        )
    if not create_if_missing:
        raise ValueError(f"Missing exact titled discussion '{want}'")
    return DiscussionResolveResult(
        title=want,
        discussion_id=None,
        created=True,
        action="create",
        kind="renewal_update",
    )


def resolve_existing_lob_renewal_discussion(
    discussions: Sequence[Dict[str, Any]],
    line_of_business: Optional[str],
) -> Optional[DiscussionResolveResult]:
    """Return the existing ``{LOB} Renewal`` card when present. Never creates."""
    if not (line_of_business or "").strip():
        return None
    found = find_exact_titled_discussion_any(
        discussions, candidate_existing_lob_renewal_titles(line_of_business)
    )
    if not found:
        for d in discussions:
            title = (d.get("title") or d.get("Title") or "").strip()
            if is_existing_lob_renewal_title(title, line_of_business):
                found = {
                    "discussionId": extract_cli_discussion_id(d),
                    "title": title,
                    "raw": d,
                }
                break
    if not found:
        return None
    title = found["title"]
    if is_disqualified_discussion_title(title) or is_manual_lob_renewal_title(title):
        return None
    if is_renewal_update_title(title):
        return None
    return DiscussionResolveResult(
        title=title,
        discussion_id=found.get("discussionId"),
        created=False,
        action="matched",
        kind="existing_lob_renewal",
    )


def resolve_manual_renewal_note_targets(
    discussions: Sequence[Dict[str, Any]],
    line_of_business: Optional[str],
    *,
    create_if_missing: bool = True,
) -> RenewalNoteTargets:
    """Prefer existing ``{LOB} Renewal``; else Manual + Renewal Update fallbacks."""
    existing = resolve_existing_lob_renewal_discussion(discussions, line_of_business)
    if existing:
        return RenewalNoteTargets(targets=[existing], used_existing_lob_renewal=True)
    manual = resolve_manual_lob_discussion(
        discussions, line_of_business, create_if_missing=create_if_missing
    )
    manual.kind = "manual_lob"
    update = resolve_renewal_update_discussion(
        discussions, line_of_business, create_if_missing=create_if_missing
    )
    update.kind = "renewal_update"
    return RenewalNoteTargets(targets=[manual, update], used_existing_lob_renewal=False)


def verify_discussion_id_and_title(
    discussions: Sequence[Dict[str, Any]],
    want_title: str,
    *,
    expected_discussion_id: Optional[str] = None,
) -> Dict[str, Any]:
    """COMPLETE gate: discussionId + exact title must both match."""
    found = find_exact_titled_discussion(discussions, want_title)
    if not found:
        return {
            "verified": False,
            "discussion_id": None,
            "title": None,
            "reason": "exact_title_not_found",
        }
    found_id = found.get("discussionId")
    if expected_discussion_id and str(found_id or "") != str(expected_discussion_id):
        return {
            "verified": False,
            "discussion_id": found_id,
            "title": found.get("title"),
            "reason": "discussion_id_mismatch",
        }
    if is_disqualified_discussion_title(found.get("title")):
        return {
            "verified": False,
            "discussion_id": found_id,
            "title": found.get("title"),
            "reason": "disqualified_title",
        }
    return {
        "verified": True,
        "discussion_id": found_id,
        "title": found.get("title"),
        "reason": None,
    }


def _premiums_match(left: Any, right: Any) -> bool:
    from src.ezlynx.policy_renewer import premiums_match

    return premiums_match(left, right)


def _fmt_premium(value: Any) -> str:
    from src.ezlynx.policy_renewer import parse_money

    parsed = parse_money(value)
    if parsed is None:
        return "missing"
    return f"${parsed:,.2f}"


def _policy_total_from_evidence(evidence: "DoneChecklistEvidence") -> Any:
    """Authoritative COMPLETE premium is Policy Total from the PDF text.

    Re-parse ``firmed_pdf_text`` so a Coverage A extract stored as
    ``pdf_premium`` cannot COMPLETE when the same page has a higher
    Policy Total (Benli HONJ2025100027-26 / $1,116 vs $1,348).
    """
    text = (evidence.firmed_pdf_text or "").strip()
    if text:
        from src.extractor.quote_parser import extract_policy_total_premium

        from_text = extract_policy_total_premium(text)
        if from_text is not None:
            return from_text
    return evidence.pdf_premium


@dataclass
class DoneChecklistEvidence:
    firmed_pdf_uploaded: bool = False
    firmed_pdf_label: Optional[str] = None
    firmed_pdf_folder: Optional[str] = None
    firmed_pdf_kind: Optional[str] = None
    firmed_pdf_name: Optional[str] = None
    firmed_pdf_text: Optional[str] = None
    pdf_premium: Optional[Any] = None
    keyed_premium: Optional[Any] = None
    pending_rwl_count: int = 0
    bound: bool = False
    used_existing_lob_renewal: bool = False
    existing_lob_renewal_note_posted: bool = False
    existing_lob_renewal_discussion_id: Optional[str] = None
    existing_lob_renewal_title: Optional[str] = None
    existing_lob_renewal_title_expected: Optional[str] = None
    manual_lob_note_posted: bool = False
    manual_lob_discussion_id: Optional[str] = None
    manual_lob_title: Optional[str] = None
    manual_lob_title_expected: Optional[str] = None
    renewal_update_note_posted: bool = False
    renewal_update_discussion_id: Optional[str] = None
    renewal_update_title: Optional[str] = None
    renewal_update_title_expected: Optional[str] = None


@dataclass
class DoneChecklistResult:
    status: str
    complete: bool
    missing: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def evaluate_done_checklist(evidence: DoneChecklistEvidence) -> DoneChecklistResult:
    """Gate COMPLETE. Docs-only or a missing Manual LOB note is PARTIAL, never SUCCESS."""
    missing: List[str] = []
    reasons: List[str] = []

    if (
        is_automation_discussion_title(evidence.manual_lob_title)
        or is_automation_discussion_title(evidence.renewal_update_title)
        or is_automation_discussion_title(evidence.existing_lob_renewal_title)
    ):
        return DoneChecklistResult(
            status=STATUS_BLOCKED,
            complete=False,
            missing=["automation_discussion"],
            reasons=["Refusing Email Automation / Automation Center (Paulette misfire)"],
        )

    if evidence.bound:
        missing.append("bind_must_be_false")
        reasons.append("bind must stay false")

    if not evidence.firmed_pdf_uploaded:
        missing.append("firmed_pdf_uploaded")
        reasons.append("firmed PDF not uploaded")
    if (evidence.firmed_pdf_label or "").strip() != RENEWAL_OFFER_LABEL:
        missing.append("renewal_offer_label")
        reasons.append(f"document label must be '{RENEWAL_OFFER_LABEL}'")
    if not is_renewal_offer_folder(evidence.firmed_pdf_folder):
        missing.append("renewal_offer_folder")
        reasons.append(f"document must be in the '{RENEWAL_OFFER_FOLDER}' folder")

    classified = classify_renewal_document(
        name=evidence.firmed_pdf_name,
        text=evidence.firmed_pdf_text,
        kind=evidence.firmed_pdf_kind,
    )
    if classified != DOC_KIND_RENEWAL_OFFER:
        missing.append("true_renewal_offer_pdf")
        reasons.append(
            "Application / Bound Quote / Renewal Application is not a Renewal Offer; "
            "HITL for a true offer, declaration, or firmed renewal quote"
        )

    policy_total = _policy_total_from_evidence(evidence)
    if not _premiums_match(policy_total, evidence.keyed_premium):
        missing.append("premium_match")
        reasons.append(
            "keyed RWL premium "
            f"{_fmt_premium(evidence.keyed_premium)} does not match "
            f"PDF Policy Total {_fmt_premium(policy_total)} "
            "(use Policy Total / Total Annual / Grand Total, not Coverage A)"
        )

    if evidence.pending_rwl_count != 1:
        missing.append("exactly_one_pending_rwl")
        reasons.append("exactly one pending RWL (bind=false) is required")

    if evidence.used_existing_lob_renewal:
        expected_existing = (
            evidence.existing_lob_renewal_title_expected or evidence.existing_lob_renewal_title
        )
        if not evidence.existing_lob_renewal_note_posted:
            missing.append("existing_lob_renewal_note")
            reasons.append("note missing on existing {LOB} Renewal")
        if not evidence.existing_lob_renewal_discussion_id:
            missing.append("existing_lob_renewal_discussion_id")
            reasons.append("existing {LOB} Renewal discussionId not verified")
        if (
            not titles_match_exact(evidence.existing_lob_renewal_title, expected_existing)
            or not is_existing_lob_renewal_title(evidence.existing_lob_renewal_title)
            or is_manual_lob_renewal_title(evidence.existing_lob_renewal_title)
            or is_renewal_update_title(evidence.existing_lob_renewal_title)
        ):
            missing.append("existing_lob_renewal_title")
            reasons.append("existing {LOB} Renewal title not verified")
    else:
        expected_manual = evidence.manual_lob_title_expected or evidence.manual_lob_title
        if not evidence.manual_lob_note_posted:
            missing.append("manual_lob_note")
            reasons.append("note missing on Manual {LOB} Renewal")
        if not evidence.manual_lob_discussion_id:
            missing.append("manual_lob_discussion_id")
            reasons.append("Manual {LOB} Renewal discussionId not verified")
        if not titles_match_exact(evidence.manual_lob_title, expected_manual) or not is_manual_lob_renewal_title(
            evidence.manual_lob_title
        ):
            missing.append("manual_lob_title")
            reasons.append("Manual {LOB} Renewal exact title not verified")

        expected_update = evidence.renewal_update_title_expected or evidence.renewal_update_title
        if not evidence.renewal_update_note_posted:
            missing.append("renewal_update_note")
            reasons.append("note missing on Renewal Update {LOB}")
        if not evidence.renewal_update_discussion_id:
            missing.append("renewal_update_discussion_id")
            reasons.append("Renewal Update {LOB} discussionId not verified")
        if not titles_match_exact(evidence.renewal_update_title, expected_update) or not is_renewal_update_title(
            evidence.renewal_update_title
        ):
            missing.append("renewal_update_title")
            reasons.append("Renewal Update {LOB} exact title not verified")

    # Deduplicate while preserving order
    seen = set()
    missing = [m for m in missing if not (m in seen or seen.add(m))]

    if evidence.bound:
        return DoneChecklistResult(
            status=STATUS_BLOCKED,
            complete=False,
            missing=missing,
            reasons=reasons,
        )
    if missing:
        return DoneChecklistResult(
            status=STATUS_PARTIAL,
            complete=False,
            missing=missing,
            reasons=reasons,
        )
    return DoneChecklistResult(status=STATUS_COMPLETE, complete=True, missing=[], reasons=[])
