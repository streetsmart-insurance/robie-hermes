"""Fail-closed applicant scope for consequential EZLynx writes.

The allowlist is fixed at process start. It is read once from
``ROBIE_EZLYNX_WRITE_APPLICANT_IDS`` (preferred) or the legacy alias
``EZLYNX_WRITE_APPLICANT_IDS`` (comma-separated applicant IDs).

Ops flip (Carlo 2026-09-17; fail-closed default 2026-09-26):

- **Test-account only** (default): leave the variable unset or empty. Only
  the test account 220250093 is write-eligible for note append, document
  upload, and other write-scoped calls. This is the safe default.
- **Widened**: set the variable to a comma list, e.g.
  ``ROBIE_EZLYNX_WRITE_APPLICANT_IDS=220250093,123456789``. Only those IDs
  pass the compiled allowlist.
- **All clients**: set ``ROBIE_EZLYNX_WRITE_SCOPE=all`` (or the id list to
  ``*``). This is honored only while Playground guardrails are active.
  Otherwise the id list above is used. Default is closed.

A Job payload, prompt, or any other runtime input cannot widen a
restricted list — authorizing a new account is a deployment-config
change. Bind, cancel, coverage change, and money moves stay blocked by
separate guards; this module only decides *which applicant* may receive
an already-permitted write.

Production Chat jobs stay fail-closed when
:func:`production_job_applicant` returns a bound applicant: writes are
limited to that applicant even if the compiled allowlist is
unrestricted. When no Production job is bound, agency-wide API workers
use the env policy above.

Deletes are never authorized through this scope; the cardinal no-delete
rule is enforced separately.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


EZLYNX_WRITE_SCOPE_REFUSED = "EZLYNX_WRITE_SCOPE_REFUSED"
ROBIE_EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR = "ROBIE_EZLYNX_WRITE_APPLICANT_IDS"
EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR = "EZLYNX_WRITE_APPLICANT_IDS"
# Explicit all-clients switch. Default closed. Honored only while the
# Playground hard blocks, read-back-then-go, and undo log are active.
ROBIE_EZLYNX_WRITE_SCOPE_ENV_VAR = "ROBIE_EZLYNX_WRITE_SCOPE"
WRITE_SCOPE_ALL = "all"
# Preferred name first; legacy alias kept so existing Test/CI env still applies.
EZLYNX_WRITE_APPLICANT_IDS_ENV_VARS = (
    ROBIE_EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR,
    EZLYNX_WRITE_APPLICANT_IDS_ENV_VAR,
)
EZLYNX_HOSTS = frozenset({"app.ezlynx.com", "app.uatezlynx.com"})
TEST_EZLYNX_WRITE_APPLICANT_ID = "220250093"
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
_PLAUSIBLE_APPLICANT_ID = re.compile(r"[1-9]\d*")


class EzlynxWriteScopeError(RuntimeError):
    """Raised before an EZLynx business write targets a non-allowlisted applicant."""


def normalize_applicant_id(value: object) -> str:
    """Normalize surrounding whitespace only; never rewrite an identifier."""

    return str(value or "").strip()


def is_plausible_applicant_id(value: object) -> bool:
    """True for a non-empty EZLynx applicant id that looks like a real account."""

    return bool(_PLAUSIBLE_APPLICANT_ID.fullmatch(normalize_applicant_id(value)))


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
    applicant the active job is bound to. When set, writes are limited to
    that applicant even if the compiled allowlist is unrestricted.
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


def _raw_allowlist_env() -> str | None:
    """Return the first configured allowlist env value, or None if unset."""

    for name in EZLYNX_WRITE_APPLICANT_IDS_ENV_VARS:
        if name in os.environ:
            return os.environ[name]
    return None


def _load_allowed_applicant_ids() -> frozenset[str]:
    """Read the allowlist once at import; job payloads can never widen it.

    Fail-closed: when the env var is unset or empty, the allowlist defaults
    to just the test account (220250093). Set ROBIE_EZLYNX_WRITE_APPLICANT_IDS
    explicitly to widen it — a deployment-config change, never a runtime input.
    """

    raw = _raw_allowlist_env()
    if raw is None or not raw.strip():
        return frozenset({TEST_EZLYNX_WRITE_APPLICANT_ID})
    ids: set[str] = set()
    for part in raw.split(","):
        token = normalize_applicant_id(part)
        if not token or token == "*" or token.casefold() == WRITE_SCOPE_ALL:
            continue
        ids.add(token)
    return frozenset(ids) if ids else frozenset({TEST_EZLYNX_WRITE_APPLICANT_ID})


def write_scope_requests_all() -> bool:
    """True only for an explicit all-clients setting. Default closed.

    ``ROBIE_EZLYNX_WRITE_SCOPE=all`` is the switch. ``ROBIE_EZLYNX_WRITE_APPLICANT_IDS=*``
    means the same request. Any other value, including empty, stays on the
    id list.
    """

    scope = str(os.environ.get(ROBIE_EZLYNX_WRITE_SCOPE_ENV_VAR) or "").strip().casefold()
    if scope == WRITE_SCOPE_ALL:
        return True
    raw = _raw_allowlist_env()
    if raw is None:
        return False
    parts = {normalize_applicant_id(part) for part in raw.split(",") if part.strip()}
    return "*" in parts


def all_clients_scope_honored() -> bool:
    """All-clients counts only while Playground guardrails are active.

    Hard blocks, read-back-then-go, and the undo log have to be in force.
    Otherwise this falls back to the applicant id list.
    """

    if not write_scope_requests_all():
        return False
    from .playground_config import playground_guardrails_active

    return playground_guardrails_active()


def describe_write_scope() -> str:
    """One startup line. All-clients is named only when it is actually honored."""

    if write_scope_requests_all() and all_clients_scope_honored():
        return (
            "EZLynx write scope is all clients. Playground guardrails are active: "
            "hard blocks, read-back-then-go, and the undo log. "
            "Bind, delete, billing, coverage, and client email stay blocked."
        )
    if write_scope_requests_all():
        return (
            "ROBIE_EZLYNX_WRITE_SCOPE=all was requested, but Playground guardrails "
            "are not active. Falling back to the applicant allowlist."
        )
    count = 0 if ALLOWED_EZLYNX_WRITE_APPLICANT_IDS is None else len(ALLOWED_EZLYNX_WRITE_APPLICANT_IDS)
    return (
        f"EZLynx write scope is the applicant allowlist ({count} ids). "
        "All-clients is closed."
    )


def log_write_scope_at_startup() -> str:
    """Log the scope once per process start. Returns the same line for tests."""

    message = describe_write_scope()
    if "Falling back" in message:
        logger.warning(message)
    else:
        logger.info(message)
    return message


ALLOWED_EZLYNX_WRITE_APPLICANT_IDS = _load_allowed_applicant_ids()


#: Certificate-sweep index allowlist (process-scoped).
#:
#: The certificate sweep matches requests against the full-book applicant
#: index (``CERT_APPLICANT_INDEX_PATH``) and files for ANY client in that
#: directory — the index IS the allowlist for the sweep. This is registered
#: once at sweep startup via :func:`register_cert_sweep_applicant_index`
#: and is ``None`` (unregistered) in every other process, so policy-setup
#: and other jobs keep the restrictive compiled allowlist above.
#:
#: Third-party senders (holders, lenders, brokers) are never checked here:
#: the allowlist governs the DESTINATION applicant only. The sender is
#: matched as an applicant through the normal index path; a non-client
#: sender simply never becomes a write destination.
_CERT_SWEEP_INDEX_APPLICANT_IDS: frozenset[str] | None = None


def register_cert_sweep_applicant_index(applicant_ids) -> None:
    """Register the cert-sweep applicant index as an additional allowlist.

    Call once at certificate-sweep startup after loading
    ``CERT_APPLICANT_INDEX_PATH``. Process-scoped: it affects only the
    process that calls it. Never call from policy-setup or Chat paths —
    their restrictive allowlist must not be widened.
    """

    global _CERT_SWEEP_INDEX_APPLICANT_IDS
    normalized = {normalize_applicant_id(a) for a in applicant_ids or ()}
    normalized.discard("")
    _CERT_SWEEP_INDEX_APPLICANT_IDS = frozenset(normalized)


def cert_sweep_index_is_registered() -> bool:
    """True when the cert-sweep applicant index allowlist was registered."""

    return _CERT_SWEEP_INDEX_APPLICANT_IDS is not None


def write_allowlist_is_unrestricted() -> bool:
    """True only when the allowlist was explicitly cleared (legacy mode).

    The default compiled allowlist is fail-closed to the test account; this
    returns True only if ALLOWED_EZLYNX_WRITE_APPLICANT_IDS is None (e.g.
    monkeypatched in tests simulating the old agency-wide mode).
    """

    return ALLOWED_EZLYNX_WRITE_APPLICANT_IDS is None


def applicant_is_write_allowed(value: object) -> bool:
    applicant = normalize_applicant_id(value)
    if not is_plausible_applicant_id(applicant):
        return False
    # A running Production Chat job is fail-closed to its immutable intake
    # applicant. Worker payloads, prompts, and flags cannot widen this.
    live = production_job_applicant()
    if live is not None:
        return applicant == live
    if write_allowlist_is_unrestricted():
        return True
    if all_clients_scope_honored():
        return True
    if applicant in ALLOWED_EZLYNX_WRITE_APPLICANT_IDS:
        return True
    # Certificate-sweep process: the applicant index IS the allowlist. The
    # sweep matched this applicant against the full-book directory before
    # any write was attempted. Only registered by the cert-sweep driver;
    # policy-setup and other jobs never register it, so their restrictive
    # scope is unchanged.
    if (
        _CERT_SWEEP_INDEX_APPLICANT_IDS is not None
        and applicant in _CERT_SWEEP_INDEX_APPLICANT_IDS
    ):
        return True
    return False


def require_allowed_ezlynx_write_applicant(value: object) -> str:
    from .safety_seal import assert_write_checks_intact, driver_gate_for_write

    # The driver lease and the startup snapshot are checked before the
    # allowlist global. Agent code that widens that global fails here.
    assert_write_checks_intact()
    driver_gate_for_write()
    applicant_id = normalize_applicant_id(value)
    if not applicant_is_write_allowed(applicant_id):
        display = applicant_id or "<missing>"
        raise EzlynxWriteScopeError(
            f"{EZLYNX_WRITE_SCOPE_REFUSED}: applicant {display} is not on the "
            "EZLynx business-write allowlist"
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
    attest the visible account before permitting a control action.
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
    URL named by the bound Job, and both identifiers must be write-allowed.
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
            "write-allowed"
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
            "write-allowed"
        )
    return None


log_write_scope_at_startup()
