"""Fail-closed applicant scope for consequential EZLynx writes.

This is deliberately environment-independent.  A Production, Test, local, or
replayed worker may only change an EZLynx applicant compiled into this module.
Changing the scope therefore requires a reviewed code change and release; an
environment variable, prompt, or Job payload cannot widen it.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse


EZLYNX_WRITE_SCOPE_REFUSED = "EZLYNX_WRITE_SCOPE_REFUSED"
ALLOWED_EZLYNX_WRITE_APPLICANT_IDS = frozenset({"220250093"})
_ACCOUNT_PATH = re.compile(r"/web/account/([^/?#]+)(?:/|$)", re.IGNORECASE)
_APPLICANT_PORTAL_PATH = re.compile(
    r"/applicantportal/(?:policy/actions/edit|formentry)/([^/?#]+)(?:/|$)",
    re.IGNORECASE,
)
_AUTH_PATH_MARKERS = ("/login", "/signin", "/sign-in")


class EzlynxWriteScopeError(RuntimeError):
    """Raised before an EZLynx business write targets a non-allowlisted applicant."""


def normalize_applicant_id(value: object) -> str:
    """Normalize surrounding whitespace only; never rewrite an identifier."""

    return str(value or "").strip()


def applicant_is_write_allowed(value: object) -> bool:
    return normalize_applicant_id(value) in ALLOWED_EZLYNX_WRITE_APPLICANT_IDS


def require_allowed_ezlynx_write_applicant(value: object) -> str:
    applicant_id = normalize_applicant_id(value)
    if not applicant_is_write_allowed(applicant_id):
        display = applicant_id or "<missing>"
        raise EzlynxWriteScopeError(
            f"{EZLYNX_WRITE_SCOPE_REFUSED}: applicant {display} is not on the "
            "compiled EZLynx business-write allowlist"
        )
    return applicant_id


def applicant_id_from_ezlynx_url(url: object) -> str | None:
    parsed = urlparse(str(url or "").strip())
    if parsed.hostname and parsed.hostname.casefold() != "app.ezlynx.com":
        return None
    match = _ACCOUNT_PATH.search(parsed.path or "") or _APPLICANT_PORTAL_PATH.search(
        parsed.path or ""
    )
    return normalize_applicant_id(match.group(1)) if match else None


def ezlynx_control_scope_block_reason(
    url: object,
    *,
    requested_applicant_id: object,
) -> str | None:
    """Guard generic Playwright fill/click/select operations on EZLynx.

    Login controls are authentication, not an applicant business-record write.
    Every other generic EZLynx control action must occur on the exact applicant
    URL named by the bound Job, and both identifiers must be compiled-allowed.
    Dedicated read-only crawlers do not use this generic write executor.
    """

    parsed = urlparse(str(url or "").strip())
    if (parsed.hostname or "").casefold() != "app.ezlynx.com":
        return None
    path = (parsed.path or "").casefold()
    if any(marker in path for marker in _AUTH_PATH_MARKERS):
        return None
    requested = normalize_applicant_id(requested_applicant_id)
    target = applicant_id_from_ezlynx_url(url)
    if not requested:
        return (
            f"{EZLYNX_WRITE_SCOPE_REFUSED}: bound Job has no explicit "
            "applicant_id"
        )
    if not applicant_is_write_allowed(requested):
        return (
            f"{EZLYNX_WRITE_SCOPE_REFUSED}: Job applicant {requested} is not "
            "compiled-allowed"
        )
    if not target:
        return (
            f"{EZLYNX_WRITE_SCOPE_REFUSED}: EZLynx page is not scoped to an "
            "explicit /web/account/<applicant_id>/ URL"
        )
    if target != requested:
        return (
            f"{EZLYNX_WRITE_SCOPE_REFUSED}: page applicant {target} does not "
            f"match bound Job applicant {requested}"
        )
    if not applicant_is_write_allowed(target):
        return (
            f"{EZLYNX_WRITE_SCOPE_REFUSED}: page applicant {target} is not "
            "compiled-allowed"
        )
    return None
