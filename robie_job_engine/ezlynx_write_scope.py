"""Keep Test writes isolated; permit Production writes only for the active job's requested client.

Real-client scope is read from the durable original message in the canonical
Production ledger. A worker-supplied applicant flag alone never grants access.
"""

from __future__ import annotations

import re
import json
import os
import socket
import sqlite3
from pathlib import Path
from urllib.parse import urlparse


EZLYNX_WRITE_SCOPE_REFUSED = "EZLYNX_WRITE_SCOPE_REFUSED"
ALLOWED_EZLYNX_WRITE_APPLICANT_IDS = frozenset({"220250093"})
PRODUCTION_JOB_DB = Path('/opt/streetsmart-hermes/robie-job-engine/data/jobs.db')
EZLYNX_HOSTS = frozenset({"app.ezlynx.com", "app.uatezlynx.com"})
_ACCOUNT_PATH = re.compile(r"/web/account/([^/?#]+)(?:/|$)", re.IGNORECASE)
_APPLICANT_PORTAL_PATH = re.compile(
    r"/applicantportal/(?:policy/actions/edit|formentry)/([^/?#]+)(?:/|$)",
    re.IGNORECASE,
)
_APPLICANT_PORTAL_ADD_PATH = re.compile(
    r"^/applicantportal/policy/actions/add/([^/?#]+)/0/?$",
    re.IGNORECASE,
)
_POLICY_FORM_ENTRY_PATH = re.compile(
    r"^/applicantportal/policy/[0-9]+/formentry/index/[0-9]+/?$",
    re.IGNORECASE,
)
_AUTH_PATH_MARKERS = ("/login", "/signin", "/sign-in")


class EzlynxWriteScopeError(RuntimeError):
    """Raised before an EZLynx business write targets a non-allowlisted applicant."""


def normalize_applicant_id(value: object) -> str:
    """Normalize surrounding whitespace only; never rewrite an identifier."""

    return str(value or "").strip()


def requested_message_applicant(payload: dict) -> str | None:
    """Resolve one explicit EZLynx client from original message text, never a worker claim."""
    if not isinstance(payload, dict):
        return None
    text = str(payload.get('request_text') or payload.get('text') or '')
    found = set()
    for url in re.findall(r'https://app\.ezlynx\.com/[^\s<>"\']+', text, flags=re.I):
        applicant = applicant_id_from_ezlynx_url(url.rstrip('.,);]'))
        if applicant and re.fullmatch(r'[1-9]\d*', applicant):
            found.add(applicant)
    for applicant in re.findall(r'\b(?:applicant(?:\s+id)?|ezlynx\s+account(?:\s+id)?)(?:\s*[:#]\s*|\s+)([1-9]\d*)\b', text, flags=re.I):
        found.add(applicant)
    if len(found) != 1:
        return None
    selected = next(iter(found))
    # Existing binding may narrow a job; it must never redirect its request.
    for key in ('applicant_id', 'account_id'):
        if payload.get(key) and normalize_applicant_id(payload[key]) != selected:
            return None
    return selected


def _is_installed_production_runtime() -> bool:
    return (os.environ.get('ROBIE_ENV', '').upper() == 'PRODUCTION'
            and socket.gethostname().split('.')[0] == 'hermes-poc-01'
            and Path(__file__).resolve().is_relative_to(Path('/opt/streetsmart-hermes/releases')))


def production_job_applicant() -> str | None:
    """Read scope only from this running job on the actual Production host/release."""
    if not _is_installed_production_runtime():
        return None
    job_ids = {os.environ[key] for key in ('ROBIE_CURRENT_JOB_ID', 'ROBIE_JOB_ID', 'JOB_ID') if os.environ.get(key)}
    if len(job_ids) != 1:
        return None
    job_id = next(iter(job_ids))
    canonical = PRODUCTION_JOB_DB
    try:
        if not job_id or Path(os.environ.get('ROBIE_JOB_DB', '')).resolve() != canonical.resolve():
            return None
        with sqlite3.connect(f'file:{canonical}?mode=ro', uri=True, timeout=5) as connection:
            row = connection.execute(
                'SELECT j.status,j.payload_json,i.payload_json FROM jobs j '
                'JOIN job_intake i ON i.job_id=j.id WHERE j.id=?', (job_id,)).fetchone()
        if not row or row[0] != 'RUNNING':
            return None
        original = json.loads(row[2])
        current = json.loads(row[1])
        # Mutable worker data may only narrow the immutable intake scope.
        if not isinstance(current, dict) or not isinstance(original, dict):
            return None
        selected = requested_message_applicant(original)
        if not selected:
            return None
        for key in ('applicant_id', 'account_id'):
            if current.get(key) and normalize_applicant_id(current[key]) != selected:
                return None
        return selected
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return None


def applicant_is_write_allowed(value: object) -> bool:
    applicant = normalize_applicant_id(value)
    if applicant in ALLOWED_EZLYNX_WRITE_APPLICANT_IDS:
        return True
    return bool(re.fullmatch(r'[1-9]\d*', applicant) and production_job_applicant() == applicant)


def require_allowed_ezlynx_write_applicant(value: object) -> str:
    applicant_id = normalize_applicant_id(value)
    if not applicant_is_write_allowed(applicant_id):
        display = applicant_id or "<missing>"
        raise EzlynxWriteScopeError(
            f"{EZLYNX_WRITE_SCOPE_REFUSED}: applicant {display} is not authorized by the "
            "active Production job or the Test allowlist"
        )
    return applicant_id


def applicant_id_from_ezlynx_url(url: object) -> str | None:
    parsed = urlparse(str(url or "").strip())
    if parsed.hostname and parsed.hostname.casefold() not in EZLYNX_HOSTS:
        return None
    path = parsed.path or ""
    match = (
        _ACCOUNT_PATH.search(path)
        or _APPLICANT_PORTAL_PATH.search(path)
        or _APPLICANT_PORTAL_ADD_PATH.fullmatch(path)
    )
    return normalize_applicant_id(match.group(1)) if match else None


def is_policy_form_entry_url(url: object) -> bool:
    """True for the numeric FormEntry route whose URL omits applicant id.

    This shape alone never authorizes a write. The Playwright guard must also
    attest the visible account and Test-policy header before permitting a
    control action.
    """

    parsed = urlparse(str(url or "").strip())
    return (
        (parsed.hostname or "").casefold() == "app.ezlynx.com"
        and _POLICY_FORM_ENTRY_PATH.fullmatch(parsed.path or "") is not None
    )


def ezlynx_control_scope_block_reason(
    url: object,
    *,
    requested_applicant_id: object,
) -> str | None:
    """Guard generic Playwright fill/click/select operations on EZLynx.

    Login controls are authentication, not an applicant business-record write.
    Every other generic EZLynx control action must occur on the exact applicant
    URL named by the bound Job, and the active job must authorize that applicant.
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
            "authorized for the active job"
        )
    if not target:
        return (
            f"{EZLYNX_WRITE_SCOPE_REFUSED}: EZLynx page is not scoped to an "
            "explicit allowed applicant route"
        )
    if target != requested:
        return (
            f"{EZLYNX_WRITE_SCOPE_REFUSED}: page applicant {target} does not "
            f"match bound Job applicant {requested}"
        )
    if not applicant_is_write_allowed(target):
        return (
            f"{EZLYNX_WRITE_SCOPE_REFUSED}: page applicant {target} is not "
            "authorized for the active job"
        )
    return None
