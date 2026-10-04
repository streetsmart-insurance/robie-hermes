"""Dialable phone from an EZLynx applicant record.

Only a validated US E.164 number from a phone field may be returned.
Policy, claim, and quote fields are logged and never dialed.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

PHONE_FIELDS = ("CellPhone", "BusinessPhone", "HomePhone")
NON_PHONE_FIELDS = (
    "PolicyNumber",
    "Policy",
    "policy_number",
    "ClaimNumber",
    "Claim",
    "QuoteNumber",
    "Quote",
    "ApplicantID",
    "ApplicantId",
)
_DIGITS = re.compile(r"\d")


def to_e164_us(raw: Any) -> Optional[str]:
    """Return +1 and ten NANP digits, or None when the value is not a phone."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    if digits[0] in "01" or digits[3] in "01":
        return None
    return "+1" + digits


def dialable_phone_field(record: Mapping[str, Any] | None) -> Optional[str]:
    """The phone field that produced the dialable number, or None."""
    if not isinstance(record, Mapping):
        return None
    for key in PHONE_FIELDS:
        value = record.get(key)
        if value in (None, ""):
            continue
        if to_e164_us(value):
            return key
    return None


def ambiguous_phone(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Fail closed when phone fields hold more than one E.164 number.

    Candidate labels carry no digits. One number, or the same number in
    more than one field, is not ambiguous.
    """
    if not isinstance(record, Mapping):
        return None
    found: list[tuple[str, str]] = []
    for key in PHONE_FIELDS:
        phone = to_e164_us(record.get(key))
        if phone:
            found.append((key, phone))
    distinct = {phone for _key, phone in found}
    if len(distinct) < 2:
        return None
    labels = {
        "CellPhone": "Cell",
        "BusinessPhone": "Business",
        "HomePhone": "Home",
    }
    return {
        "phone": None,
        "ambiguous": True,
        "candidates": [{"label": labels.get(key, "a number")} for key, _phone in found],
    }


def extract_dialable_phone(record: Mapping[str, Any] | None) -> Optional[str]:
    """The applicant's phone, or None. Never returns a non-phone field."""
    if not isinstance(record, Mapping):
        logger.warning("applicant phone lookup refused: record is not an object")
        return None
    for key in NON_PHONE_FIELDS:
        value = record.get(key)
        if value and _DIGITS.search(str(value)):
            logger.warning("refusing non-phone field %s; not dialing", key)
    for key in PHONE_FIELDS:
        value = record.get(key)
        if value in (None, ""):
            continue
        phone = to_e164_us(value)
        if phone:
            return phone
        logger.warning("phone field %s is not a validated E.164 number; not dialing it", key)
    logger.warning("no validated E.164 phone on the applicant record")
    return None


class EzlynxApplicantPhoneLookup:
    """ApplicantPhonePort backed by an injected applicant-record reader."""

    def __init__(self, fetch_applicant: Callable[[str], Mapping[str, Any] | None]):
        self._fetch = fetch_applicant

    def get_phone(self, applicant_id: str) -> Optional[str]:
        if not str(applicant_id or "").strip():
            logger.warning("applicant phone lookup refused: missing applicant id")
            return None
        try:
            record = self._fetch(str(applicant_id))
        except Exception as exc:  # noqa: BLE001 — fail closed, no dial
            logger.warning("applicant phone lookup failed: %s", type(exc).__name__)
            return None
        ambiguous = ambiguous_phone(record)
        if ambiguous is not None:
            logger.warning("applicant phone lookup refused: more than one E.164 number")
            return ambiguous
        return extract_dialable_phone(record)

    def is_mobile(self, applicant_id: str) -> bool:
        """True only when the dialable number came from CellPhone."""
        if not str(applicant_id or "").strip():
            return False
        try:
            record = self._fetch(str(applicant_id))
        except Exception:  # noqa: BLE001 — fail closed, no text offer
            return False
        return dialable_phone_field(record) == "CellPhone"
