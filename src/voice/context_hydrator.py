"""
Context Hydrator for Autonomous Voice Outreach.
Gathers all necessary policy metadata, carrier phone numbers, agency codes,
and contact info from renewals.db, EZLynx API, and carrier directories.
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any, List

from src.config import BASE_DIR, settings
from src.database.models import PolicyRenewal
from src.database.session import SessionLocal
from src.voice.call_directory import lookup_producer, lookup_requestor

logger = logging.getLogger("voice_context_hydrator")

CALL_TYPE_CARRIER = "carrier"
CALL_TYPE_CLIENT_FOLLOWUP = "client_followup"
CALL_TYPE_CLIENT_OUTREACH = "client_outreach"
TRANSFER_MODE_WARM = "warm"
CLIENT_CALL_TYPES = frozenset({CALL_TYPE_CLIENT_FOLLOWUP, CALL_TYPE_CLIENT_OUTREACH})

# Cell → Home → Work. Classic Applicant/v2 uses BusinessPhone as the work line;
# portal sidebar ContactInfo may use WorkPhone. Never invent a number.
_CELL_PHONE_KEYS = ("CellPhone", "cellPhone", "Cell", "MobilePhone", "mobilePhone")
_HOME_PHONE_KEYS = ("HomePhone", "homePhone", "Home")
_WORK_PHONE_KEYS = (
    "WorkPhone",
    "workPhone",
    "Work",
    "BusinessPhone",
    "businessPhone",
)
_CONTACT_NEST_KEYS = ("ContactInfo", "contactInfo", "Contact")
_CO_APPLICANT_KEYS = (
    "CoApplicant",
    "coApplicant",
    "SecondaryApplicant",
    "secondaryApplicant",
    "CoApplicantInfo",
)

# Greeting producer is Sales Center opportunity.producerName — not these
# Classic / commission keys. Kept only so we can refuse them explicitly in tests.
_COMMISSION_PRODUCER_KEYS = (
    "CommissionProducers",
    "commissionProducers",
    "CommissionProducer",
)
_PRODUCER_EMAIL_KEYS = (
    "ProducerEmail",
    "producerEmail",
    "ProducerMail",
)
_FIRST_NAME_KEYS = (
    "FirstName",
    "firstName",
    "PreferredName",
    "PreferredFirstName",
    "preferredName",
    "NickName",
    "Nickname",
    "nickname",
)
# Commercial / contact person first-name keys (Classic Applicant/v2 + sidebar).
# Prefer these over BusinessName. Zapier commercial search uses
# "Primary Contact First Name"; live payloads may nest the same idea.
_COMMERCIAL_FIRST_NAME_KEYS = (
    "ContactFirstName",
    "contactFirstName",
    "PrincipalFirstName",
    "principalFirstName",
    "OwnerFirstName",
    "ownerFirstName",
    "PrimaryContactFirstName",
    "primaryContactFirstName",
    *_FIRST_NAME_KEYS,
)
_COMBINED_PERSON_NAME_KEYS = (
    "ContactName",
    "contactName",
    "FullName",
    "fullName",
    "DisplayName",
    "displayName",
)
_COMMERCIAL_OBJECT_KEYS = (
    "CommercialDetail",
    "commercialDetail",
    "Commercial",
    "commercial",
    "Contact",
    "contact",
    "ContactInfo",
    "contactInfo",
    "PrimaryContact",
    "primaryContact",
    "PrimaryContactInfo",
    "primaryContactInfo",
    "Principal",
    "principal",
    "PrincipalContact",
    "principalContact",
    "Owner",
    "owner",
    "OwnerContact",
    "ownerContact",
    "NamedInsured",
    "namedInsured",
)
_CONTACT_LIST_KEYS = (
    "Contacts",
    "contacts",
    "ContactList",
    "contactList",
    "Principals",
    "principals",
    "Owners",
    "owners",
)
_STAFF_SKIP_KEYS = {
    "Assignment",
    "assignment",
    "AssignedTo",
    "assignedTo",
    "CsrUserModel",
    "csrUserModel",
    "Producer",
    "producer",
    "CommissionProducers",
    "commissionProducers",
    "AssignedProducer",
    "assignedProducer",
    "producerName",
    "ProducerName",
}
_BUSINESS_NAME_KEYS = {
    "BusinessName",
    "businessName",
    "CompanyName",
    "companyName",
    "LegalName",
    "legalName",
    "DBA",
    "Dba",
    "dba",
    "AccountName",
    "accountName",
    "InsuredName",
    "insuredName",
}
_COMMERCIAL_TYPE_RE = re.compile(r"commercial", re.IGNORECASE)
_BUSINESS_NAME_RE = re.compile(
    r"\b(llc|inc|corp|ltd|lp|plc|dba|company|co|insurance|agency|group|"
    r"services|enterprises|associates|holdings)\b",
    re.IGNORECASE,
)
# Classic FirstName placeholders — not a real person name (hermes live verify).
_PLACEHOLDER_FIRST_NAMES = frozenset(
    {
        "n/a",
        "na",
        "n.a",
        "n.a.",
        "none",
        "null",
        "-",
        "--",
        "unknown",
    }
)


def normalize_call_type(raw: Optional[str]) -> str:
    """Map note/email cues onto client_outreach | client_followup | carrier.

    ``client_outreach`` / ``outreach`` / ``cancellation`` stay distinct from
    ``client`` / ``client_followup`` (lead follow-up greeting path).
    """
    if not raw:
        return CALL_TYPE_CARRIER
    cleaned = re.sub(r"[\s-]+", "_", str(raw).strip().lower())
    if (
        "outreach" in cleaned
        or cleaned in ("cancellation", "client_cancellation")
        or cleaned.endswith("_cancellation")
    ):
        return CALL_TYPE_CLIENT_OUTREACH
    if cleaned.startswith("client"):
        return CALL_TYPE_CLIENT_FOLLOWUP
    return CALL_TYPE_CARRIER


def is_client_call_type(call_type: Optional[str]) -> bool:
    """True for insured-facing Bland paths (lead follow-up or client outreach)."""
    return call_type in CLIENT_CALL_TYPES


def normalize_us_e164(raw_phone: Optional[str]) -> Optional[str]:
    """US E.164 only (+1XXXXXXXXXX). Returns None instead of inventing a number."""
    if not raw_phone:
        return None
    digits = re.sub(r"\D", "", str(raw_phone))
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None


def _first_e164_from_mapping(mapping: Dict[str, Any], keys: tuple) -> Optional[str]:
    for key in keys:
        phone = normalize_us_e164(mapping.get(key))
        if phone:
            return phone
    return None


def _contact_field_dicts(contact: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Person dict plus nested ContactInfo (Classic + portal sidebar)."""
    if not isinstance(contact, dict):
        return []
    sources = [contact]
    for nest_key in _CONTACT_NEST_KEYS:
        nested = contact.get(nest_key)
        if isinstance(nested, dict):
            sources.append(nested)
    return sources


def extract_contact_phone(contact: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """CellPhone → HomePhone → WorkPhone. Skip if no valid US E.164."""
    for source in _contact_field_dicts(contact):
        phone = _first_e164_from_mapping(source, _CELL_PHONE_KEYS)
        if phone:
            return phone
    for source in _contact_field_dicts(contact):
        phone = _first_e164_from_mapping(source, _HOME_PHONE_KEYS)
        if phone:
            return phone
    for source in _contact_field_dicts(contact):
        phone = _first_e164_from_mapping(source, _WORK_PHONE_KEYS)
        if phone:
            return phone
    return None


def _co_applicant_from_mapping(mapping: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(mapping, dict):
        return None
    for key in _CO_APPLICANT_KEYS:
        value = mapping.get(key)
        if isinstance(value, dict) and value:
            return value
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item:
                    return item
    return None


def extract_co_applicant(
    applicant: Optional[Dict[str, Any]] = None,
    sidebar: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Co-applicant / secondary from Classic Applicant/v2 or portal sidebar."""
    for mapping in (applicant,):
        found = _co_applicant_from_mapping(mapping)
        if found:
            return found
        for source in _contact_field_dicts(mapping):
            found = _co_applicant_from_mapping(source)
            if found:
                return found
    if not isinstance(sidebar, dict):
        return None
    sidebar_applicant = sidebar.get("Applicant") if isinstance(sidebar.get("Applicant"), dict) else sidebar
    for mapping in (sidebar_applicant, sidebar):
        found = _co_applicant_from_mapping(mapping)
        if found:
            return found
        for source in _contact_field_dicts(mapping):
            found = _co_applicant_from_mapping(source)
            if found:
                return found
    return None


def extract_primary_applicant_contact(
    applicant: Optional[Dict[str, Any]] = None,
    sidebar: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Primary person dict: Classic applicant, else sidebar Applicant / ContactInfo."""
    if isinstance(applicant, dict) and applicant:
        return applicant
    if isinstance(sidebar, dict):
        inner = sidebar.get("Applicant")
        if isinstance(inner, dict) and inner:
            return inner
        for key in _CONTACT_NEST_KEYS:
            nested = sidebar.get(key)
            if isinstance(nested, dict) and nested:
                return nested
    return None


def resolve_client_outreach_targets(
    applicant: Optional[Dict[str, Any]] = None,
    sidebar: Optional[Dict[str, Any]] = None,
    insured_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Primary then secondary dials. Skip missing phones; dedupe shared numbers.

    Never invents E.164 values. Buster Brown (26356199): primary ``7329953409``,
    co-applicant currently has no cell — secondary is omitted until a phone exists.
    """
    targets: List[Dict[str, Any]] = []
    seen_phones = set()

    primary = extract_primary_applicant_contact(applicant, sidebar)
    primary_phone = extract_contact_phone(primary)
    if not primary_phone and isinstance(sidebar, dict):
        sidebar_applicant = sidebar.get("Applicant") if isinstance(sidebar.get("Applicant"), dict) else sidebar
        primary_phone = extract_contact_phone(sidebar_applicant)
    if primary_phone and primary_phone not in seen_phones:
        seen_phones.add(primary_phone)
        targets.append(
            {
                "role": "primary",
                "phone": primary_phone,
                "first_name": extract_client_first_name(
                    primary, insured_name, sidebar=sidebar
                ),
            }
        )

    secondary = extract_co_applicant(applicant, sidebar)
    secondary_phone = extract_contact_phone(secondary)
    if secondary_phone and secondary_phone not in seen_phones:
        seen_phones.add(secondary_phone)
        targets.append(
            {
                "role": "secondary",
                "phone": secondary_phone,
                "first_name": extract_client_first_name(secondary),
            }
        )

    return targets


def _looks_like_business_name(name: str) -> bool:
    return bool(_BUSINESS_NAME_RE.search(name))


def spoken_client_first_name(value: Optional[str]) -> Optional[str]:
    """Speakable first name only. Never a full personal name or LLC token."""
    return _spoken_first_token(value)


def client_spoken_greeting(first_name: Optional[str], *, voicemail: bool = False) -> str:
    """Client-facing opener: ``Hi {First}`` or generic Hi/Hello — never full name."""
    first = spoken_client_first_name(first_name)
    if first:
        return f"Hi {first}"
    return "Hello" if voicemail else "Hi"


def client_account_context_name(
    insured_name: Optional[str],
    first_name: Optional[str] = None,
) -> Optional[str]:
    """Business/account name for staff briefing — never a personal full name.

    ``Buster`` + ``Buster Brown`` → omit (do not say "Buster (Buster Brown)").
    ``Buster`` + ``Green Lion Lawn Care LLC`` → ``Green Lion Lawn Care LLC``.
    """
    insured = " ".join(str(insured_name).split()) if insured_name else ""
    if not insured:
        return None
    first = spoken_client_first_name(first_name)
    if _looks_like_business_name(insured):
        return insured
    if first:
        tokens = insured.split()
        if insured.lower() == first.lower():
            return None
        if len(tokens) >= 2 and tokens[0].lower() == first.lower():
            return None
    return None


def _is_placeholder_first_name(value: str) -> bool:
    token = " ".join(value.split()).strip().lower().rstrip(".")
    return token in _PLACEHOLDER_FIRST_NAMES


def _spoken_first_token(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned or "@" in cleaned:
        return None
    if _is_placeholder_first_name(cleaned):
        return None
    if _looks_like_business_name(cleaned):
        return None
    token = cleaned.split()[0].strip(".,")
    if not token or _is_placeholder_first_name(token) or _looks_like_business_name(token):
        return None
    return token


def _business_name_from_sources(*sources: Optional[Dict[str, Any]]) -> Optional[str]:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in _BUSINESS_NAME_KEYS:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return " ".join(value.split())
    return None


def _is_llc_first_token(token: Optional[str], *business_names: Optional[str]) -> bool:
    """True when token is only the first word of an LLC / business named insured."""
    if not token:
        return False
    needle = token.lower()
    for name in business_names:
        if not name or not _looks_like_business_name(name):
            continue
        first = name.strip().split()[0].lower()
        if first == needle:
            return True
    return False


def _applicant_type_token(*sources: Optional[Dict[str, Any]]) -> str:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("ApplicantType", "applicantType", "AccountType", "accountType"):
            val = source.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _is_commercial_account(
    applicant: Optional[Dict[str, Any]] = None,
    sidebar: Optional[Dict[str, Any]] = None,
    insured_name: Optional[str] = None,
) -> bool:
    sidebar_applicant = None
    if isinstance(sidebar, dict):
        inner = sidebar.get("Applicant") or sidebar.get("applicant")
        if isinstance(inner, dict):
            sidebar_applicant = inner
    sources = (applicant, sidebar_applicant, sidebar)
    if _COMMERCIAL_TYPE_RE.search(_applicant_type_token(*sources)):
        return True
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get("CommercialDetail") or source.get("commercialDetail"):
            return True
        biz = _business_name_from_sources(source)
        if biz and _looks_like_business_name(biz):
            return True
    if insured_name and _looks_like_business_name(insured_name):
        return True
    return False


def _is_primary_contact_row(item: Dict[str, Any]) -> bool:
    for key in (
        "IsPrimary",
        "isPrimary",
        "IsPrimaryContact",
        "isPrimaryContact",
        "Primary",
        "primary",
    ):
        if item.get(key) is True:
            return True
    return False


def _iter_person_dicts(mapping: Optional[Dict[str, Any]], depth: int = 0):
    """Yield applicant / commercial contact dicts. Skip staff assignment trees."""
    if not isinstance(mapping, dict) or depth > 5:
        return
    yield mapping
    for key, nested in mapping.items():
        if key in _STAFF_SKIP_KEYS or key in _BUSINESS_NAME_KEYS:
            continue
        if key in _COMMERCIAL_OBJECT_KEYS and isinstance(nested, dict):
            yield from _iter_person_dicts(nested, depth + 1)
        elif key in _CONTACT_LIST_KEYS:
            if isinstance(nested, dict):
                yield from _iter_person_dicts(nested, depth + 1)
            elif isinstance(nested, list):
                rows = [row for row in nested if isinstance(row, dict)]
                primaries = [row for row in rows if _is_primary_contact_row(row)]
                others = [row for row in rows if row not in primaries]
                for row in primaries + others:
                    yield from _iter_person_dicts(row, depth + 1)


def _first_name_from_person_dict(mapping: Dict[str, Any]) -> Optional[str]:
    """Trust an explicit person FirstName field. Do not reject because it
    matches the first word of BusinessName (Marek / Marek PKS Transportation Inc).
    """
    for key in _COMMERCIAL_FIRST_NAME_KEYS:
        token = _spoken_first_token(mapping.get(key))
        if token:
            return token
    for key in _COMBINED_PERSON_NAME_KEYS:
        token = _spoken_first_token(mapping.get(key))
        if token:
            return token
    return None


def extract_client_first_name(
    applicant: Optional[Dict[str, Any]] = None,
    insured_name: Optional[str] = None,
    sidebar: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """First name for client-facing voice copy.

    Always trust Classic ``FirstName`` / preferred / nickname when present and
    not a placeholder (n/a, none, null, -). A person named Marek on
    ``Marek PKS Transportation Inc`` is valid — do not drop FirstName just
    because it appears in BusinessName.

    When FirstName is missing: nested commercial contacts, then personal
    display name (first token of ``First Last`` only if it is not
    business-looking). Never invent a name from an LLC token.
    """
    sidebar_applicant = None
    if isinstance(sidebar, dict):
        inner = sidebar.get("Applicant") or sidebar.get("applicant")
        if isinstance(inner, dict):
            sidebar_applicant = inner

    business = _business_name_from_sources(applicant, sidebar_applicant, sidebar)
    sources: List[Optional[Dict[str, Any]]] = [applicant, sidebar_applicant, sidebar]
    # Classic Applicant/v2 FirstName wins. Live: Marek PKS 84705043 FirstName=Marek;
    # Green Lion 21587333 FirstName=Anthony; Buster 26356199 FirstName=Buster.
    if isinstance(applicant, dict):
        for key in _FIRST_NAME_KEYS:
            token = _spoken_first_token(applicant.get(key))
            if token:
                return token

    for source in sources:
        for person in _iter_person_dicts(source):
            token = _first_name_from_person_dict(person)
            if token:
                return token

    if _is_commercial_account(applicant, sidebar, insured_name):
        return None
    if insured_name and not _looks_like_business_name(insured_name):
        token = _spoken_first_token(insured_name)
        if token and not _is_llc_first_token(token, business):
            return token
    return None


_OPEN_OPPORTUNITY_STATUSES = {
    "open",
    "active",
    "inprogress",
    "in_progress",
    "new",
    "working",
    "qualified",
    "quoted",
    "proposal",
    "negotiation",
}
_CLOSED_OPPORTUNITY_STATUSES = {
    "closed",
    "won",
    "lost",
    "inactive",
    "cancelled",
    "canceled",
    "expired",
    "dead",
}


def _clean_person_name(value: Any) -> Optional[str]:
    """Strip blanks. Never invent a name. Reject emails."""
    if not isinstance(value, str):
        return None
    name = " ".join(value.split())
    if not name or "@" in name:
        return None
    return name


def _looks_like_full_display_name(name: str) -> bool:
    """Portal AssignedTo is 'Carlo Ferrara'. Classic AssignedTo username is 'Carlo1'."""
    if " " not in name:
        return False
    if re.search(r"\d", name):
        return False
    return True


def unwrap_opportunity_list(payload: Any) -> List[Dict[str, Any]]:
    """Normalize Sales Center GetOpportunitiesForApplicant JSON to opportunity dicts."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    if any(key in payload for key in _COMMISSION_PRODUCER_KEYS):
        return []
    for key in ("opportunities", "Opportunities"):
        inner = payload.get(key)
        if isinstance(inner, list):
            return [row for row in inner if isinstance(row, dict)]
    data = payload.get("data") or payload.get("Data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        nested = data.get("opportunities") or data.get("Opportunities")
        if isinstance(nested, list):
            return [row for row in nested if isinstance(row, dict)]
    # Single opportunity object (has producerName, is not a Classic applicant/policy).
    if payload.get("producerName") or payload.get("ProducerName"):
        if payload.get("AssignedTo") or payload.get("CommissionProducers"):
            return []
        return [payload]
    return []


def _opportunity_status_token(opp: Dict[str, Any]) -> str:
    for key in (
        "status",
        "Status",
        "opportunityStatus",
        "OpportunityStatus",
        "state",
        "State",
        "stage",
        "Stage",
    ):
        val = opp.get(key)
        if isinstance(val, dict):
            val = val.get("name") or val.get("Name") or val.get("status") or val.get("Status")
        if isinstance(val, str) and val.strip():
            return re.sub(r"[\s-]+", "_", val.strip().lower())
    return ""


def opportunity_is_open(opp: Dict[str, Any]) -> Optional[bool]:
    """True/False when the payload distinguishes; None if unknown."""
    for key in ("isOpen", "IsOpen", "isActive", "IsActive"):
        val = opp.get(key)
        if isinstance(val, bool):
            return val
    for key in ("isClosed", "IsClosed", "closed", "Closed"):
        val = opp.get(key)
        if isinstance(val, bool):
            return not val
    token = _opportunity_status_token(opp)
    if not token:
        return None
    if token in _CLOSED_OPPORTUNITY_STATUSES or token.startswith("closed"):
        return False
    if token in _OPEN_OPPORTUNITY_STATUSES or token.startswith("open"):
        return True
    return None


def _opportunity_recency(opp: Dict[str, Any]) -> str:
    for key in (
        "lastModifiedDate",
        "LastModifiedDate",
        "modifiedDate",
        "ModifiedDate",
        "updatedDate",
        "UpdatedDate",
        "createdDate",
        "CreatedDate",
        "dateCreated",
        "DateCreated",
        "opportunityDate",
        "OpportunityDate",
    ):
        val = opp.get(key)
        if val:
            return str(val)
    return ""


def extract_sales_center_producer_name(payload: Any) -> Optional[str]:
    """Pick opportunity ``producerName`` for the Robie lead-follow-up greeting.

    Prefer an open/active opportunity when the payload distinguishes status;
    otherwise the most recent, then the first with a non-empty producerName.
    """
    named: List[Dict[str, Any]] = []
    for opp in unwrap_opportunity_list(payload):
        name = _clean_person_name(opp.get("producerName") or opp.get("ProducerName"))
        if name:
            named.append(opp)
    if not named:
        return None
    open_named = [opp for opp in named if opportunity_is_open(opp) is True]
    pool = open_named or named
    dated = [opp for opp in pool if _opportunity_recency(opp)]
    if dated:
        best = max(dated, key=_opportunity_recency)
        return _clean_person_name(best.get("producerName") or best.get("ProducerName"))
    first = pool[0]
    return _clean_person_name(first.get("producerName") or first.get("ProducerName"))


def extract_sidebar_assigned_producer_full_name(sidebar: Any) -> Optional[str]:
    """Portal ``Applicant.Assignment.AssignedTo`` full name only.

    Fallback when Sales Center has no producerName. Classic Applicant/v2
    ``AssignedTo`` (username like Carlo1) is not this field and is rejected
    unless it is already a full display name (first + last, no digits).
    Never reads ``CsrUserModel`` or ``CommissionProducers``.
    """
    if not isinstance(sidebar, dict):
        return None
    applicant = sidebar.get("Applicant") or sidebar.get("applicant")
    blob: Any = applicant if isinstance(applicant, dict) else sidebar
    assignment = None
    if isinstance(blob, dict):
        assignment = blob.get("Assignment") or blob.get("assignment")
    if not isinstance(assignment, dict):
        return None
    raw = assignment.get("AssignedTo") or assignment.get("assignedTo")
    name = _clean_person_name(raw)
    if name and _looks_like_full_display_name(name):
        return name
    return None


def extract_producer_name(
    *sources: Optional[Dict[str, Any]],
    sales_opportunities: Any = None,
    sidebar: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Greeting producer = Sales Center ``producerName``.

    Fallback (only if Sales Center has no producerName): portal sidebar
    ``Assignment.AssignedTo`` full name. Never commission ``Producer`` /
    ``CommissionProducers``. Never Classic ``AssignedTo`` username alone.
    Never ``CsrUserModel``. No free-text guess.
    """
    if sales_opportunities is not None:
        name = extract_sales_center_producer_name(sales_opportunities)
        if name:
            return name
    for source in sources:
        name = extract_sales_center_producer_name(source)
        if name:
            return name
    if sidebar is not None:
        name = extract_sidebar_assigned_producer_full_name(sidebar)
        if name:
            return name
    for source in sources:
        name = extract_sidebar_assigned_producer_full_name(source)
        if name:
            return name
    return None


def extract_producer_email(*sources: Optional[Dict[str, Any]]) -> Optional[str]:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in _PRODUCER_EMAIL_KEYS:
            value = source.get(key)
            if isinstance(value, str) and "@" in value:
                return value.strip()
        producer = source.get("Producer") or source.get("producer")
        if isinstance(producer, str) and "@" in producer:
            return producer.strip()
        if isinstance(producer, dict):
            for key in ("Email", "email", "EMail"):
                value = producer.get(key)
                if isinstance(value, str) and "@" in value:
                    return value.strip()
    return None


def unwrap_policy_list(policies_res: Any) -> List[Dict[str, Any]]:
    if isinstance(policies_res, list):
        return [p for p in policies_res if isinstance(p, dict)]
    if isinstance(policies_res, dict):
        for key in ("policies", "Policies", "data"):
            inner = policies_res.get(key)
            if isinstance(inner, list):
                return [p for p in inner if isinstance(p, dict)]
            if isinstance(inner, dict):
                nested = inner.get("policies") or inner.get("Policies")
                if isinstance(nested, list):
                    return [p for p in nested if isinstance(p, dict)]
    return []


def match_policy_record(
    policies: List[Dict[str, Any]], policy_number: Optional[str]
) -> Optional[Dict[str, Any]]:
    if not policies:
        return None
    if not policy_number:
        return policies[0]
    needle = str(policy_number).strip().upper()
    for pol in policies:
        for key in ("policyNumber", "PolicyNumber", "PolicyNum"):
            value = pol.get(key)
            if value and needle in str(value).upper():
                return pol
    return policies[0]


@dataclass
class CallingDossier:
    policy_number: str
    insured_name: str
    carrier_name: str
    line_of_business: str
    expiration_date: Optional[str] = None
    expiring_premium: Optional[float] = None
    carrier_phone: Optional[str] = None
    carrier_extension: Optional[str] = None
    agency_code: Optional[str] = None
    applicant_id: Optional[int] = None
    assigned_csr_email: Optional[str] = None
    custom_instructions: Optional[str] = None
    ivr_instructions: Optional[str] = None
    client_first_name: Optional[str] = None
    producer_name: Optional[str] = None
    producer_phone: Optional[str] = None
    requestor_name: Optional[str] = None
    requestor_email: Optional[str] = None
    requestor_phone: Optional[str] = None
    call_type: str = CALL_TYPE_CARRIER
    transfer_mode: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy_number": self.policy_number,
            "insured_name": self.insured_name,
            "carrier_name": self.carrier_name,
            "line_of_business": self.line_of_business,
            "expiration_date": self.expiration_date,
            "expiring_premium": self.expiring_premium,
            "carrier_phone": self.carrier_phone,
            "carrier_extension": self.carrier_extension,
            "agency_code": self.agency_code,
            "applicant_id": self.applicant_id,
            "assigned_csr_email": self.assigned_csr_email,
            "custom_instructions": self.custom_instructions,
            "ivr_instructions": self.ivr_instructions,
            "client_first_name": self.client_first_name,
            "producer_name": self.producer_name,
            "producer_phone": self.producer_phone,
            "requestor_name": self.requestor_name,
            "requestor_email": self.requestor_email,
            "requestor_phone": self.requestor_phone,
            "call_type": self.call_type,
            "transfer_mode": self.transfer_mode,
        }


class ContextHydrator:
    def __init__(
        self,
        carrier_directory_path: Optional[Path] = None,
        ezlynx_directory_path: Optional[Path] = None,
    ):
        self.carrier_dir_path = carrier_directory_path or (BASE_DIR / "data" / "carrier_directory.json")
        self.ezlynx_dir_path = ezlynx_directory_path or (BASE_DIR / "data" / "ezlynx_full_extracted_directory.json")
        self._carrier_dict: Dict[str, Any] = self._load_json(self.carrier_dir_path)
        self._ezlynx_dict: Dict[str, Any] = self._load_json(self.ezlynx_dir_path)

    @staticmethod
    def _load_json(path: Path) -> Dict[str, Any]:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Could not load JSON from {path}: {e}")
        return {}

    def hydrate(
        self,
        policy_number: Optional[str] = None,
        applicant_name: Optional[str] = None,
        phone_override: Optional[str] = None,
        instructions: Optional[str] = None,
        requester_email: Optional[str] = None,
        requestor_name: Optional[str] = None,
        requestor_email: Optional[str] = None,
        call_type: Optional[str] = None,
        applicant_profile: Optional[Dict[str, Any]] = None,
        policy_profile: Optional[Dict[str, Any]] = None,
        sales_opportunities: Any = None,
        sidebar: Optional[Dict[str, Any]] = None,
        enrich_from_ezlynx: bool = False,
    ) -> Optional[CallingDossier]:
        """
        Hydrates a full CallingDossier given partial input (policy number or applicant name).
        Searches renewals.db first, then falls back to EZLynx API if available.
        """
        pol_record: Optional[PolicyRenewal] = None

        # 1. Search local renewals.db
        db = SessionLocal()
        try:
            if policy_number:
                clean_pol = policy_number.strip().upper()
                pol_record = (
                    db.query(PolicyRenewal)
                    .filter(PolicyRenewal.policy_number.ilike(f"%{clean_pol}%"))
                    .first()
                )
            elif applicant_name:
                clean_name = applicant_name.strip()
                pol_record = (
                    db.query(PolicyRenewal)
                    .filter(PolicyRenewal.insured_name.ilike(f"%{clean_name}%"))
                    .first()
                )
        except Exception as exc:
            logger.debug("renewals.db lookup skipped: %s", exc)
            pol_record = None
        finally:
            db.close()

        # 2. Extract base fields from record or fallback
        if pol_record:
            pol_num = pol_record.policy_number
            insured = pol_record.insured_name
            carrier = pol_record.carrier_name
            lob = pol_record.line_of_business or "Commercial"
            exp_date = pol_record.expiration_date.strftime("%Y-%m-%d") if pol_record.expiration_date else None
            exp_prem = pol_record.expiring_premium
            app_id = pol_record.applicant_id
            csr_email = (
                self._resolve_csr_email(pol_record.assigned_agent)
                or requester_email
                or "carlo@streetsmart.insurance"
            )
        else:
            # Fallback to direct EZLynx API search if credentials configured
            api_dossier = self._search_ezlynx_api(policy_number or applicant_name)
            if api_dossier:
                pol_num = api_dossier.get("policy_number", policy_number or "UNKNOWN")
                insured = api_dossier.get("insured_name", applicant_name or "UNKNOWN")
                carrier = api_dossier.get("carrier_name", "UNKNOWN")
                lob = api_dossier.get("line_of_business", "Commercial")
                exp_date = api_dossier.get("expiration_date")
                exp_prem = api_dossier.get("expiring_premium")
                app_id = api_dossier.get("applicant_id")
                csr_email = requester_email or "carlo@streetsmart.insurance"
            else:
                # Minimal fallback when record not found
                if not policy_number and not applicant_name:
                    return None
                pol_num = policy_number or "UNKNOWN"
                insured = applicant_name or "Policyholder"
                carrier = "Unknown Carrier"
                lob = "Commercial"
                exp_date = None
                exp_prem = None
                app_id = None
                csr_email = requester_email or "carlo@streetsmart.insurance"

        # 3. Resolve carrier phone, extension, agency code, and IVR details
        carrier_info = self._resolve_carrier_contact(carrier)
        resolved_phone = phone_override or carrier_info.get("phone")
        resolved_ext = carrier_info.get("extension")
        resolved_agency_code = carrier_info.get("agency_code")
        ivr_notes = carrier_info.get("ivr_instructions")

        dossier = CallingDossier(
            policy_number=pol_num,
            insured_name=insured,
            carrier_name=carrier,
            line_of_business=lob,
            expiration_date=exp_date,
            expiring_premium=exp_prem,
            carrier_phone=resolved_phone,
            carrier_extension=resolved_ext,
            agency_code=resolved_agency_code,
            applicant_id=app_id,
            assigned_csr_email=csr_email,
            custom_instructions=instructions,
            ivr_instructions=ivr_notes,
            call_type=normalize_call_type(call_type),
            requestor_name=requestor_name,
            requestor_email=requestor_email or requester_email,
        )
        self.enrich_identity(
            dossier,
            applicant=applicant_profile,
            policy=policy_profile,
            sales_opportunities=sales_opportunities,
            sidebar=sidebar,
            call_type=call_type,
            requestor_name=requestor_name,
            requestor_email=requestor_email or requester_email,
        )
        if enrich_from_ezlynx:
            self.enrich_identity_from_ezlynx(dossier)
        return dossier

    def enrich_identity(
        self,
        dossier: CallingDossier,
        applicant: Optional[Dict[str, Any]] = None,
        policy: Optional[Dict[str, Any]] = None,
        sales_opportunities: Any = None,
        sidebar: Optional[Dict[str, Any]] = None,
        call_type: Optional[str] = None,
        requestor_name: Optional[str] = None,
        requestor_email: Optional[str] = None,
    ) -> CallingDossier:
        """Apply EZLynx FirstName + Sales Center producerName (greeting) and requestor transfer.

        Greeting producer is Sales Center opportunity ``producerName``
        ("quote {producerName} put together"). Optional fallback is portal
        sidebar ``Assignment.AssignedTo`` full name — never commission
        ``Producer``, never Classic ``AssignedTo`` username. Warm-transfer
        phone is the Robie Call invoker (note author / email sender).
        Missing requestor phone skips transfer — never fall back to the
        greeting producer or another staff DID.
        """
        if call_type:
            dossier.call_type = normalize_call_type(call_type)
        if requestor_name:
            dossier.requestor_name = requestor_name
        if requestor_email:
            dossier.requestor_email = requestor_email

        if applicant or sidebar:
            dossier.client_first_name = extract_client_first_name(
                applicant, dossier.insured_name, sidebar=sidebar
            )
        if not dossier.producer_name:
            dossier.producer_name = extract_producer_name(
                policy,
                applicant,
                sales_opportunities=sales_opportunities,
                sidebar=sidebar,
            )
        if dossier.producer_name:
            producer_email = extract_producer_email(policy, applicant)
            match = lookup_producer(name=dossier.producer_name, email=producer_email)
            if match:
                dossier.producer_name = match.get("name") or dossier.producer_name
            # Greeting-only: do not use this phone for Bland transfer.
            dossier.producer_phone = None

        self._apply_requestor_transfer(dossier)
        return dossier

    def _apply_requestor_transfer(self, dossier: CallingDossier) -> CallingDossier:
        """Look up the label invoker / email sender. No Producer fallback."""
        name = dossier.requestor_name
        email = dossier.requestor_email
        if not name and not email:
            dossier.requestor_phone = None
            dossier.transfer_mode = None
            return dossier
        if name and "robie" in str(name).lower():
            dossier.requestor_phone = None
            dossier.transfer_mode = None
            return dossier
        match = lookup_requestor(name=name, email=email)
        if not match:
            logger.info(
                "Requestor %s <%s> is not in the voice directory; skipping warm transfer.",
                name,
                email,
            )
            dossier.requestor_phone = None
            dossier.transfer_mode = None
            return dossier
        dossier.requestor_name = match.get("name") or dossier.requestor_name
        dossier.requestor_email = match.get("email") or dossier.requestor_email
        phone = match.get("phone")
        if not phone:
            logger.info(
                "Requestor %s has no E.164 DID in the voice directory; skipping warm transfer.",
                dossier.requestor_name,
            )
            dossier.requestor_phone = None
            dossier.transfer_mode = None
            return dossier
        dossier.requestor_phone = phone
        dossier.transfer_mode = TRANSFER_MODE_WARM
        return dossier

    def enrich_identity_from_ezlynx(
        self,
        dossier: CallingDossier,
        ezlynx_client: Optional[Any] = None,
    ) -> CallingDossier:
        """Fetch applicant + Sales Center opportunities; hydrate first name + greeting producer."""
        if not dossier.applicant_id:
            return dossier
        try:
            from src.ezlynx.api_client import EZLynxApiClient

            client = ezlynx_client or EZLynxApiClient()
            app_res = client.get_applicant(str(dossier.applicant_id))
            applicant = app_res.get("applicant") if app_res.get("status") == "success" else None
            policy = None
            try:
                pol_res = client.get_applicant_policies(str(dossier.applicant_id))
                policy = match_policy_record(
                    unwrap_policy_list(pol_res), dossier.policy_number
                )
            except Exception as exc:
                logger.debug("EZLynx policy lookup skipped: %s", exc)
            sales_opportunities = None
            sidebar = None
            try:
                sales_opportunities = client.get_sales_center_opportunities(
                    str(dossier.applicant_id)
                )
            except Exception as exc:
                logger.debug("Sales Center opportunities lookup skipped: %s", exc)
            first_from_applicant = extract_client_first_name(
                applicant, dossier.insured_name
            )
            # Sidebar is needed for AssignedTo producer fallback and for
            # commercial contact first names (Classic often has only BusinessName).
            if not extract_sales_center_producer_name(sales_opportunities) or not first_from_applicant:
                try:
                    sidebar = client.get_applicant_sidebar(str(dossier.applicant_id))
                except Exception as exc:
                    logger.debug("Portal sidebar identity lookup skipped: %s", exc)
            return self.enrich_identity(
                dossier,
                applicant=applicant,
                policy=policy,
                sales_opportunities=sales_opportunities,
                sidebar=sidebar,
            )
        except Exception as exc:
            logger.debug("EZLynx identity enrichment failed gracefully: %s", exc)
            return dossier

    def _resolve_carrier_contact(self, carrier_name: str) -> Dict[str, Any]:
        """Looks up carrier contact info across carrier_directory.json and ezlynx_full_extracted_directory.json."""
        res: Dict[str, Any] = {
            "phone": None,
            "extension": None,
            "agency_code": None,
            "ivr_instructions": None,
        }
        if not carrier_name or carrier_name == "UNKNOWN":
            return res

        clean_carrier = carrier_name.strip().lower()

        # Check carrier_directory.json first (contains curated agent notes & IVR prompts)
        for name, data in self._carrier_dict.items():
            if name.lower() in clean_carrier or clean_carrier in name.lower():
                notes = data.get("notes", "")
                res["agency_code"] = self._extract_agency_code(notes)
                phone_match = self._extract_phone_from_text(notes)
                if phone_match:
                    res["phone"] = phone_match
                res["ivr_instructions"] = self._extract_ivr_instructions(notes)
                break

        # Check ezlynx_full_extracted_directory.json for official phone/ext if phone not found
        for org_name, org_data in self._ezlynx_dict.items():
            if org_name.lower() in clean_carrier or clean_carrier in org_name.lower():
                directory = org_data.get("directory", {})
                ez_phone = directory.get("phone")
                if ez_phone and not res["phone"]:
                    res["phone"] = self._format_phone(ez_phone)
                ez_ext = directory.get("extension")
                if ez_ext and not res["extension"]:
                    res["extension"] = str(ez_ext).strip()
                break

        return res

    @staticmethod
    def _extract_phone_from_text(text: str) -> Optional[str]:
        """Extracts 10-digit phone number formatted as XXX-XXX-XXXX or (XXX) XXX-XXXX."""
        matches = re.findall(r"(?:(?:\+?1\s*(?:[.-]\s*)?)?(?:\(\s*([2-9]1[02-9]|[2-9][02-8]1|[2-9][02-8][02-9])\s*\)|([2-9]1[02-9]|[2-9][02-8]1|[2-9][02-8][02-9]))\s*(?:[.-]\s*)?)?([2-9]1[02-9]|[2-9][02-9]1|[2-9][02-9]{2})\s*(?:[.-]\s*)?([0-9]{4})", text)
        for m in matches:
            digits = "".join(m)
            if len(digits) == 10:
                return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        return None

    @staticmethod
    def _format_phone(raw_phone: str) -> str:
        digits = re.sub(r"\D", "", raw_phone)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        return raw_phone

    @staticmethod
    def _extract_agency_code(notes: str) -> Optional[str]:
        if "X4688" in notes:
            return "X4688"
        if "13653283" in notes:
            return "13653283"
        # Match pattern like Agent Code: 0CHN19 or Agent Code Agt8572 or Code: 7723Y
        matches = re.findall(r"(?:Agent Code|Agency Code|Company Code|Code)[:\s]+([A-Z0-9\-]+)", notes, re.IGNORECASE)
        for m in matches:
            val = m.strip()
            if val.lower() not in ["personal", "commercial", "use", "see", "agent", "this", "value", "proposition", "none"]:
                return val
        return None

    @staticmethod
    def _extract_ivr_instructions(notes: str) -> Optional[str]:
        lines = []
        for line in notes.splitlines():
            if any(k in line.lower() for k in ["press ", "extension", "help line", "line", "option"]):
                lines.append(line.strip())
        return " | ".join(lines) if lines else None

    @staticmethod
    def _resolve_csr_email(agent_name: Optional[str]) -> Optional[str]:
        if not agent_name:
            return None
        low = agent_name.lower()
        if "jake" in low:
            return "jake@streetsmart.insurance"
        if "eimy" in low:
            return "eimy@streetsmart.insurance"
        if "sandy" in low:
            return "sandy@streetsmart.insurance"
        if "nicole" in low:
            return "nicole@streetsmart.insurance"
        if "carlo" in low:
            return "carlo@streetsmart.insurance"
        return None

    def _search_ezlynx_api(self, query: Optional[str]) -> Optional[Dict[str, Any]]:
        """Invokes EZLynx search if available."""
        if not query:
            return None
        try:
            from src.ezlynx.api_client import EZLynxApiClient
            client = EZLynxApiClient()
            results = client.search_applicant(query)
            if not results:
                return None
            first_match = results[0]
            app_id = first_match.get("applicantId") or first_match.get("id")
            insured_name = first_match.get("applicantName") or first_match.get("name")

            if app_id:
                policies = client.get_applicant_policies(app_id)
                for pol in policies:
                    pol_num = pol.get("policyNumber")
                    if not query or query.upper() in (pol_num or "").upper():
                        return {
                            "applicant_id": app_id,
                            "insured_name": insured_name,
                            "policy_number": pol_num,
                            "carrier_name": pol.get("carrierName"),
                            "line_of_business": pol.get("lobName"),
                            "expiration_date": pol.get("expirationDate"),
                            "expiring_premium": pol.get("premium"),
                        }
        except Exception as e:
            logger.debug(f"EZLynx API search failed gracefully: {e}")
        return None
