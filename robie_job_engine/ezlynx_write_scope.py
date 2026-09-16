"""Fail-closed applicant scope for consequential EZLynx writes.

The allowlist is fixed at process start: it is read once from the
``EZLYNX_WRITE_APPLICANT_IDS`` environment variable (comma-separated
applicant IDs) and falls back to the compiled ``{"220250093"}`` when the
variable is unset or empty.  A Job payload, prompt, or any other runtime
input cannot widen it -- authorizing a new account is a deployment-config
change, reviewed and released like code, and owned by Carlo.

Deletes are never authorized through this scope; the cardinal no-delete
rule is enforced separately.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
from pathlib import Path
from urllib.parse import urlparse


EZLYNX_WRITE_SCOPE_REFUSED = "EZLYNX_WRITE_SCOPE_REFUSED"
EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR = "EZLYNX_WRITE_APPLICANT_IDS"
EZLYNX_HOSTS = frozenset({"app.ezlynx.com", "app.uatezlynx.com"})
DEFAULT_ALLOWED_EZLYNX_WRITE_APPLICANT_IDS = frozenset({"220250093"})
PRODUCTION_JOB_DB = Path("/opt/streetsmart-hermes/robie-job-engine/data/jobs.db")
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
    """Resolve one explicit EZLynx client from original message text, never a worker claim.

    Read-only and fail-closed: returns an applicant id only when the text
    names exactly one, and never redirects an existing binding. It does not
    widen the write allowlist.
    """
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
    """Read scope only from this running job on the actual Production host/release.

    Read-only and fail-closed: returns None anywhere else. It resolves which
    applicant the active job is bound to; it does not widen the write allowlist.
    """
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


def _load_allowed_applicant_ids() -> frozenset:
    """Read the allowlist once at import; job payloads can never widen it."""

    raw = os.environ.get(EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR, "")
    ids = {normalize_applicant_id(part) for part in raw.split(",")}
    ids.discard("")
    return frozenset(ids) if ids else DEFAULT_ALLOWED_EZLYNX_WRITE_APPLICANT_IDS


ALLOWED_EZLYNX_WRITE_APPLICANT_IDS = _load_allowed_applicant_ids()


def applicant_is_write_allowed(value: object) -> bool:
    applicant = normalize_applicant_id(value)
    if applicant in ALLOWED_EZLYNX_WRITE_APPLICANT_IDS:
        return True
    # A running Production job may write only to the client named by its own
    # immutable intake record on the real Production host/release. Worker
    # payloads, prompts, and flags can never widen this: production_job_applicant
    # is fail-closed everywhere else.
    return bool(
        re.fullmatch(r"[1-9]\d*", applicant) and production_job_applicant() == applicant
    )


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
            "compiled-allowed"
        )
    return None
