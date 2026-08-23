from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class SubmissionRoute:
    business_stage: str
    submission_folder_required: bool
    document_destination: str
    note_destination: str
    workflow_discussion_candidates: tuple[str, ...]
    workflow_to_start: str | None
    selection_rule: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_submission_route(
    *,
    is_renewal: bool,
    is_manual_renewal: bool = False,
    is_high_risk: bool = False,
) -> SubmissionRoute:
    """Resolve StreetSmart's SOP-aware destination for a submission artifact.

    The explicit operating rule is stronger than a generic file-standard rule:
    anything belonging to a Submission Center submission is filed in that
    submission's own folder, whether the business is new or renewal.
    """
    if not is_renewal:
        return SubmissionRoute(
            business_stage="new_business",
            submission_folder_required=True,
            document_destination="exact Submission Center folder for this submission",
            note_destination="Submission Center activity discussion for this submission",
            workflow_discussion_candidates=("New Business", "Submission Center"),
            workflow_to_start=None,
            selection_rule="Use the existing discussion with meaningful activity; never create Untitled.",
        )

    candidates: list[str] = ["Renewals"]
    if is_high_risk:
        candidates.insert(0, "High Risk Renewal")
    if is_manual_renewal:
        candidates.insert(0, "Renewal Manual")
    candidates.append("Submission Center")
    return SubmissionRoute(
        business_stage="manual_renewal" if is_manual_renewal else "renewal",
        submission_folder_required=True,
        document_destination="exact Submission Center folder for this submission",
        note_destination="Submission Center activity discussion for this submission",
        workflow_discussion_candidates=tuple(candidates),
        workflow_to_start="High Risk Renewal" if is_high_risk else None,
        selection_rule=(
            "When renewal discussions conflict, use the existing candidate with meaningful "
            "activity (prefer the most recently active); never create Untitled. Submission "
            "upload notes still belong in the Submission Center activity discussion."
        ),
    )


def choose_active_discussion(
    discussions: Iterable[dict[str, Any]], candidates: Iterable[str]
) -> dict[str, Any] | None:
    """Choose an existing, meaningful discussion deterministically.

    A matching discussion with real activity outranks an empty title match.
    Recency breaks ties. Untitled discussions are never selected.
    """
    wanted = {value.casefold() for value in candidates}
    eligible = []
    for discussion in discussions:
        title = str(discussion.get("title") or "").strip()
        if not title or title.casefold() == "untitled" or title.casefold() not in wanted:
            continue
        activity_count = int(discussion.get("activity_count") or 0)
        meaningful = bool(discussion.get("has_meaningful_activity", activity_count > 0))
        eligible.append((meaningful, activity_count, str(discussion.get("last_activity_at") or ""), discussion))
    if not eligible:
        return None
    eligible.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return eligible[0][3]


def submission_verification_requirements(route: SubmissionRoute) -> dict[str, Any]:
    return {
        "fresh_navigation_required": True,
        "document": {
            "location": route.document_destination,
            "exact_submission_identity_required": True,
            "exact_filename_required": True,
        },
        "note": {
            "location": route.note_destination,
            "exact_text_required": True,
            "existing_named_discussion_required": True,
            "untitled_forbidden": True,
        },
        "workflow": {
            "required": route.workflow_to_start is not None,
            "name": route.workflow_to_start,
        },
    }
