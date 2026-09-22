"""Tier A evidence fetchers: independent API read-backs of the destination.

Each fetcher re-reads the system of record fresh after the work -- never
from the executing agent's memory -- and returns plain observed values for
the generic comparison in :mod:`robie_job_engine.evidence`.

Every fetcher takes its I/O dependency as an injectable callable so tests
(and the box) can supply fakes without touching the network:

- policy:      ``search_fn(policy_number) -> search payload``
- discussions: ``list_fn(applicant_id) -> list of discussion dicts``
- documents:   ``search_fn(applicant_id) -> document search payload``

Any failure to reach the destination raises
:class:`robie_job_engine.evidence.EvidenceUnavailable`, which the evidence
check turns into NO_EVIDENCE. A fetcher never fabricates a value.

Tier B session rule (EZLynx caps at 2 concurrent browser sessions):
  Tier B "fresh page" evidence must NOT open a parallel browser session --
  it would compete with the executor for the same scarce slots. Tier B
  reads serialize through the EZLynx session lock: same slot, new tab,
  fresh navigation, independent extraction. "Fresh" means no shared
  in-memory state from the executor, not a separate login. And wherever a
  field is API-visible, prefer Tier A read-back -- the API consumes no
  browser session at all.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

from .evidence import EvidenceUnavailable
from .ezlynx_writers import _first_present, find_policy_record

try:  # document helpers live in the (WIP) API client module
    from .ezlynx_api import extract_document_api_results
except ImportError:  # pragma: no cover - defensive; module is present in-repo
    extract_document_api_results = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# PolicyApi read-back (proven: search_policy_by_number)
# ---------------------------------------------------------------------------

_POLICY_FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "policyNumber": ("PolicyNumber", "policyNumber", "policy_number", "PolicyNo"),
    "writtenPremium": (
        "WrittenPremium",
        "writtenPremium",
        "written_premium",
        "Premium",
        "premium",
    ),
    "fullTermPremium": ("FullTermPremium", "fullTermPremium", "full_term_premium"),
    "policyStatus": ("PolicyStatus", "policyStatus", "Status", "status"),
    "effectiveDate": ("EffectiveDate", "effectiveDate", "effective_date"),
    "expirationDate": ("ExpirationDate", "expirationDate", "expiration_date"),
    "carrier": ("Carrier", "carrier", "MasterCompany", "masterCompany"),
}


def fetch_policy_evidence(
    search_fn: Callable[[str], Any],
    policy_number: str,
    fields: list[str],
) -> dict[str, Any]:
    """Read a policy back from PolicyApi and extract the requested fields.

    Fields the record does not carry come back as None -- the comparison
    treats that as "not yet visible" (settle window) or MISMATCH, never as
    a silent skip.
    """

    number = str(policy_number or "").strip()
    if not number:
        raise EvidenceUnavailable("policy number is required for policy evidence")
    try:
        payload = search_fn(number)
    except EvidenceUnavailable:
        raise
    except Exception as exc:
        raise EvidenceUnavailable(f"PolicyApi search failed: {exc}") from exc

    record = find_policy_record(payload, number)
    if record is None:
        raise EvidenceUnavailable(
            f"policy {number} not found in PolicyApi search results"
        )

    observed: dict[str, Any] = {}
    for name in fields:
        keys = _POLICY_FIELD_KEYS.get(name, (name,))
        observed[name] = _first_present(record, keys)
    # NOTE: the PolicyApi has a known read defect where carrier comes back
    # as "0" (proven on the D01 read-back). Callers comparing the carrier
    # field should treat "0" as unreadable, not as a real value.
    return observed


# ---------------------------------------------------------------------------
# DiscussionApi read-back (host-only base; note bodies are NOT API-readable)
# ---------------------------------------------------------------------------

DISCUSSION_API_BASE = "https://app.ezlynx.com/DiscussionApi/"
# CRITICAL: derive nothing from the DocumentApi base URL -- that produces
# .../DocumentApi/DiscussionApi/ and 404s on every call.

_DISCUSSION_ID_KEYS = ("id", "discussionId", "DiscussionId", "discussionID")
_DISCUSSION_TITLE_KEYS = ("title", "Title", "discussionTitle", "DiscussionTitle")
_DISCUSSION_NOTE_COUNT_KEYS = ("noteCount", "NoteCount", "note_count")
_DISCUSSION_LAST_NOTE_KEYS = (
    "mostRecentNoteId",
    "MostRecentNoteId",
    "lastNoteId",
    "LastNoteId",
)


def make_discussion_list_reader(
    token_provider: Callable[[], str],
    *,
    base_url: str = DISCUSSION_API_BASE,
    timeout_seconds: int = 30,
) -> Callable[[str], list[dict[str, Any]]]:
    """Build the proven DiscussionApi list reader.

    GET ``v8/discussions/by-applicant?applicantId=`` on the host-only base.
    ``token_provider`` supplies a fresh OAuth bearer token.
    """

    def list_discussions(applicant_id: str) -> list[dict[str, Any]]:
        applicant = str(applicant_id or "").strip()
        if not applicant:
            raise EvidenceUnavailable("applicant id is required for discussion evidence")
        url = (
            base_url.rstrip("/")
            + "/v8/discussions/by-applicant?"
            + urllib.parse.urlencode({"applicantId": applicant})
        )
        try:
            token = token_provider()
            request = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except EvidenceUnavailable:
            raise
        except Exception as exc:
            raise EvidenceUnavailable(
                f"DiscussionApi list failed for applicant {applicant}: {exc}"
            ) from exc
        if isinstance(payload, dict):
            for key in ("discussions", "Discussions", "items", "Items", "data", "Data"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
            return []
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        raise EvidenceUnavailable(
            "DiscussionApi returned an unexpected shape for applicant "
            f"{applicant}: {type(payload).__name__}"
        )

    return list_discussions


def fetch_discussion_evidence(
    list_fn: Callable[[str], list[dict[str, Any]]],
    applicant_id: str,
    discussion_id: str,
) -> dict[str, Any]:
    """Confirm a discussion exists and report its note counters.

    Note TEXT is not API-readable (v8 exposes title/noteCount/
    mostRecentNoteId only), so discussion evidence is: the thread exists,
    and its noteCount / mostRecentNoteId moved the way the plan expects.
    A note the plan expects is verified by mostRecentNoteId equality, never
    by assuming the POST succeeded.
    """

    discussion = str(discussion_id or "").strip()
    if not discussion:
        raise EvidenceUnavailable("discussion id is required for discussion evidence")
    try:
        discussions = list_fn(applicant_id)
    except EvidenceUnavailable:
        raise
    except Exception as exc:
        raise EvidenceUnavailable(f"DiscussionApi read failed: {exc}") from exc

    match: dict[str, Any] | None = None
    for row in discussions or []:
        if not isinstance(row, Mapping):
            continue
        for key in _DISCUSSION_ID_KEYS:
            if str(row.get(key) or "").strip() == discussion:
                match = dict(row)
                break
        if match is not None:
            break
    if match is None:
        raise EvidenceUnavailable(
            f"discussion {discussion} not found for applicant {applicant_id}"
        )
    return {
        "discussion_id": discussion,
        "title": _first_present(match, _DISCUSSION_TITLE_KEYS),
        "note_count": _first_present(match, _DISCUSSION_NOTE_COUNT_KEYS),
        "most_recent_note_id": _first_present(match, _DISCUSSION_LAST_NOTE_KEYS),
    }


# ---------------------------------------------------------------------------
# DocumentApi read-back (proven: search_applicant_documents)
# ---------------------------------------------------------------------------


def fetch_document_evidence(
    search_fn: Callable[[str], Any],
    applicant_id: str,
    document_name: str,
) -> dict[str, Any]:
    """Check whether a named document is attached to the applicant.

    Unlike the policy/discussion fetchers, a missing document is a real
    observed fact (``found: False`` -> MISMATCH against an expected
    attachment), not a fetch failure. Only a failed search itself raises
    EvidenceUnavailable.
    """

    name = str(document_name or "").strip()
    if not name:
        raise EvidenceUnavailable("document name is required for document evidence")
    if extract_document_api_results is None:
        raise EvidenceUnavailable("document API helpers are not importable")
    try:
        payload = search_fn(applicant_id)
    except EvidenceUnavailable:
        raise
    except Exception as exc:
        raise EvidenceUnavailable(f"DocumentApi search failed: {exc}") from exc

    wanted = name.casefold()
    for row in extract_document_api_results(payload):
        display = str(row.get("name") or "").strip()
        if display.casefold() == wanted:
            return {
                "found": True,
                "document_id": str(row.get("id") or ""),
                "name": display,
            }
    return {"found": False, "document_id": None, "name": name}
