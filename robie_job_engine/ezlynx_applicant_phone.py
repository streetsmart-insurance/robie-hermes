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
        return extract_dialable_phone(record)
