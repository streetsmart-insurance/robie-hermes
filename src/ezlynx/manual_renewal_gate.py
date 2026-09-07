"""Paulette Fagone HO (2026-09-07) standing rules for Manual Renewals.

Exact titled discussions only:

* ``Manual {LOB} Renewal`` (create if missing; never untitled)
* ``Renewal Update {LOB}`` (Carlo: notes on both)

Never match Email Automation / Automation Center. Docs-only is not COMPLETE.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.ezlynx.api_client import DISQUALIFIED_DISCUSSION_PATTERNS

RENEWAL_OFFER_LABEL = "Renewal Offer"
STATUS_COMPLETE = "COMPLETE"
STATUS_PARTIAL = "PARTIAL"
STATUS_BLOCKED = "BLOCKED"

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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


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
        )
    if not create_if_missing:
        raise ValueError(f"Missing exact titled discussion '{want}'")
    return DiscussionResolveResult(
        title=want,
        discussion_id=None,
        created=True,
        action="create",
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
        )
    if not create_if_missing:
        raise ValueError(f"Missing exact titled discussion '{want}'")
    return DiscussionResolveResult(
        title=want,
        discussion_id=None,
        created=True,
        action="create",
    )


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


@dataclass
class DoneChecklistEvidence:
    firmed_pdf_uploaded: bool = False
    firmed_pdf_label: Optional[str] = None
    pdf_premium: Optional[Any] = None
    keyed_premium: Optional[Any] = None
    pending_rwl_count: int = 0
    bound: bool = False
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

    if is_automation_discussion_title(evidence.manual_lob_title) or is_automation_discussion_title(
        evidence.renewal_update_title
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

    if not _premiums_match(evidence.pdf_premium, evidence.keyed_premium):
        missing.append("premium_match")
        reasons.append("PDF/extract premium must match keyed shell premium")

    if evidence.pending_rwl_count != 1:
        missing.append("exactly_one_pending_rwl")
        reasons.append("exactly one pending RWL (bind=false) is required")

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
