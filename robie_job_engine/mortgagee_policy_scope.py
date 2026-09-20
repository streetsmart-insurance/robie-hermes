"""Read-only policy scope for 4372 task CSVs (which contain no policy LOB).

Never infer policy facts from a policy-number prefix, note, or task due date.
Only a unique identity-matched PolicyApi search record supplies metadata.
Loan/lender enrichment remains on the existing Additional Interests path.
"""
from __future__ import annotations

from typing import Any
from datetime import datetime, timezone


# Explicit scope requested by Carlo. Unknown values remain unresolved.
LOB_ALIASES = {
    "homeowners": "Homeowners", "home": "Homeowners",
    "home nj": "Homeowners", "ho": "Homeowners",
    "flood": "Flood", "fld": "Flood",
    "dwelling fire": "Dwelling Fire", "condo": "Condo",
    "auto": "Auto", "personal auto": "Auto", "commercial auto": "Auto",
    "wc": "Workers Comp", "workers comp": "Workers Comp",
    "workers compensation": "Workers Comp",
    "gl": "General Liability", "general liability": "General Liability",
}
PROPERTY_LOBS = frozenset({"Homeowners", "Flood", "Dwelling Fire", "Condo"})


class PolicyScopeUnavailable(ValueError):
    """Missing, ambiguous, or conflicting facts: hold the policy, never skip."""


def normalize_lob(value: Any) -> str | None:
    return LOB_ALIASES.get(" ".join(str(value or "").split()).casefold())


def field(record: dict, *names: str) -> str:
    """Read literal field aliases; conflicting aliases are not first-wins."""
    values = {str(record[name]).strip() for name in names
              if isinstance(record.get(name), (str, int)) and str(record[name]).strip()}
    if len(values) > 1:
        raise PolicyScopeUnavailable(f"conflicting aliases for {names[0]}")
    return next(iter(values), "")


def policy_records(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        if not all(isinstance(row, dict) for row in payload):
            raise PolicyScopeUnavailable("malformed PolicyApi search records")
        return payload
    if not isinstance(payload, dict):
        raise PolicyScopeUnavailable("unrecognized PolicyApi search response")
    if "data" in payload and "status" in payload and str(payload["status"]).casefold() != "success":
        raise PolicyScopeUnavailable("PolicyApi search did not report success")
    if any(key in payload for key in ("policyNumber", "PolicyNumber", "policy_number")):
        return [payload]
    envelopes = [key for key in ("data", "results", "policies", "items") if key in payload]
    if len(envelopes) != 1:
        raise PolicyScopeUnavailable("missing or ambiguous PolicyApi search envelope")
    return policy_records(payload[envelopes[0]])


def resolve_policy_metadata(row: dict, lookup: Any) -> dict:
    """Return whitelisted policy facts only; never copy authorization flags."""
    number = field(row, "policy_number", "Policy Number")
    applicant = field(row, "applicant_id", "Applicant ID")
    master = field(row, "policy_master_id", "Policy Master ID")
    if not number or not applicant:
        raise PolicyScopeUnavailable("policy number and applicant ID required")
    try:
        payload = lookup.search_policy_by_number(number)
    except Exception as exc:
        # Exception text can contain URLs/tokens; retain only its type.
        raise PolicyScopeUnavailable(f"PolicyApi lookup unavailable ({type(exc).__name__})") from None
    candidates = []
    for record in policy_records(payload):
        if field(record, "policyNumber", "PolicyNumber", "policy_number").casefold() != number.casefold():
            continue
        if field(record, "applicantId", "ApplicantId", "ApplicantID", "applicant_id") != applicant:
            continue
        if master and field(record, "policyMasterId", "PolicyMasterId", "policy_master_id") != master:
            continue
        candidates.append(record)
    if len(candidates) != 1:
        raise PolicyScopeUnavailable(f"expected one identity-matched policy; found {len(candidates)}")
    record = candidates[0]
    raw_lob = field(record, "lob", "LOB", "lineOfBusiness", "LineOfBusiness", "line_of_business")
    lob = normalize_lob(raw_lob)
    if lob is None:
        raise PolicyScopeUnavailable("policy LOB missing or unrecognized")
    for key in ("lob", "line_of_business"):
        if row.get(key) and normalize_lob(row[key]) != lob:
            raise PolicyScopeUnavailable("queue and PolicyApi LOB disagree")
    expiration = field(record, "expirationDate", "ExpirationDate", "expiration_date")
    if lob in PROPERTY_LOBS:
        parsed = None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                parsed = datetime.strptime(expiration, fmt).date()
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(expiration.replace("Z", "+00:00")).date()
            except ValueError:
                raise PolicyScopeUnavailable("policy expiration date missing or unparseable") from None
        expiration = parsed.isoformat()
    if row.get("expiration_date") and str(row["expiration_date"]).strip() != expiration:
        raise PolicyScopeUnavailable("queue and PolicyApi expiration disagree")
    return {
        "lob": lob,
        "expiration_date": expiration,
        "carrier": field(record, "carrier", "carrierName", "MasterCompany"),
        "policy_status": field(record, "policyStatus", "PolicyStatus", "policy_status"),
        "_policy_scope_evidence": {
            "source": "PolicyApi/policy/v1/search", "policy_number": number,
            "applicant_id": applicant, "policy_master_id": master,
            "raw_lob": raw_lob, "lob": lob, "expiration_date": expiration,
            "identity_match_count": 1,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def default_policy_lookup():
    # Reuse the established environment-scoped client. No browser, no writes,
    # no fallback from UAT to Production credentials.
    from .ezlynx_api import EzlynxApiClient, load_ezlynx_api_config
    return EzlynxApiClient(load_ezlynx_api_config())
