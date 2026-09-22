"""Magellan SAD row identity: client phone and display name.

The accountability Google Doc section ``Customer sentiment (SAD) — Magellan``
must show the **client** phone and a real client name. Magellan rows often
expose both a Magellan account / agency DID and a caller ANI. Prefer the
caller ANI. Prefer Magellan's caller name when it is present and not a
generic/agency label; otherwise enrich from EZLynx applicant/client data
matched on that client phone. Never invent a name and never fall back to the
agency brand or agency DID as the client identity.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional, Sequence

# StreetSmart main / Magellan account DIDs observed in RingCentral and Magellan.
DEFAULT_AGENCY_DIDS: frozenset[str] = frozenset(
    {
        "7324628343",
    }
)

_AGENCY_NAME_TOKENS: frozenset[str] = frozenset(
    {
        "street smart",
        "streetsmart",
        "street smart insurance",
        "streetsmart insurance",
        "street smart insurance agency",
        "streetsmart insurance agency",
        "street smart insurance agency llc",
        "agency",
        "main",
        "main line",
        "ai receptionist",
        "magellan",
        "unknown",
        "unknown client",
        "unknown caller",
        "unverified",
        "n/a",
        "na",
        "none",
        "null",
        "-",
        "--",
    }
)

_PHONE_IN_TEXT = re.compile(
    r"(?:\+?1[-.\s]*)?(?:\(?\d{3}\)?[-.\s]*)?\d{3}[-.\s]*\d{4}"
)


def normalize_phone_digits(value: Any) -> str:
    """Return the trailing 10-digit US national number when present."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) >= 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) >= 10:
        return digits[-10:]
    return digits


def is_agency_did(value: Any, agency_dids: Iterable[str] | None = None) -> bool:
    digits = normalize_phone_digits(value)
    if not digits:
        return False
    known = {normalize_phone_digits(item) for item in (agency_dids or DEFAULT_AGENCY_DIDS)}
    known.discard("")
    return digits in known


def is_usable_client_name(value: Any, *, agency_names: Iterable[str] | None = None) -> bool:
    """True when Magellan/EZLynx supplied a non-blank, non-generic person/account name."""
    text = " ".join(str(value or "").split())
    if not text:
        return False
    if _PHONE_IN_TEXT.fullmatch(text):
        return False
    if normalize_phone_digits(text) and not re.search(r"[A-Za-z]", text):
        return False
    folded = text.casefold()
    blocked = set(_AGENCY_NAME_TOKENS)
    if agency_names:
        blocked.update(" ".join(str(item).split()).casefold() for item in agency_names if item)
    return folded not in blocked


def parse_magellan_party_cell(value: Any) -> tuple[str, str]:
    """Split a Magellan From/To cell into ``(caller_name, phone_text)``.

    Magellan often renders a display name above a ``tel:`` number in one cell.
    Returns empty strings when a part is absent. Does not invent names.
    """
    raw = str(value or "").replace("\xa0", " ")
    if not raw.strip():
        return "", ""

    phones = list(_PHONE_IN_TEXT.finditer(raw))
    phone_text = phones[-1].group(0) if phones else ""
    name = raw
    for match in phones:
        name = name.replace(match.group(0), " ")
    name = " ".join(name.split()).strip(" -\t|/")
    if not is_usable_client_name(name):
        name = ""
    if not phone_text:
        # Bare tel:/E.164 with no formatting still counts as a phone.
        digits = normalize_phone_digits(raw)
        if digits and not re.search(r"[A-Za-z]", raw):
            phone_text = digits
            name = ""
    return name, phone_text


def select_client_phone(
    *,
    from_phone: Any = None,
    to_phone: Any = None,
    client_phone: Any = None,
    caller_phone: Any = None,
    agency_dids: Iterable[str] | None = None,
) -> str:
    """Choose the Magellan client/ANI phone, never the agency/account DID.

    Preference:
    1. Explicit ``client_phone`` / ``caller_phone`` when not an agency DID
    2. Magellan ``from`` when it is not an agency DID (typical inbound ANI)
    3. Magellan ``to`` when ``from`` is the agency DID and ``to`` is the client
    4. Empty string when only agency numbers are present
    """
    known = agency_dids
    explicit_candidates = (client_phone, caller_phone)
    for candidate in explicit_candidates:
        _, phone_text = parse_magellan_party_cell(candidate)
        phone_text = phone_text or str(candidate or "").strip()
        if phone_text and not is_agency_did(phone_text, known):
            return phone_text

    from_name, from_text = parse_magellan_party_cell(from_phone)
    to_name, to_text = parse_magellan_party_cell(to_phone)
    del from_name, to_name  # name handling belongs in resolve_sad_client_name
    from_text = from_text or str(from_phone or "").strip()
    to_text = to_text or str(to_phone or "").strip()

    from_is_agency = is_agency_did(from_text, known)
    to_is_agency = is_agency_did(to_text, known)

    if from_text and not from_is_agency:
        return from_text
    if to_text and not to_is_agency:
        return to_text
    return ""


def resolve_sad_client_name(
    *,
    magellan_name: Any = None,
    ezlynx_name: Any = None,
    override_name: Any = None,
    agency_names: Iterable[str] | None = None,
    unknown: str = "Unknown",
) -> str:
    """Name preference: Magellan caller name, else EZLynx/override enrichment, else unknown.

    ``override_name`` is treated as verified EZLynx-side enrichment (for example
    ``MAGELLAN_ACCOUNT_OVERRIDES``). Agency brand labels and bare phones are
    rejected. Does not invent names.
    """
    for candidate in (magellan_name, override_name, ezlynx_name):
        text = " ".join(str(candidate or "").split())
        if is_usable_client_name(text, agency_names=agency_names):
            return text
    return unknown


def ezlynx_name_from_match(match: Mapping[str, Any] | None) -> str:
    """Pull an applicant/client display name from an EZLynx Sales/applicant row."""
    if not match:
        return ""
    for key in (
        "Account Name",
        "Applicant Name",
        "Customer Name",
        "Insured",
        "account_name",
        "applicant_name",
        "client_name",
        "name",
    ):
        value = match.get(key)
        if is_usable_client_name(value):
            return " ".join(str(value).split())
    return ""


def build_sad_identity(
    row: Mapping[str, Any],
    *,
    ezlynx_match: Mapping[str, Any] | None = None,
    phone_overrides: Mapping[str, str] | None = None,
    agency_dids: Sequence[str] | None = None,
    agency_names: Iterable[str] | None = None,
    unknown_name: str = "Unknown",
) -> dict[str, str]:
    """Resolve ``client_phone`` and ``client_name`` for one Magellan SAD call row."""
    client_phone = select_client_phone(
        from_phone=row.get("from_phone") or row.get("from_number") or row.get("From"),
        to_phone=row.get("to_phone") or row.get("to_number") or row.get("To"),
        client_phone=row.get("client_phone"),
        caller_phone=row.get("caller_phone") or row.get("caller_phone_masked"),
        agency_dids=agency_dids,
    )
    magellan_name = (
        row.get("caller_name")
        or row.get("client_name")
        or row.get("from_name")
        or ""
    )
    if not is_usable_client_name(magellan_name, agency_names=agency_names):
        # From cell may still carry "Jane Doe (908) ..." when caller_name was omitted.
        parsed_name, _ = parse_magellan_party_cell(
            row.get("from_phone") or row.get("from_number") or row.get("From") or ""
        )
        magellan_name = parsed_name

    normalized = normalize_phone_digits(client_phone)
    override_name = ""
    if phone_overrides and normalized:
        override_name = str(phone_overrides.get(normalized) or phone_overrides.get(client_phone) or "")

    client_name = resolve_sad_client_name(
        magellan_name=magellan_name,
        ezlynx_name=ezlynx_name_from_match(ezlynx_match),
        override_name=override_name,
        agency_names=agency_names,
        unknown=unknown_name,
    )
    return {
        "client_phone": client_phone,
        "client_name": client_name,
        "normalized_client_phone": normalized,
    }
