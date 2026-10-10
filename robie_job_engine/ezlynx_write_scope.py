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

Operation scope (Carlo 2026-10-10): a process may be allowed to write
exactly two operations, ``document_upload`` and ``note_append``, to ANY
applicant, without widening the applicant allowlist for everything else.
It is switched on only by a root-owned policy file
(``/etc/streetsmart-hermes/ezlynx-write-scope.json``), never by an
environment variable, and only in the two entrypoints the file names
(``robie_filer``, ``ezlynx_api_cli``). No file means closed. See
``docs/EZLYNX_WRITE_SCOPE_POLICY_RUNBOOK.md``.

Deletes are never authorized through this scope; the cardinal no-delete
rule is enforced separately.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
import stat
from datetime import datetime
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
    # One id the user typed, alone or after a name ("Buster Brown 26356199").
    # Digits that appear only inside a URL stay on the host check above.
    # Digits inside a policy number or other hyphenated token (TEST-HO-20260911-E01)
    # are not an applicant. A second different id still fails closed.
    # This does not widen the allowlist.
    prose = re.sub(r'https?://\S+', ' ', text)
    for applicant in re.findall(r'(?<![\dA-Za-z-])([1-9]\d{5,9})(?![\dA-Za-z-])', prose):
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



# --------------------------------------------------------------- operation scope
OPERATION_DOCUMENT_UPLOAD = "document_upload"
OPERATION_NOTE_APPEND = "note_append"
#: The only two operations an operation scope can ever allow. Policy create,
#: discussion create, task create, labels, reassign and every browser save pass
#: no ``operation`` and keep the applicant allowlist.
FILER_OPERATIONS = frozenset({OPERATION_DOCUMENT_UPLOAD, OPERATION_NOTE_APPEND})
ENTRYPOINT_ROBIE_FILER = "robie_filer"
ENTRYPOINT_EZLYNX_API_CLI = "ezlynx_api_cli"
OPERATION_SCOPE_ENTRYPOINTS = frozenset({ENTRYPOINT_ROBIE_FILER, ENTRYPOINT_EZLYNX_API_CLI})
WRITE_SCOPE_POLICY_PATH = Path("/etc/streetsmart-hermes/ezlynx-write-scope.json")
WRITE_SCOPE_POLICY_VERSION = 1
MAX_POLICY_BYTES = 8192
_TRUSTED_OWNER_UID = 0  # root. Tests patch this; nothing at runtime does.
_POLICY_HOSTS = {"PRODUCTION": "hermes-poc-01", "TEST": "hermes-test-01"}
_POLICY_TOP_KEYS = frozenset({"version", "environment", "approved_by", "approved_at", "entrypoints"})
_POLICY_ENTRYPOINT_KEYS = frozenset({"operations"})

#: ``(entrypoint, operations)`` once :func:`register_operation_scope` succeeded
#: in THIS process, else ``None``. Never set by an environment variable.
_OPERATION_SCOPE: tuple[str, frozenset[str]] | None = None


def _hostname() -> str:
    return socket.gethostname().split(".")[0]


def _runtime_environment() -> str:
    return os.environ.get("ROBIE_ENV", "").strip().upper()


def _mode_problem(mode: int, what: str) -> str | None:
    if mode & 0o022:
        return f"{what} is group or world writable"
    return None


def load_write_scope_policy(
    entrypoint: str,
    *,
    policy_path: Path | str | None = None,
    owner_uid: int | None = None,
) -> dict | None:
    """Read and validate the root-owned policy file for one entrypoint.

    Returns ``None`` when there is no file (closed) or the file does not list
    ``entrypoint``. Raises :class:`EzlynxWriteScopeError` for anything wrong
    with a file that exists: a symlink, a wrong owner, a writable file or
    directory, bad JSON, unknown keys or operations, the wrong environment or
    host. A damaged policy never widens anything.

    ``policy_path`` and ``owner_uid`` exist for tests (as does the module's
    ``_TRUSTED_OWNER_UID``). Production code never passes them, and the sealed agent interpreter cannot register at all.
    """

    path = Path(policy_path) if policy_path is not None else WRITE_SCOPE_POLICY_PATH
    owner_uid = _TRUSTED_OWNER_UID if owner_uid is None else owner_uid

    def refuse(reason: str) -> EzlynxWriteScopeError:
        return EzlynxWriteScopeError(
            f"{EZLYNX_WRITE_SCOPE_REFUSED}: write-scope policy {path} refused: {reason}"
        )

    try:
        link = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise refuse(f"cannot be inspected ({exc.__class__.__name__})") from exc
    if not stat.S_ISREG(link.st_mode):
        raise refuse("is not a regular file (symlinks are refused)")
    try:
        parent = os.lstat(path.parent)
    except OSError as exc:
        raise refuse(f"its directory cannot be inspected ({exc.__class__.__name__})") from exc
    if parent.st_uid != owner_uid:
        raise refuse("its directory is not owned by root")
    problem = _mode_problem(parent.st_mode, "its directory")
    if problem:
        raise refuse(problem)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise refuse(f"cannot be opened ({exc.__class__.__name__})") from exc
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (link.st_dev, link.st_ino):
            raise refuse("changed while it was being read")
        if info.st_uid != owner_uid:
            raise refuse("is not owned by root")
        problem = _mode_problem(info.st_mode, "the file")
        if problem:
            raise refuse(problem)
        if info.st_size > MAX_POLICY_BYTES:
            raise refuse("is larger than expected")
        raw = os.read(fd, MAX_POLICY_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_POLICY_BYTES:
        raise refuse("is larger than expected")
    try:
        policy = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise refuse("is not valid JSON") from exc
    if not isinstance(policy, dict):
        raise refuse("must be a JSON object")
    unknown = set(policy) - _POLICY_TOP_KEYS
    if unknown:
        raise refuse(f"has unknown keys {sorted(unknown)}")
    if policy.get("version") != WRITE_SCOPE_POLICY_VERSION or isinstance(policy.get("version"), bool):
        raise refuse(f"version must be {WRITE_SCOPE_POLICY_VERSION}")
    environment = policy.get("environment")
    if environment not in _POLICY_HOSTS:
        raise refuse("environment must be PRODUCTION or TEST")
    if environment != _runtime_environment():
        raise refuse(f"is for {environment}, this process is {_runtime_environment() or 'unset'}")
    if _hostname() != _POLICY_HOSTS[environment]:
        raise refuse(f"is for host {_POLICY_HOSTS[environment]}, this host is {_hostname()}")
    approved_by = policy.get("approved_by")
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise refuse("approved_by is required")
    approved_at = policy.get("approved_at")
    try:
        datetime.fromisoformat(str(approved_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise refuse("approved_at must be an ISO timestamp") from exc
    entrypoints = policy.get("entrypoints")
    if not isinstance(entrypoints, dict) or not entrypoints:
        raise refuse("entrypoints must be a non-empty object")
    parsed: dict[str, frozenset[str]] = {}
    for name, body in entrypoints.items():
        if name not in OPERATION_SCOPE_ENTRYPOINTS:
            raise refuse(f"unknown entrypoint {name!r}")
        if not isinstance(body, dict) or set(body) - _POLICY_ENTRYPOINT_KEYS:
            raise refuse(f"entrypoint {name} has unexpected keys")
        ops = body.get("operations")
        if not isinstance(ops, list) or not ops or not all(isinstance(op, str) for op in ops):
            raise refuse(f"entrypoint {name} needs a non-empty operations list")
        if len(set(ops)) != len(ops):
            raise refuse(f"entrypoint {name} lists an operation twice")
        extra = set(ops) - FILER_OPERATIONS
        if extra:
            raise refuse(f"entrypoint {name} lists operations that cannot be allowed: {sorted(extra)}")
        parsed[name] = frozenset(ops)
    if entrypoint not in parsed:
        return None
    return {
        "entrypoint": entrypoint,
        "operations": parsed[entrypoint],
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "environment": environment,
        "approved_by": approved_by,
        "approved_at": str(approved_at),
        "contents": json.dumps(policy, sort_keys=True, separators=(",", ":")),
    }


def register_operation_scope(
    entrypoint: str,
    *,
    policy_path: Path | str | None = None,
    owner_uid: int | None = None,
) -> dict | None:
    """Allow ``document_upload`` and ``note_append`` to any applicant in THIS process.

    Called by a named entrypoint at start. Needs the root-owned policy file to
    list the entrypoint; with no file, or a file that does not list it, nothing
    changes and ``None`` is returned. Refused in the sealed agent interpreter.
    The policy's sha256 and contents are logged here and returned so the caller
    records them in its own audit trail.
    """

    from .safety_seal import agent_interpreter

    if agent_interpreter():
        raise EzlynxWriteScopeError(
            f"{EZLYNX_WRITE_SCOPE_REFUSED}: operation scope refused in the agent interpreter"
        )
    if entrypoint not in OPERATION_SCOPE_ENTRYPOINTS:
        raise EzlynxWriteScopeError(
            f"{EZLYNX_WRITE_SCOPE_REFUSED}: {entrypoint!r} is not an entrypoint that can hold an operation scope"
        )
    policy = load_write_scope_policy(entrypoint, policy_path=policy_path, owner_uid=owner_uid)
    if policy is None:
        return None
    global _OPERATION_SCOPE
    if _OPERATION_SCOPE is not None and _OPERATION_SCOPE[0] != entrypoint:
        raise EzlynxWriteScopeError(
            f"{EZLYNX_WRITE_SCOPE_REFUSED}: this process already holds the {_OPERATION_SCOPE[0]} scope"
        )
    _OPERATION_SCOPE = (entrypoint, policy["operations"])
    logger.warning(
        "EZLynx operation scope registered: entrypoint=%s operations=%s policy_sha256=%s policy=%s",
        entrypoint,
        ",".join(sorted(policy["operations"])),
        policy["sha256"],
        policy["contents"],
    )
    return policy


def register_filer_operation_scope(
    *, policy_path: Path | str | None = None, owner_uid: int | None = None
) -> dict:
    """robie_filer main only (``--any-applicant``). Refuses when no policy lists the filer."""

    policy = register_operation_scope(
        ENTRYPOINT_ROBIE_FILER, policy_path=policy_path, owner_uid=owner_uid
    )
    if policy is None:
        raise EzlynxWriteScopeError(
            f"{EZLYNX_WRITE_SCOPE_REFUSED}: --any-applicant needs a root-owned write-scope policy "
            f"({WRITE_SCOPE_POLICY_PATH}) that lists {ENTRYPOINT_ROBIE_FILER}. Writes stay on the "
            "applicant allowlist."
        )
    return policy


def operation_scope_registered() -> bool:
    return _OPERATION_SCOPE is not None


def operation_is_write_allowed(value: object, operation: str | None) -> bool:
    """True when this process holds the operation scope for ``operation``.

    Only ``document_upload`` and ``note_append`` can ever pass. A Production
    Chat job bound to an applicant is never widened by it.
    """

    scope = _OPERATION_SCOPE
    if operation is None or scope is None or operation not in FILER_OPERATIONS:
        return False
    if operation not in scope[1]:
        return False
    from .safety_seal import agent_interpreter

    if agent_interpreter():
        return False
    applicant = normalize_applicant_id(value)
    return is_plausible_applicant_id(applicant) and production_job_applicant() is None


def applicant_is_write_allowed_for(value: object, operation: str | None = None) -> bool:
    """Applicant allowlist, or the operation scope for ``operation``."""

    return applicant_is_write_allowed(value) or operation_is_write_allowed(value, operation)


def require_allowed_ezlynx_write_applicant(value: object, *, operation: str | None = None) -> str:
    from .safety_seal import assert_write_checks_intact, driver_gate_for_write

    # The driver lease and the startup snapshot are checked before the
    # allowlist global. Agent code that widens that global fails here.
    assert_write_checks_intact()
    driver_gate_for_write()
    applicant_id = normalize_applicant_id(value)
    if operation_is_write_allowed(applicant_id, operation):
        return applicant_id
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
