"""Apply the exact EZLynx org label ``Ascend NOC`` via HTTP API.

This is the mailbox-notice path for cancellation emails. It is not
Playwright, does not create labels, does not guess among matches, and
never applies the bare name ``Cancellation``. Live voice dial is frozen
agency-wide; applying this label is only so existing email/text
automation can fire.

List + apply use the same OAuth Bearer token as PolicyApi / DiscussionApi
(``EzlynxApiClient``). Portal org-label routes live on the EZLynx origin.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .ezlynx_write_scope import require_allowed_ezlynx_write_applicant

# Character-for-character. Do not casefold, trim, or substitute "Cancellation".
ASCEND_NOC_LABEL = "Ascend NOC"
FORBIDDEN_LABELS = frozenset({"Cancellation", "cancellation", "CANCELATION", "Cancel"})

ORG_LABELS_LIST_PATH = "/EZLynxPortalAPI/Organizations/GetOrganizationLabels"
APPLICANT_LABELS_PATH = "/EZLynxPortalAPI/Applicants/{applicant_id}/OrganizationLabels"

LABEL_NOT_FOUND = "LABEL_NOT_FOUND"
LABEL_NOT_UNIQUE = "LABEL_NOT_UNIQUE"
LABEL_REFUSED = "LABEL_REFUSED"
LABEL_APPLY_FAILED = "LABEL_APPLY_FAILED"


class OrgLabelError(RuntimeError):
    """Fail-closed label selection or apply. Nothing was guessed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def label_name_of(record: dict[str, Any]) -> str:
    for key in ("name", "Name", "label", "Label", "labelName", "LabelName"):
        value = str(record.get(key) or "")
        if value:
            return value
    return ""


def label_id_of(record: dict[str, Any]) -> str:
    for key in ("id", "Id", "labelId", "LabelId", "organizationLabelId", "OrganizationLabelId"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return ""


def normalize_org_label_rows(parsed: Any) -> list[dict[str, Any]]:
    if isinstance(parsed, list):
        return [row for row in parsed if isinstance(row, dict)]
    if isinstance(parsed, dict):
        for key in ("labels", "Labels", "items", "Items", "data", "Data", "results", "Results"):
            candidate = parsed.get(key)
            if isinstance(candidate, list):
                return [row for row in candidate if isinstance(row, dict)]
        if label_name_of(parsed) or label_id_of(parsed):
            return [parsed]
    return []


def select_unique_org_label(
    labels: list[dict[str, Any]] | None,
    required_name: str = ASCEND_NOC_LABEL,
) -> dict[str, Any]:
    """Exactly one org label whose name equals ``required_name``, or raise.

    Comparison is character-for-character. ``Ascend noc`` / ``Ascend NOC ``
    / ``Cancellation`` do not match ``Ascend NOC``.
    """
    wanted = str(required_name or "")
    if wanted != ASCEND_NOC_LABEL or wanted in FORBIDDEN_LABELS:
        raise OrgLabelError(
            LABEL_REFUSED,
            f"refusing org label {wanted!r}; only {ASCEND_NOC_LABEL!r} is allowed",
        )
    rows = [row for row in (labels or []) if isinstance(row, dict)]
    matched = [row for row in rows if label_name_of(row) == wanted]
    if not matched:
        raise OrgLabelError(
            LABEL_NOT_FOUND,
            f"no organization label named exactly {wanted!r}",
        )
    if len(matched) > 1:
        raise OrgLabelError(
            LABEL_NOT_UNIQUE,
            f"{len(matched)} organization labels named exactly {wanted!r}; refusing to guess",
        )
    if not label_id_of(matched[0]):
        raise OrgLabelError(
            LABEL_NOT_UNIQUE,
            f"organization label {wanted!r} has no usable id; refusing to guess",
        )
    return matched[0]


def plan_exact_label(client: Any, required_name: str = ASCEND_NOC_LABEL) -> dict[str, str]:
    """Read-only unique lookup. Does not apply and does not create."""
    if not hasattr(client, "list_organization_labels"):
        raise OrgLabelError(
            LABEL_APPLY_FAILED,
            "EZLynx client cannot list organization labels",
        )
    try:
        rows = normalize_org_label_rows(client.list_organization_labels())
    except OrgLabelError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise OrgLabelError(
            LABEL_APPLY_FAILED, f"organization label list failed: {type(exc).__name__}"
        ) from exc
    record = select_unique_org_label(rows, required_name)
    return {
        "name": label_name_of(record),
        "id": label_id_of(record),
    }


def apply_planned_label(
    client: Any,
    applicant_id: str,
    plan: dict[str, str],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Apply a previously unique-resolved label to the applicant.

    Write-scope is enforced first. Dry-run validates and writes nothing.
    """
    applicant = require_allowed_ezlynx_write_applicant(applicant_id)
    name = str((plan or {}).get("name") or "")
    label_id = str((plan or {}).get("id") or "").strip()
    if name != ASCEND_NOC_LABEL or name in FORBIDDEN_LABELS:
        raise OrgLabelError(
            LABEL_REFUSED,
            f"refusing to apply org label {name!r}; only {ASCEND_NOC_LABEL!r} is allowed",
        )
    if not label_id:
        raise OrgLabelError(LABEL_NOT_UNIQUE, "planned label has no id; refusing to guess")
    result = {
        "status": "dry_run" if dry_run else "applied",
        "applicant_id": applicant,
        "label_name": name,
        "label_id": label_id,
        "method": "api",
    }
    if dry_run:
        return result
    if not hasattr(client, "apply_applicant_organization_label"):
        raise OrgLabelError(
            LABEL_APPLY_FAILED,
            "EZLynx client cannot apply organization labels",
        )
    try:
        client.apply_applicant_organization_label(applicant, label_id)
    except OrgLabelError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise OrgLabelError(
            LABEL_APPLY_FAILED, f"organization label apply failed: {type(exc).__name__}"
        ) from exc
    return result


def applicant_labels_path(applicant_id: str) -> str:
    return APPLICANT_LABELS_PATH.format(applicant_id=quote(str(applicant_id), safe=""))
