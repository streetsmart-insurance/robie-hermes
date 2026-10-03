"""StreetSmart agency identity shared by Progressive FAO pulls.

Live FAO Home renders this agency as ``Streetsmart Risk Mgr (33617)`` and the
login id ``33617c``, with no ``CA33617`` literal. Parenthesized ``(#####)`` and
a ``#####c`` login are agency displays for whichever agency they name. A bare
5-digit token counts only for this agency (``33617``); other bare numbers are
not agent codes (ZIP codes and similar).
"""
from __future__ import annotations

import re
from typing import Any

from .intake_core import IntakeHold


DEFAULT_AGENT_CODE = "CA33617"
_AGENT_CODE = re.compile(r"^CA\d{5}$")
_AGENT_CODE_IN_TEXT = re.compile(r"\bCA\d{5}\b")
_PAREN_AGENCY_IN_TEXT = re.compile(r"\(\s*(\d{5})\s*\)")
_LOGIN_AGENCY_IN_TEXT = re.compile(r"\b(\d{5})c\b", re.IGNORECASE)
_BARE_STREETSMART_AGENCY = re.compile(rf"\b{DEFAULT_AGENT_CODE[2:]}\b")


def require_agent_code(value: str) -> str:
    code = str(value or "").strip().upper()
    if not _AGENT_CODE.fullmatch(code):
        raise IntakeHold("Progressive FAO agent code is missing or ambiguous")
    return code


def agent_codes_in_text(text: str) -> frozenset[str]:
    """Canonical ``CA#####`` codes implied by FAO page text.

    ``CA33617``, ``(33617)``, bare ``33617``, and login ``33617c`` are one
    StreetSmart agency. Any other ``CA#####``, parenthesized ``(#####)``, or
    ``#####c`` login is a different agency. No recognized code is an empty set.
    """
    raw = str(text or "")
    found = set(_AGENT_CODE_IN_TEXT.findall(raw))
    found.update(f"CA{digits}" for digits in _PAREN_AGENCY_IN_TEXT.findall(raw))
    found.update(f"CA{digits}" for digits in _LOGIN_AGENCY_IN_TEXT.findall(raw))
    if _BARE_STREETSMART_AGENCY.search(raw):
        found.add(DEFAULT_AGENT_CODE)
    return frozenset(found)


def assert_agent_context(page: Any, agent_code: str) -> None:
    body = page.locator("body").inner_text()
    found = agent_codes_in_text(str(body or ""))
    if found != {require_agent_code(agent_code)}:
        raise IntakeHold("Progressive FAO agent context is missing or ambiguous")
