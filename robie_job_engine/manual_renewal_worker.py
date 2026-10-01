"""Manual renewal verification worker (job type ``manual_renewal_verification``).

Ports the proven ``renewal-automation-system`` daily pipeline
(``DailyRenewalOrchestrator.run_daily_cycle``) onto the robie-hermes bounded
job engine:

* Fetch EZLynx report 4247 rows (Manual Renewals queue).
* Keep ``Source=Manual`` / non-download rows inside the renewal window.
* MANDATORY real-time cancellation gate per policy before any outreach
  (fail closed: gate unavailable -> no email, ever).
* Carrier routing (PORTAL / EMAIL / EMAIL_ASK_PORTAL) via the carrier
  directory; PORTAL is a bounded stub at launch (per-carrier crawlers are a
  later milestone) that records ``portal_attempted`` plus a login-gap record.
* Underwriter email cadence from robie@streetsmart.insurance, signed "Robie",
  subject ``[RENEWAL-REQ-{id}]``; CC assigned CSR + jake@streetsmart.insurance.
* 5-7 day follow-up cadence, max 2 follow-ups, then exactly one carrier voice
  call via the Bland path with the ``carrier_voice_attempted`` once-only
  guard. Voice NEVER dials clients: the target must resolve from the carrier
  phone directory keyed by carrier name.
* Expiration <= 25 days with no terms -> ESCALATED_MANUAL (pending, owner CSR,
  urgent EZLynx task intent recorded).
* Policy-number aliases: match rows on any known alias (renewal-term flips).
* Every EZLynx note carries the ``Policy: #{num} ({LOB} - {carrier})`` header
  and ends with the exact line ``ROBIE was here``.

Safety invariants (non-negotiable):

* CARDINAL RULE: this module performs no deletions, ever. It only reads
  report rows and upserts JSON state into ``durable_work_items``.
* Every outbound action (email, note post, task, portal retrieval, voice
  call) goes through ``is_action_authorized()``. Unauthorized -> recorded as
  pending, never executed.
* No credentials, cookies, 2SV codes, or customer artifacts appear in code,
  logs, or tests. Voice API key is read from the environment at runtime and
  never logged.
* Forbidden carriers and accounts are never referenced anywhere in this
  module - no special cases, no exceptions.

Sibling modules (built in parallel) are imported when present; every import
has a fail-closed local fallback so the worker degrades to recorded-pending
instead of crashing:

* ``.report_fetcher.fetch_report_rows(*, report_id, fields=None, filters=None,
  db_path=None, session=None) -> list[dict]``
* ``.cancellation_gate.check_policy_active(...) -> (active, reason)``
* ``.carrier_directory.route_carrier`` (falls back to the ported 29-carrier
  routing table when the sibling has not added it yet)
* ``.verification_mailer.send_verification_email``
* ``.verification_common``: ``PolicyOutcome``, ``record_outcomes``,
  ``is_action_authorized``, ``record_login_gap``, note builders
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .models import JobStatus, VerificationEvidence, VerificationResult, WorkerResult

log = logging.getLogger("manual_renewal_worker")

# ---------------------------------------------------------------------------
# Identity / job-type constants
# ---------------------------------------------------------------------------

JOB_TYPE = "manual_renewal_verification"
WORKER_NAME = "manual-renewal"
NAMESPACE = "manual_renewal_verification"  # durable_work_items namespace
REPORT_ID_DEFAULT = "4247"

SENDER_EMAIL = "robie@streetsmart.insurance"
ALWAYS_CC = ("jake@streetsmart.insurance",)
CSR_FALLBACK_EMAIL = "sandy@streetsmart.insurance"
AGENCY_NAME = "StreetSmart Insurance"

# Timing (Carlo 2026-09-10): manual renewals are worked 30-45 days before
# renewal; <=25 days is the escalation band.
WINDOW_MIN_DAYS_DEFAULT = 30
WINDOW_MAX_DAYS_DEFAULT = 45
ESCALATION_THRESHOLD_DAYS_DEFAULT = 25  # <= this with no terms -> ESCALATED_MANUAL

# Cadence (ported from OutreachCadenceManager).
FOLLOWUP_MIN_DAYS = 5
FOLLOWUP_MAX_DAYS = 7
MAX_FOLLOWUPS = 2  # quiet checks after the initial email, then the voice branch

# Voice (Carlo 2026-09-10): Bland AI outbound to CARRIERS is approved.
# Payload voice_enabled defaults to True; the authorized-actions gate and the
# carrier_voice_attempted once-only guard still apply.
VOICE_ENABLED_DEFAULT = True
VOICE_CALLER_ID_DEFAULT = "+17322986745"  # Robie line (old system default)
BLAND_QUEUE_ERROR_STATUSES = frozenset({"pre_queue_error", "queue_error"})
BLAND_CALL_PLACED_STATUSES = frozenset(
    {"queued", "ringing", "in_progress", "in-progress", "completed", "ended"}
)

# ---------------------------------------------------------------------------
# Sibling imports (fail-closed fallbacks)
# ---------------------------------------------------------------------------

try:  # sibling: exact signature fetch_report_rows(*, report_id, fields=None, filters=None, db_path=None, session=None)
    from .report_fetcher import fetch_report_rows  # type: ignore[no-redef]
except ImportError:  # pragma: no cover - sibling not delivered yet
    fetch_report_rows = None  # type: ignore[assignment]

try:  # sibling: check_policy_active(...) -> (active, reason)
    from .cancellation_gate import check_policy_active  # type: ignore[no-redef]
except ImportError:  # pragma: no cover
    check_policy_active = None  # type: ignore[assignment]

try:  # sibling: route_carrier(carrier_name) -> {"channel": ...}
    from .carrier_channel_routing import route_carrier  # type: ignore[no-redef]
except ImportError:  # pragma: no cover
    route_carrier = None  # type: ignore[assignment]

try:  # sibling: send_verification_email(...)
    from .verification_mailer import send_verification_email  # type: ignore[no-redef]
except ImportError:  # pragma: no cover
    send_verification_email = None  # type: ignore[assignment]

try:  # sibling: shared verification helpers
    from . import verification_common as _verification_common  # type: ignore[no-redef]
except ImportError:  # pragma: no cover
    _verification_common = None  # type: ignore[assignment]


def _sibling_attr(name: str):
    """Return a sibling helper from verification_common, or None."""
    if _verification_common is None:
        return None
    fn = getattr(_verification_common, name, None)
    return fn if callable(fn) else None


# ---------------------------------------------------------------------------
# Authorized-actions contract
# ---------------------------------------------------------------------------
# Consumed by the authorized-actions gate (is_action_authorized). Every
# outbound action the worker can take must be named here; anything not in the
# job's authorized set is recorded as pending and never executed.

MANUAL_RENEWAL_CONTRACT_ACTIONS = frozenset(
    {
        "read_report_rows",        # fetch EZLynx report 4247 rows
        "check_cancellation",     # real-time policy status check
        "portal_retrieval",       # bounded carrier-portal document retrieval
        "send_underwriter_email", # initial outreach email to underwriter
        "send_followup_email",    # 5-7 day cadence follow-up email
        "post_ezlynx_note",       # EZLynx discussion note
        "create_ezlynx_task",     # urgent CSR task (escalation)
        "place_carrier_voice_call",  # exactly-once Bland carrier call (give-up branch)
    }
)


def is_action_authorized(job: dict[str, Any], action_name: str) -> bool:
    """True only when the job authorizes this contract action.

    Prefers the sibling gate from verification_common; falls back to the
    job payload's ``authorized_actions`` list intersected with
    MANUAL_RENEWAL_CONTRACT_ACTIONS. Fail closed: unknown -> False.
    """
    sibling = _sibling_attr("is_action_authorized")
    if sibling is not None:
        try:
            return bool(sibling(job, action_name))
        except Exception as exc:  # sibling gate error -> fail closed
            log.warning("sibling is_action_authorized failed: %s", exc)
    payload = job.get("payload") or {}
    allowed = payload.get("authorized_actions")
    if not isinstance(allowed, list):
        return False
    return action_name in MANUAL_RENEWAL_CONTRACT_ACTIONS and action_name in allowed


# ---------------------------------------------------------------------------
# Ported reference data (no credentials)
# ---------------------------------------------------------------------------

# CSR email directory ported from the old OutreachCadenceManager. Business
# contact routing only - no secrets. Payload "csr_email_directory" overrides.
CSR_EMAIL_DIRECTORY = {
    "Bara, Maria": "maria@streetsmart.insurance",
    "Maria Bara": "maria@streetsmart.insurance",
    "Aguilar, Ricardo": "ricardo@streetsmart.insurance",
    "Ricardo Aguilar": "ricardo@streetsmart.insurance",
    "Aguilar, Daniela": "daniela@streetsmart.insurance",
    "Daniela Aguilar": "daniela@streetsmart.insurance",
    "Molina, Jazmin": "jazmin@streetsmart.insurance",
    "Jazmin Molina": "jazmin@streetsmart.insurance",
    "Ferrara, Jake": "jake@streetsmart.insurance",
    "Jake Ferrara": "jake@streetsmart.insurance",
    "Illanes, Andrea": "andrea@streetsmart.insurance",
    "Andrea Illanes": "andrea@streetsmart.insurance",
    "Santana, Sandy": "sandy@streetsmart.insurance",
    "Sandy Santana": "sandy@streetsmart.insurance",
    "Sandy Mara": "sandy@streetsmart.insurance",
    "Cimei, Taylor": "taylor@streetsmart.insurance",
    "Taylor Cimei": "taylor@streetsmart.insurance",
    "Valladarez, Angie": "angie@streetsmart.insurance",
    "Angie Valladarez": "angie@streetsmart.insurance",
    "Ramos, Eimy": "eimy@streetsmart.insurance",
    "Eimy Ramos": "eimy@streetsmart.insurance",
    "Perdomo, Lenin": "lenin@streetsmart.insurance",
    "Gabriela": "gabrielac@streetsmart.insurance",
    "Gabriela C": "gabrielac@streetsmart.insurance",
    "Ashley": "ashley@streetsmart.insurance",
}

# Carrier routing table ported from the old data/carrier_directory.json
# (29 carriers). Used only when the sibling .carrier_directory.route_carrier
# is unavailable. Channels: PORTAL / EMAIL / EMAIL_ASK_PORTAL.
CARRIER_ROUTING_TABLE: dict[str, dict[str, Any]] = {
    "coterie": {"channel": "PORTAL", "portal_url": "https://agent.coterieinsurance.com"},
    "the hartford": {"channel": "PORTAL", "portal_url": "https://ebusiness.thehartford.com"},
    "travelers": {"channel": "PORTAL", "portal_url": "https://www.travelers.com/for-agents"},
    "chubb group": {"channel": "PORTAL"},
    "chubb": {"channel": "PORTAL"},
    "njcrib - hartford assigned risk": {"channel": "EMAIL_ASK_PORTAL", "underwriter_email": "assignedrisk@hartford.com"},
    "amwins mga": {"channel": "EMAIL", "underwriter_email": "renewals@amwins.com"},
    "amwins": {"channel": "EMAIL", "underwriter_email": "renewals@amwins.com"},
    "jimcor mga": {"channel": "EMAIL_ASK_PORTAL", "underwriter_email": "renewals@jimcor.com"},
    "jimcor": {"channel": "EMAIL_ASK_PORTAL", "underwriter_email": "renewals@jimcor.com"},
    "specialty coverage insurance agency mga": {"channel": "EMAIL", "underwriter_email": "renewals@specialtycoverage.com"},
    "rocklake insurance group mga": {"channel": "EMAIL", "underwriter_email": "renewals@rocklakeins.com"},
    "tapco underwriters inc.": {"channel": "PORTAL", "underwriter_email": "renewals@gotapco.com"},
    "tapco": {"channel": "PORTAL", "underwriter_email": "renewals@gotapco.com"},
    "diesel insurance solutions": {"channel": "EMAIL", "underwriter_email": "renewals@dieselins.com"},
    "trinity underwriters": {"channel": "EMAIL", "underwriter_email": "quotes@trinityunderwriters.net"},
    "trinity": {"channel": "EMAIL", "underwriter_email": "quotes@trinityunderwriters.net"},
    "risk placement services (rps) mga": {"channel": "EMAIL_ASK_PORTAL", "underwriter_email": "renewals@rpsins.com"},
    "rps": {"channel": "EMAIL_ASK_PORTAL", "underwriter_email": "renewals@rpsins.com"},
    "cover whale mga": {"channel": "PORTAL"},
    "cover whale": {"channel": "PORTAL"},
    "xpt partners mga": {"channel": "EMAIL", "underwriter_email": "renewals@xptpartners.com"},
    "hyundai marine & fire insurance company": {"channel": "EMAIL", "underwriter_email": "renewals@hyundaifire.com"},
    "njcrib - liberty assigned risk": {"channel": "EMAIL_ASK_PORTAL", "underwriter_email": "assignedrisk@libertymutual.com"},
    "interguard ltd": {"channel": "EMAIL", "underwriter_email": "renewals@interguard.com"},
    "johnson & johnson, inc. mga": {"channel": "EMAIL", "underwriter_email": "renewals@jjins.com"},
    "ftp inc. mga": {"channel": "EMAIL", "underwriter_email": "renewals@ftpinc.com"},
    "new england excess exchange mga": {"channel": "EMAIL", "underwriter_email": "renewals@neee.com"},
    "mga resource, llc": {"channel": "EMAIL", "underwriter_email": "renewals@mgaresource.com"},
    "homeowners choice": {"channel": "PORTAL"},
    "cna surety": {"channel": "PORTAL"},
    "cna": {"channel": "PORTAL"},
    "insurtec inc mga": {"channel": "EMAIL", "underwriter_email": "renewals@insurtec.com"},
    "insurtec": {"channel": "EMAIL", "underwriter_email": "renewals@insurtec.com"},
    "geico": {"channel": "PORTAL"},
    "berkshire hathaway inc": {"channel": "PORTAL"},
    "berkshire hathaway": {"channel": "PORTAL"},
    "morstan general agency": {"channel": "EMAIL", "underwriter_email": "renewals@morstan.com"},
    "markel insurance company": {"channel": "PORTAL"},
    "markel": {"channel": "PORTAL"},
}

# Carrier voice directory (ported from the old voice call directory).
# CARRIER NUMBERS ONLY - staff/personal entries from the old table are
# deliberately NOT ported. Fictional 555 placeholder numbers are dropped:
# numbers are never invented, so a carrier with no verified number resolves
# to None and the voice branch records "phone needed" instead of dialing.
# Payload "carrier_phone_directory" (name -> E.164) overrides/adds entries.
CARRIER_PHONE_DIRECTORY: dict[str, str] = {
    "travelers": "+18002386225",
    "coterie": "+18555673421",
    "coterie insurance": "+18555673421",
    "progressive": "+18008765581",
    "tapco": "+18003345579",
    "tapco underwriters": "+18003345579",
    "chubb": "+18002524670",
    "chubb group": "+18002524670",
    "amtrust": "+18775287878",
    "cna": "+18002622000",
    "cna surety": "+18002622000",
    "liberty mutual": "+18003440197",
    "employers": "+18886826671",
    "guard": "+18006732265",
    "berkshire hathaway guard": "+18006732265",
    "bhhc": "+18884958949",
    "berkshire hathaway": "+18884958949",
    "rps": "+18665958405",
    "risk placement services": "+18665958405",
    "amwins": "+18002213824",
    "jimcor": "+18006440333",
    "specialty coverage": "+18002422200",
    "new england excess": "+18005484301",
    "markel": "+18004311270",
    "utica first": "+18005565376",
    "utica first insurance company": "+18005565376",
    "tip national": "+18006888408",
    "tip national llc": "+18006888408",
}

# ---------------------------------------------------------------------------
# Per-policy outcome schema
# ---------------------------------------------------------------------------

VALID_OUTCOME_STATUSES = ("done", "not_done", "pending")


@dataclass
class PolicyOutcome:
    """Per-policy outcome record (mirrors the design-doc schema)."""

    policy_number: str
    status: str  # done | not_done | pending
    reason: str
    actions_taken: list[str] = field(default_factory=list)
    waiting_on: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""
    policy_aliases: list[str] = field(default_factory=list)
    applicant_id: str | None = None
    insured_name: str | None = None
    department: str | None = None
    carrier: str | None = None
    line_of_business: str | None = None
    expiration_date: str | None = None
    days_to_expiration: int | None = None
    tracking_id: str | None = None
    next_action: str | None = None
    next_followup_due: str | None = None
    followup_count: int = 0
    carrier_voice_attempted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _policy_outcome_cls():
    """Sibling PolicyOutcome when available, else the local dataclass."""
    sibling = _sibling_attr("PolicyOutcome")
    return sibling if sibling is not None else PolicyOutcome


def _mirror_outcomes_local(
    outcomes: list[dict[str, Any]],
    *,
    db_path: str,
    namespace: str = NAMESPACE,
) -> None:
    """Persist per-policy outcome JSON into durable_work_items.

    Uses raw sqlite (the DurableWorkLedger API has no set-outcome call and
    rejects ephemeral paths, which unit tests need). One row per policy
    identity; upsert is atomic per row. This table is append/merge only -
    nothing here deletes.
    """
    now = _utcnow()
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS durable_work_items (
                namespace TEXT NOT NULL,
                work_item_key TEXT NOT NULL,
                lease_owner TEXT,
                lease_expires_at TEXT,
                external_actions INTEGER NOT NULL DEFAULT 0,
                outcome TEXT,
                verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (namespace, work_item_key)
            )"""
        )
        for outcome in outcomes:
            key = _normalize_key(outcome.get("policy_number") or "")
            if not key:
                continue
            payload = json.dumps(outcome, sort_keys=True, default=str)
            conn.execute(
                """INSERT INTO durable_work_items
                   (namespace, work_item_key, created_at, updated_at, outcome)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(namespace, work_item_key) DO UPDATE SET
                     outcome=excluded.outcome, updated_at=excluded.updated_at""",
                (namespace, key, now, now, payload),
            )
        conn.commit()
    finally:
        conn.close()


def _store_for_record_outcomes(job: dict[str, Any], db_path: str | None = None) -> Any:
    """Build a JobStore from the explicit db path or the job's resolved path."""
    from .store import JobStore

    return JobStore(db_path or _resolve_db_path(job))


def record_outcomes(
    job: dict[str, Any],
    outcomes: list[Any],
    *,
    db_path: str | None = None,
    namespace: str = NAMESPACE,
) -> dict[str, Any]:
    """Record per-policy outcomes via the shared path when available.

    Calls the sibling ``verification_common.record_outcomes`` if present,
    and ALWAYS mirrors the outcomes into ``durable_work_items`` under the
    job-type namespace keyed by normalized policy number (the verifier's
    fresh read-back source). Returns a small report of what happened.
    """
    as_dicts: list[dict[str, Any]] = []
    for outcome in outcomes:
        if isinstance(outcome, dict):
            as_dicts.append(outcome)
        elif hasattr(outcome, "to_dict"):
            as_dicts.append(outcome.to_dict())
        else:
            as_dicts.append(dict(outcome))
    report: dict[str, Any] = {"sibling_recorded": False, "mirrored": False}
    sibling = _sibling_attr("record_outcomes")
    if sibling is not None:
        try:
            store = _store_for_record_outcomes(job, db_path=db_path)
            sibling(store, job.get("id"), JOB_TYPE, as_dicts)
        except Exception as exc:  # noqa: BLE001 - sibling failure is data
            log.warning("sibling record_outcomes failed: %s", exc)
        else:
            report["sibling_recorded"] = True
    path = db_path or _resolve_db_path(job)
    if path:
        try:
            _mirror_outcomes_local(as_dicts, db_path=path, namespace=namespace)
            report["mirrored"] = True
        except Exception as exc:
            log.warning("durable outcome mirror failed: %s", exc)
            report["mirror_error"] = str(exc)
    return report


def record_login_gap(
    store: Any,
    job_id: str,
    job_type: str,
    portal_name: str,
    step: str,
    whats_missing: str,
) -> dict[str, Any]:
    """Record a portal login gap for Carlo's walkthrough scheduling.

    Calls sibling ``verification_common.record_login_gap(store, job_id,
    job_type, portal_name, step, whats_missing)`` when available; the gap is
    always also returned so the caller can persist it in outcome evidence.
    """
    gap = {
        "job_id": job_id,
        "job_type": job_type,
        "portal_name": portal_name,
        "step": step,
        "whats_missing": whats_missing,
        "recorded_at": _utcnow(),
    }
    sibling = _sibling_attr("record_login_gap")
    if sibling is not None:
        try:
            sibling(store, job_id, job_type, portal_name, step, whats_missing)
            gap["sibling_recorded"] = True
        except Exception as exc:
            log.warning("sibling record_login_gap failed: %s", exc)
            gap["sibling_error"] = str(exc)
    else:
        gap["sibling_recorded"] = False
    return gap


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%m-%d-%Y"):
        try:
            return datetime.strptime(text[:10] if "T" in text and fmt.endswith("S") else text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _today(payload: dict[str, Any]) -> date:
    ref = payload.get("reference_date")
    parsed = _parse_date(ref) if ref else None
    return parsed or date.today()


def normalize_policy_number(value: Any) -> str:
    """Alphanumeric fold used for equality (dashes/spaces ignored).

    Ported from the old ``policy_aliases.normalize_policy_number``.
    """
    if not value:
        return ""
    return re.sub(r"[^a-zA-Z0-9]", "", str(value)).lower()


def numbers_equivalent(left: Any, right: Any) -> bool:
    a, b = normalize_policy_number(left), normalize_policy_number(right)
    return bool(a) and a == b


def _normalize_key(policy_number: Any) -> str:
    return normalize_policy_number(policy_number)


def _invoke_sibling(fn: Any, attempts: list[tuple[tuple, dict]]) -> tuple[bool, Any, str]:
    """Try sibling call signatures in order.

    Returns (invoked, result, error). TypeError moves to the next signature;
    any other exception is a real failure (fail closed by the caller).
    """
    last_error = "no signatures attempted"
    for args, kwargs in attempts:
        try:
            return True, fn(*args, **kwargs), ""
        except TypeError as exc:
            last_error = f"signature mismatch: {exc}"
            continue
        except Exception as exc:  # noqa: BLE001 - sibling failure is data
            return False, None, f"{type(exc).__name__}: {exc}"
    return False, None, last_error


def _resolve_db_path(job: dict[str, Any], store: Any | None = None) -> str:
    payload = job.get("payload") or {}
    if payload.get("db_path"):
        return str(payload["db_path"])
    store_path = getattr(store, "path", None)
    if store_path:
        return str(store_path)
    env = os.environ.get("ROBIE_JOB_DB")
    if env:
        return env
    return "/opt/streetsmart-hermes/robie-job-engine/data/jobs.db"


def _ctx_note_poster(ctx: dict[str, Any]) -> Any | None:
    """Injected note-poster port from the worker constructor, if any."""
    worker = ctx.get("worker")
    return getattr(worker, "_note_poster", None)


def _ctx_voice_dispatcher(ctx: dict[str, Any]) -> Any | None:
    """Injected voice-dispatcher port from the worker constructor, if any."""
    worker = ctx.get("worker")
    return getattr(worker, "_voice_dispatcher", None)


def _ctx_carrier_directory(ctx: dict[str, Any]) -> dict[str, str]:
    """Merged carrier phone directory: worker seed under payload overrides."""
    merged: dict[str, str] = {}
    worker = ctx.get("worker")
    seed = getattr(worker, "_directory", None)
    if isinstance(seed, dict):
        merged.update(seed)
    payload_dir = (ctx.get("payload") or {}).get("carrier_phone_directory")
    if isinstance(payload_dir, dict):
        merged.update(payload_dir)
    return merged


def _read_durable_state(db_path: str, namespace: str, keys: list[str]) -> dict[str, Any] | None:
    """Fresh read of a durable work-item outcome JSON (first matching key).

    Never raises: missing/unreadable state is None (treated as "no prior
    state", never as proof of anything).
    """
    if not db_path or not keys:
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=30)
    except Exception:
        return None
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE IF NOT EXISTS durable_work_items (
                namespace TEXT NOT NULL,
                work_item_key TEXT NOT NULL,
                lease_owner TEXT,
                lease_expires_at TEXT,
                external_actions INTEGER NOT NULL DEFAULT 0,
                outcome TEXT,
                verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (namespace, work_item_key)
            )"""
        )
        for key in keys:
            if not key:
                continue
            row = conn.execute(
                "SELECT outcome FROM durable_work_items WHERE namespace=? AND work_item_key=?",
                (namespace, key),
            ).fetchone()
            if row and row["outcome"]:
                try:
                    data = json.loads(row["outcome"])
                except (ValueError, TypeError):
                    continue
                if isinstance(data, dict):
                    return data
        return None
    except Exception:
        return None
    finally:
        conn.close()


def _next_followup_date(from_date: date) -> date:
    """+5 days, pushed past the weekend (ported from OutreachCadenceManager)."""
    target = from_date + timedelta(days=FOLLOWUP_MIN_DAYS)
    if target.weekday() == 5:  # Saturday
        target += timedelta(days=2)
    elif target.weekday() == 6:  # Sunday
        target += timedelta(days=1)
    return target


def _row_str(row: dict[str, Any], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


# ---------------------------------------------------------------------------
# EZLynx note builders (ported from the old note_builder.py)
# ---------------------------------------------------------------------------
# Every note: header ``Policy: #{num} ({LOB} - {carrier})`` and ends with the
# exact line ``ROBIE was here``. Builders from verification_common are
# preferred when the sibling provides them.


def _note_header(policy_number: str, lob: str, carrier: str) -> str:
    return f"Policy: #{policy_number} ({lob or 'Commercial'} - {carrier or 'Carrier'})"


def _note_signoff() -> str:
    return "ROBIE was here"


def build_portal_check_note(ctx: dict[str, Any]) -> str:
    sibling = _sibling_attr("build_portal_check_note")
    if sibling is not None:
        try:
            return str(sibling(ctx))
        except Exception as exc:
            log.warning("sibling build_portal_check_note failed: %s", exc)
    lines = [
        _note_header(ctx.get("policy_number", ""), ctx.get("line_of_business", ""), ctx.get("carrier", "")),
        "=== [CARRIER PORTAL AUTOMATION] ===",
        "Status: PORTAL CHECKED - RETRIEVAL DEFERRED (per-carrier crawler milestone)",
        f"Timestamp: {_utcnow()}",
        f"Carrier: {ctx.get('carrier', '')}",
        f"Portal: {ctx.get('portal_url') or ctx.get('portal_name', '')}",
        f"Details: {ctx.get('details', '')}",
        "",
        _note_signoff(),
    ]
    return "\n".join(lines)


def build_outreach_email_note(ctx: dict[str, Any]) -> str:
    sibling = _sibling_attr("build_outreach_email_note")
    if sibling is not None:
        try:
            return str(sibling(ctx))
        except Exception as exc:
            log.warning("sibling build_outreach_email_note failed: %s", exc)
    cc_str = f" (CC: {', '.join(ctx.get('cc_list') or [])})" if ctx.get("cc_list") else ""
    if ctx.get("is_followup"):
        action_desc = (
            f"Sent follow-up #{ctx.get('followup_number', 1)} email to "
            f"{ctx.get('recipient_email', '')}{cc_str} for {ctx.get('carrier', '')}."
        )
    else:
        action_desc = f"Emailed {ctx.get('recipient_email', '')}{cc_str} at {ctx.get('carrier', '')}."
    lines = [
        _note_header(ctx.get("policy_number", ""), ctx.get("line_of_business", ""), ctx.get("carrier", "")),
        action_desc,
        f"Requested upcoming renewal offer and loss runs for Policy #{ctx.get('policy_number', '')} "
        f"(Exp: {ctx.get('expiration_date', '')}).",
        f"Subject: {ctx.get('subject', '')}",
        f"Tracking Ref: [{ctx.get('tracking_id', '')}]",
        "Pending renewal offer - awaiting documents back from underwriter.",
    ]
    if ctx.get("next_followup_due"):
        lines.append(f"Next follow-up scheduled for {ctx.get('next_followup_due')}.")
    lines.extend(["", _note_signoff()])
    return "\n".join(lines)


def build_csr_escalation_note(ctx: dict[str, Any]) -> str:
    sibling = _sibling_attr("build_csr_escalation_note")
    if sibling is not None:
        try:
            return str(sibling(ctx))
        except Exception as exc:
            log.warning("sibling build_csr_escalation_note failed: %s", exc)
    lines = [
        _note_header(ctx.get("policy_number", ""), ctx.get("line_of_business", ""), ctx.get("carrier", "")),
        "=== [CSR ESCALATION - URGENT RENEWAL REVIEW] ===",
        f"Timestamp: {_utcnow()}",
        f"Policy #: {ctx.get('policy_number', '')}",
        f"Named Insured: {ctx.get('insured_name', '')}",
        f"Carrier / MGA: {ctx.get('carrier', '')}",
        f"Expiration Date: {ctx.get('expiration_date', '')} ({ctx.get('days_to_expiration', '?')} days remaining)",
        f"Assigned CSR: {ctx.get('assigned_agent') or 'Unassigned'}",
        f"Reason: {ctx.get('reason', '')}",
        "Action Required: High-priority CSR follow-up with carrier underwriter / portal.",
        "",
        _note_signoff(),
    ]
    return "\n".join(lines)


def build_carrier_voice_note(ctx: dict[str, Any]) -> str:
    sibling = _sibling_attr("build_carrier_voice_note")
    if sibling is not None:
        try:
            return str(sibling(ctx))
        except Exception as exc:
            log.warning("sibling build_carrier_voice_note failed: %s", exc)
    lines = [
        _note_header(ctx.get("policy_number", ""), ctx.get("line_of_business", ""), ctx.get("carrier", "")),
        "",
        "[ROBIE AUTONOMOUS CARRIER CALL DISPATCHED]",
        f"5-7d follow-up budget exhausted ({ctx.get('attempts', MAX_FOLLOWUPS)} quiet checks). "
        f"Robie placed one outbound carrier call to {ctx.get('carrier', '')} at {ctx.get('phone', '')}.",
        "Call type: carrier (never client autodial)",
        f"Call ID: {ctx.get('call_id') or 'n/a'}",
        f"Status: {ctx.get('status', 'DISPATCHED')}",
        "No additional carrier calls will be placed for this policy/term. "
        "Client outreach is not part of this cadence.",
        "",
        _note_signoff(),
    ]
    return "\n".join(lines)


def build_voice_phone_needed_note(ctx: dict[str, Any]) -> str:
    lines = [
        _note_header(ctx.get("policy_number", ""), ctx.get("line_of_business", ""), ctx.get("carrier", "")),
        "",
        "[ROBIE CALL - PHONE NUMBER NEEDED]",
        f"The 5-7d follow-up budget ({MAX_FOLLOWUPS} quiet checks) received no renewal. "
        "Robie would place one carrier call, but no verified carrier phone is on file. "
        "Numbers are never invented.",
        "",
        "Add a verified carrier underwriter number to the voice call directory to enable the call.",
        "",
        _note_signoff(),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email templates (ported from old email_outreach/templates.py; adapted)
# ---------------------------------------------------------------------------


def build_outreach_subject(tracking_id: str, insured_name: str, policy_num: str, expiration: date | None) -> str:
    base = f"[{tracking_id}] Renewal & Loss Runs Request: {insured_name} - Pol #{policy_num}"
    if expiration:
        base += f" (Exp: {expiration.strftime('%m/%d/%Y')})"
    return base


def build_initial_outreach_body(
    *,
    underwriter_name: str = "",
    insured_name: str,
    policy_num: str,
    carrier_name: str,
    line_of_business: str,
    expiration_date: date | None,
    expiring_premium: float | None = None,
    ask_portal: bool = False,
) -> str:
    greeting = f"Hello {underwriter_name};" if underwriter_name else "Hello;"
    lines = [
        greeting,
        "",
        "We hope you are well!",
        "",
        "At your earliest convenience please forward the upcoming renewal and loss runs for our file.",
        "",
        f"• Named Insured: {insured_name}",
        f"• Policy Number: {policy_num}",
        f"• Carrier: {carrier_name}",
        f"• Line of Business: {line_of_business or 'Commercial'}",
        f"• Expiration Date: {expiration_date.strftime('%m/%d/%Y') if expiration_date else 'n/a'}",
    ]
    if expiring_premium:
        lines.append(f"• Expiring Term Premium: ${expiring_premium:,.2f}")
    if ask_portal:
        lines.extend(
            [
                "",
                "Additionally, please let us know if there is an online agent portal where we can "
                "view and download renewal documents directly.",
            ]
        )
    lines.extend(
        [
            "",
            "Should you have any questions please feel free to email me back.",
            "",
            "Thank you,",
            "",
            "Robie",
            AGENCY_NAME,
        ]
    )
    return "\n".join(lines)


def build_followup_body(
    *,
    followup_number: int,
    underwriter_name: str = "",
    insured_name: str,
    policy_num: str,
    expiration_date: date | None,
    days_to_expiration: int | None,
) -> str:
    greeting = f"Hello {underwriter_name};" if underwriter_name else "Hello;"
    exp_str = expiration_date.strftime("%m/%d/%Y") if expiration_date else "n/a"
    days_str = f" ({days_to_expiration} days remaining)" if days_to_expiration is not None else ""
    if followup_number == 1:
        opener = (
            f"We hope you are well!\n\nFollowing up on our earlier request for {insured_name} "
            f"(Pol #{policy_num}), expiring on {exp_str}{days_str}."
        )
    elif followup_number == 2:
        opener = (
            f"We hope you are well!\n\nSecond follow-up regarding the upcoming renewal for {insured_name} "
            f"(Pol #{policy_num}) expiring on {exp_str}{days_str}."
        )
    else:
        opener = (
            f"[URGENT] Final reminder regarding the upcoming renewal for {insured_name} "
            f"(Pol #{policy_num}) expiring on {exp_str}."
        )
    return (
        f"{greeting}\n\n{opener}\n\n"
        "At your earliest convenience please forward the upcoming renewal and loss runs for our file.\n\n"
        "Should you have any questions please feel free to email me back.\n\n"
        "Thank you,\n\nRobie\n"
        f"{AGENCY_NAME}\n"
    )


def resolve_outreach_cc_list(
    assigned_agent: str | None,
    directory: dict[str, str] | None = None,
) -> list[str]:
    """CC list: assigned CSR (directory lookup, sandy@ fallback) + jake@ always."""
    directory = directory or CSR_EMAIL_DIRECTORY
    cc: list[str] = []
    csr_email = None
    if assigned_agent:
        csr_email = directory.get(assigned_agent.strip())
        if not csr_email:
            for part in assigned_agent.replace(",", " ").split():
                part_clean = part.strip().lower()
                if not part_clean:
                    continue
                for key, email in directory.items():
                    if part_clean in key.lower():
                        csr_email = email
                        break
                if csr_email:
                    break
    if not csr_email:
        csr_email = CSR_FALLBACK_EMAIL
    if csr_email and csr_email not in cc:
        cc.append(csr_email)
    for fixed in ALWAYS_CC:
        if fixed not in cc:
            cc.append(fixed)
    return cc

# ---------------------------------------------------------------------------
# Cancellation gate (MANDATORY, fail closed)
# ---------------------------------------------------------------------------


def check_policy_cancellation(row: dict[str, Any]) -> tuple[bool, bool, str]:
    """Run the real-time cancellation gate.

    Returns (ran, active, reason). ``ran=False`` means the check could not
    run at all -> the caller must treat the policy as not_done and NEVER
    email (fail closed).
    """
    if check_policy_active is None:
        return False, False, "cancellation gate unavailable (sibling module not delivered)"
    policy_number = _row_str(row, "policy_number", "policyNumber")
    try:
        result = check_policy_active(
            policy_number=policy_number,
            applicant_id=_row_str(row, "applicant_id", "applicantId"),
            ezlynx_client=_row_str(row, "ezlynx_client", "ezlynxClient"),
        )
    except Exception as exc:  # noqa: BLE001 - gate failure is data
        return False, False, f"cancellation check failed: {type(exc).__name__}: {exc}"
    active, reason = True, ""
    if isinstance(result, (tuple, list)) and len(result) >= 1:
        active = bool(result[0])
        reason = str(result[1]) if len(result) > 1 and result[1] else ""
    elif isinstance(result, dict):
        active = bool(result.get("active", result.get("is_active", False)))
        reason = str(result.get("reason", "") or "")
    elif isinstance(result, bool):
        active = result
    else:
        return False, False, f"cancellation check returned uninterpretable result: {type(result).__name__}"
    return True, active, reason


# ---------------------------------------------------------------------------
# Carrier routing
# ---------------------------------------------------------------------------


def _fallback_route_carrier(carrier_name: str) -> dict[str, Any]:
    key = re.sub(r"\s+", " ", (carrier_name or "").strip().lower())
    entry = CARRIER_ROUTING_TABLE.get(key)
    if entry:
        return {
            "channel": entry["channel"],
            "underwriter_email": entry.get("underwriter_email"),
            "portal_url": entry.get("portal_url"),
            "source": "builtin_routing_table",
        }
    # substring fallback (e.g. "Coterie Insurance" -> "coterie")
    for name, candidate in CARRIER_ROUTING_TABLE.items():
        if name and (name in key or key in name):
            return {
                "channel": candidate["channel"],
                "underwriter_email": candidate.get("underwriter_email"),
                "portal_url": candidate.get("portal_url"),
                "source": "builtin_routing_table",
            }
    return {"channel": "UNKNOWN", "underwriter_email": None, "portal_url": None, "source": "builtin_routing_table"}


def route_policy_carrier(carrier_name: str) -> dict[str, Any]:
    """Route a carrier to PORTAL / EMAIL / EMAIL_ASK_PORTAL / UNKNOWN."""
    if route_carrier is not None:
        invoked, result, error = _invoke_sibling(route_carrier, [((carrier_name,), {}), ((), {"carrier_name": carrier_name})])
        if invoked and isinstance(result, dict) and result.get("channel"):
            out = dict(result)
            out.setdefault("source", "carrier_directory")
            return out
        if invoked:
            log.warning("route_carrier returned unusable result; using builtin table: %s", error or result)
        else:
            log.warning("route_carrier failed (%s); using builtin table", error)
    return _fallback_route_carrier(carrier_name)


# ---------------------------------------------------------------------------
# Email sending (authorized only)
# ---------------------------------------------------------------------------


def _extract_message_id(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    for key in ("id", "message_id", "gmail_message_id", "messageId"):
        value = result.get(key)
        if value:
            return str(value)
    return None


def send_outreach_email(
    *,
    to_email: str,
    cc: list[str],
    subject: str,
    body: str,
    sender: str = SENDER_EMAIL,
) -> tuple[bool, dict[str, Any]]:
    """Send via the sibling verification mailer. Returns (sent, info).

    Never raises: failures are returned so the caller records pending.
    """
    if send_verification_email is None:
        return False, {"sent": False, "error": "verification mailer unavailable (sibling module not delivered)"}
    try:
        result = send_verification_email(
            to=[to_email] if isinstance(to_email, str) else list(to_email),
            cc=list(cc),
            subject=subject,
            text_body=body,
        )
    except Exception as exc:  # noqa: BLE001 - mailer failure is data
        return False, {"sent": False, "error": f"mailer failed: {type(exc).__name__}: {exc}"}
    message_id = _extract_message_id(result)
    info: dict[str, Any] = {"sent": True, "result": result if isinstance(result, dict) else {"raw": str(result)}}
    if message_id:
        info["email_message_id"] = message_id
    return True, info


# ---------------------------------------------------------------------------
# EZLynx note / task intents (authorized only)
# ---------------------------------------------------------------------------


def post_ezlynx_note_intent(
    job: dict[str, Any],
    *,
    note_text: str,
    applicant_id: str,
    discussion_title: str,
    policy_number: str,
    line_of_business: str,
    carrier_name: str,
    note_poster: Any | None = None,
) -> dict[str, Any]:
    """Post an EZLynx discussion note when authorized; otherwise record intent.

    ``note_poster`` is an injected port with the sibling ``post_ezlynx_note``
    kwargs contract; when None the sibling port is used. Returns evidence
    dict with posted True/False. Never raises.
    """
    evidence: dict[str, Any] = {"posted": False, "note_text": note_text}
    if not is_action_authorized(job, "post_ezlynx_note"):
        evidence["reason"] = "post_ezlynx_note not authorized - intent recorded only"
        return evidence
    poster = note_poster if note_poster is not None else _sibling_attr("post_ezlynx_note")
    if poster is None:
        evidence["reason"] = "no note-posting port available - intent recorded only"
        return evidence
    attempts = [
        (
            (),
            {
                "applicant_id": applicant_id,
                "discussion_title": discussion_title,
                "note_text": note_text,
                "policy_number": policy_number,
                "line_of_business": line_of_business,
                "carrier_name": carrier_name,
            },
        ),
        ((applicant_id, discussion_title, note_text), {}),
    ]
    invoked, result, error = _invoke_sibling(poster, attempts)
    if not invoked:
        evidence["reason"] = f"note post failed: {error}"
        return evidence
    evidence["posted"] = True
    if isinstance(result, dict):
        for key in ("ezlynx_note_id", "note_id", "id"):
            if result.get(key):
                evidence["ezlynx_note_id"] = str(result[key])
                break
        evidence["result"] = result
    return evidence


def record_task_intent(
    job: dict[str, Any],
    *,
    title: str,
    description: str,
    applicant_id: str,
    assigned: str | None,
) -> dict[str, Any]:
    """Record an urgent EZLynx task intent; create only when authorized+able."""
    intent: dict[str, Any] = {
        "task_intent_recorded": True,
        "title": title,
        "description": description,
        "applicant_id": applicant_id,
        "assigned": assigned,
        "created": False,
    }
    if not is_action_authorized(job, "create_ezlynx_task"):
        intent["reason"] = "create_ezlynx_task not authorized - intent recorded only"
        return intent
    creator = _sibling_attr("create_ezlynx_task")
    if creator is None:
        intent["reason"] = "no task-creation port available - intent recorded only"
        return intent
    try:
        result = creator(
            applicant_id=applicant_id, title=title, description=description, assigned_user=assigned
        )
    except TypeError:
        try:
            result = creator({"applicant_id": applicant_id, "title": title, "description": description})
        except Exception as exc:  # noqa: BLE001
            intent["reason"] = f"task creation failed: {exc}"
            return intent
    except Exception as exc:  # noqa: BLE001
        intent["reason"] = f"task creation failed: {exc}"
        return intent
    intent["created"] = True
    intent["result"] = result if isinstance(result, dict) else {"raw": str(result)}
    return intent


# ---------------------------------------------------------------------------
# Portal retrieval stub + login-gap reporting
# ---------------------------------------------------------------------------


def _portal_slug(portal_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (portal_name or "unknown").strip().lower()).strip("_") or "unknown"


def attempt_portal_retrieval(
    job: dict[str, Any],
    row: dict[str, Any],
    route: dict[str, Any],
    *,
    db_path: str,
) -> dict[str, Any]:
    """Bounded portal-retrieval action (STUB at launch).

    Per-carrier Playwright crawlers are a later milestone. The stub records
    ``portal_attempted`` with evidence and files a deduplicated login-gap
    record (one per portal per day) via ``record_login_gap`` so Carlo can
    schedule walkthroughs around exactly which portal + step is missing.
    Performs no browser actions and no deletions.
    """
    portal_name = _row_str(row, "carrier_name", "carrierName", "carrier") or "unknown carrier"
    portal_url = route.get("portal_url")
    authorized = is_action_authorized(job, "portal_retrieval")
    evidence: dict[str, Any] = {
        "portal_attempt_stub": True,
        "portal_name": portal_name,
        "portal_url": portal_url,
        "portal_retrieval_authorized": authorized,
        "note": "per-carrier crawler milestone pending - no browser action taken",
    }
    # Deduplicated login-gap record: one per portal per day.
    gap_key = f"login_gap:{_portal_slug(portal_name)}:{date.today().isoformat()}"
    already = _read_durable_state(db_path, NAMESPACE, [gap_key])
    if already is None:
        store_obj: Any = db_path
        try:
            from .store import JobStore  # local import: optional dependency

            store_obj = JobStore(db_path)
        except Exception:
            store_obj = db_path
        gap = record_login_gap(
            store_obj,
            str(job.get("id") or ""),
            JOB_TYPE,
            portal_name,
            step="portal_crawler_unimplemented",
            whats_missing=(
                f"per-carrier Playwright crawler for {portal_name} "
                f"({portal_url or 'no portal URL on file'}) is not built yet; "
                "renewal packet retrieval deferred until the crawler milestone lands"
            ),
        )
        evidence["login_gap"] = gap
        _mirror_outcomes_local(
            [{"policy_number": gap_key, "status": "pending", "reason": "login gap recorded",
              "login_gap": gap, "updated_at": _utcnow()}],
            db_path=db_path,
            namespace=NAMESPACE,
        )
    else:
        evidence["login_gap"] = {"deduplicated": True, "existing": already.get("login_gap")}
    return {"portal_attempted": True, "evidence": evidence}

# ---------------------------------------------------------------------------
# Voice: exactly-once carrier call (Bland path, ported dispatcher semantics)
# ---------------------------------------------------------------------------
# HARD RULE: voice NEVER dials clients. The dial target must resolve from the
# carrier phone directory keyed by carrier name. Staff/personal entries are
# not in the directory; row phone fields (insured/applicant phones) are never
# consulted; numbers are never invented.


def normalize_phone_e164(raw: Any) -> str | None:
    """Normalize any phone string to E.164 (+1XXXXXXXXXX)."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if digits:
        return f"+{digits}"
    return None


def lookup_carrier_phone(carrier_name: str, directory: dict[str, str] | None = None) -> str | None:
    """Carrier-directory E.164 lookup. Never invents a number."""
    table = dict(CARRIER_PHONE_DIRECTORY)
    if directory:
        for name, phone in directory.items():
            norm = normalize_phone_e164(phone)
            if norm:
                table[re.sub(r"\s+", " ", str(name).strip().lower())] = norm
    key = re.sub(r"\s+", " ", (carrier_name or "").strip().lower())
    if key in table:
        return table[key]
    for name, phone in table.items():
        if name and (name in key or key in name):
            return phone
    return None


def resolve_carrier_voice_target(
    carrier_name: str,
    row: dict[str, Any],
    directory: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    """Resolve the dial target, enforcing the never-dial-clients rule.

    Returns (e164_phone, refusal_reason). Refuses when no verified carrier
    number exists, or when the resolved number collides with any personal
    contact field on the row (structural backstop).
    """
    phone = lookup_carrier_phone(carrier_name, directory)
    if not phone:
        return None, "no verified carrier phone on file - numbers are never invented"
    personal_numbers = set()
    for field_name in ("insured_phone", "applicant_phone", "client_phone", "phone", "mobile"):
        norm = normalize_phone_e164(row.get(field_name))
        if norm:
            personal_numbers.add(norm)
    if phone in personal_numbers:
        return None, "resolved number matches a non-carrier contact on the row - refused"
    return phone, ""


def build_carrier_call_prompt(
    *,
    policy_number: str,
    carrier_name: str,
    insured_name: str,
    expiration: date | None,
) -> str:
    """Bland 'task' prompt for the carrier renewal follow-up call."""
    exp_str = expiration.strftime("%m/%d/%Y") if expiration else "the upcoming renewal term"
    return (
        "You are Robie, an AI assistant calling on behalf of StreetSmart Insurance, "
        f"an insurance agency. You are calling {carrier_name} about a commercial "
        f"insurance renewal. Identify yourself as Robie from StreetSmart Insurance. "
        f"Policy number {policy_number} for named insured {insured_name}, expiring {exp_str}. "
        "Ask whether the renewal terms or renewal quote have been released, and where the "
        "renewal packet was delivered. Our agency has emailed twice with no response. "
        "Request that the renewal offer and loss runs be emailed to robie@streetsmart.insurance. "
        "Be polite, professional, and concise. If you reach voicemail, leave a clear message "
        "with the policy number and the callback email."
    )


def _bland_post(payload: dict[str, Any], api_key: str, timeout: int = 15) -> tuple[int, dict[str, Any]]:
    """One POST through bland_transport. This module does not open the vendor host."""
    del timeout
    from .bland_transport import BlandTransportError, post_call

    try:
        data = post_call(payload, api_key=api_key, execute=True)
    except BlandTransportError as exc:
        if exc.status is None:
            raise
        return int(exc.status), dict(exc.body)
    return 200, data if isinstance(data, dict) else {}


def _bland_get(call_id: str, api_key: str, timeout: int = 15) -> dict[str, Any]:
    """One GET through bland_transport. Ambiguous reads stay empty; no retry here."""
    del timeout
    from .bland_transport import get_call

    try:
        data = get_call(call_id, api_key=api_key, execute=True)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _classify_bland_readback(data: dict[str, Any]) -> dict[str, Any]:
    """Ported queue discipline: POST acceptance is not proof of a placed call."""
    inner = data.get("call") if isinstance(data.get("call"), dict) else data
    if not isinstance(inner, dict):
        inner = {}
    queue_status = str(inner.get("queue_status") or "").strip().lower().replace(" ", "_")
    progress = str(inner.get("status") or "").strip().lower().replace(" ", "_")
    if progress in {"success", "ok", "accepted"}:
        progress = ""
    for candidate in (queue_status, progress):
        if candidate in BLAND_QUEUE_ERROR_STATUSES:
            return {"call_placed": False, "queue_status": candidate, "status": "QUEUE_ERROR"}
    for candidate in (queue_status, progress):
        if candidate in BLAND_CALL_PLACED_STATUSES:
            return {"call_placed": True, "queue_status": candidate, "status": "DISPATCHED"}
    return {
        "call_placed": False,
        "queue_status": queue_status or progress or None,
        "status": "ACCEPTED_NOT_CONFIRMED",
    }


def dispatch_carrier_voice_call(
    *,
    policy_number: str,
    carrier_name: str,
    insured_name: str,
    phone_e164: str,
    expiration: date | None,
    job_id: str = "",
    idempotency_key: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Dispatch exactly one carrier call via the Bland path (ported semantics).

    * dry_run or missing API key -> simulation record; NO live call placed.
    * Live: POST /v1/calls (voice nat, model enhanced, record on), retry once
      without 'from' on 400/422, then GET /v1/calls/{id} queue-status
      readback (pre_queue_error/queue_error = failed, never claimed placed).
    * Never raises: transport failures return success=False.
    * The API key is never logged.
    """
    api_key = os.environ.get("VOICE_AI_API_KEY", "").strip()
    from_number = os.environ.get("VOICE_CALLER_ID", "").strip() or VOICE_CALLER_ID_DEFAULT
    prompt = build_carrier_call_prompt(
        policy_number=policy_number,
        carrier_name=carrier_name,
        insured_name=insured_name,
        expiration=expiration,
    )
    base: dict[str, Any] = {
        "policy_number": policy_number,
        "carrier": carrier_name,
        "phone": phone_e164,
        "call_type": "carrier",
    }
    if dry_run or not api_key:
        return {
            **base,
            "success": True,
            "mode": "SIMULATION",
            "call_placed": False,
            "call_id": f"sim_call_{normalize_policy_number(policy_number) or 'policy'}_001",
            "status": "SIMULATED_NO_LIVE_CALL",
            "reason": "dry_run" if dry_run else "VOICE_AI_API_KEY not configured - no live call placed",
        }
    payload: dict[str, Any] = {
        "phone_number": phone_e164,
        "task": prompt,
        "voice": "nat",
        "model": "enhanced",
        "record": True,
        "answered_by_enabled": True,
        "wait_for_greeting": True,
        "ivr_navigation": True,
        "voicemail_action": "leave_message",
        "voicemail_message": (
            f"Hello, this is Robie from StreetSmart Insurance calling about policy {policy_number} "
            f"for {insured_name}. Please email the renewal offer and loss runs to "
            "robie@streetsmart.insurance. Thank you."
        ),
        "metadata": {
            "policy_number": policy_number,
            "carrier": carrier_name,
            "job_id": job_id,
            "idempotency_key": idempotency_key,
            "call_type": "carrier",
        },
        "from": from_number,
    }
    try:
        status, data = _bland_post(payload, api_key)
        if status in (400, 422) and "from" in payload:
            payload.pop("from", None)
            status, data = _bland_post(payload, api_key)
        if status not in (200, 201):
            return {
                **base,
                "success": False,
                "mode": "LIVE_BLAND_AI",
                "call_placed": False,
                "status": "POST_FAILED",
                "error": str(data.get("message") or f"HTTP {status}"),
            }
        call_id = str(data.get("call_id") or "")
        result: dict[str, Any] = {
            **base,
            "success": True,
            "mode": "LIVE_BLAND_AI",
            "call_id": call_id,
            "call_placed": False,
            "status": "ACCEPTED_NOT_CONFIRMED",
        }
        # Queue-status readback: POST acceptance is not proof the phone rang.
        for _ in range(3):
            readback = _bland_get(call_id, api_key)
            classified = _classify_bland_readback(readback)
            result.update(classified)
            if classified.get("call_placed") or classified.get("status") == "QUEUE_ERROR":
                break
        return result
    except Exception as exc:  # noqa: BLE001 - transport failure is data
        return {**base, "success": False, "mode": "LIVE_BLAND_AI", "call_placed": False,
                "status": "TRANSPORT_ERROR", "error": f"{type(exc).__name__}"}


def run_voice_giveup_branch(
    job: dict[str, Any],
    row: dict[str, Any],
    state: dict[str, Any],
    *,
    expiration: date | None,
    dry_run: bool = False,
    dispatcher: Any | None = None,
    directory: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Give-up branch: after MAX_FOLLOWUPS quiet checks, exactly one carrier call.

    Honors the ``carrier_voice_attempted`` once-only guard (never re-fires),
    the ``voice_enabled`` payload flag (default True), and the
    ``place_carrier_voice_call`` authorization. ``dispatcher`` is an injected
    port exposing ``dispatch_carrier_call(**kwargs)`` with the same kwargs and
    result-dict contract as :func:`dispatch_carrier_voice_call`; ``directory``
    is a merged carrier-name -> E.164 override table. Returns a dict
    describing the branch outcome for the policy outcome record.
    """
    policy_number = _row_str(row, "policy_number", "policyNumber")
    carrier_name = _row_str(row, "carrier_name", "carrierName", "carrier")
    payload = job.get("payload") or {}
    voice_enabled = bool(payload.get("voice_enabled", VOICE_ENABLED_DEFAULT))
    branch: dict[str, Any] = {"branch": "voice_giveup", "attempted": False}

    if state.get("carrier_voice_attempted"):
        branch["outcome"] = "already_attempted"
        branch["reason"] = "carrier voice already attempted - guard prevents re-fire"
        return branch
    if not voice_enabled:
        branch["outcome"] = "deferred"
        branch["reason"] = "voice deferred - portal+email only at launch"
        return branch
    if not is_action_authorized(job, "place_carrier_voice_call"):
        branch["outcome"] = "deferred"
        branch["reason"] = "voice call recorded but not placed - place_carrier_voice_call not authorized"
        return branch

    phone, refusal = resolve_carrier_voice_target(
        carrier_name,
        row,
        directory if directory is not None else payload.get("carrier_phone_directory"),
    )
    if not phone:
        # Guard is set even with no phone (ported semantics): the branch ran,
        # it must never re-fire. The note tells staff how to enable the call.
        branch["outcome"] = "phone_needed"
        branch["reason"] = refusal
        branch["set_guard"] = True
        return branch

    request = {
        "policy_number": policy_number,
        "carrier_name": carrier_name,
        "insured_name": _row_str(row, "insured_name", "insuredName") or "the named insured",
        "phone_e164": phone,
        "expiration": expiration,
        "job_id": str(job.get("id") or ""),
        "idempotency_key": str(job.get("idempotency_key") or ""),
        "dry_run": dry_run,
    }
    if dispatcher is not None:
        try:
            dispatch = dispatcher.dispatch_carrier_call(**request)
        except Exception as exc:  # noqa: BLE001 - injected port failure is data
            dispatch = {
                "success": False,
                "mode": "INJECTED_DISPATCHER",
                "call_placed": False,
                "status": "TRANSPORT_ERROR",
                "error": f"{type(exc).__name__}",
                "policy_number": policy_number,
                "carrier": carrier_name,
                "phone": phone,
            }
        if not isinstance(dispatch, dict):
            dispatch = {
                "success": False,
                "mode": "INJECTED_DISPATCHER",
                "call_placed": False,
                "status": "BAD_PORT_RESULT",
                "error": f"dispatcher returned {type(dispatch).__name__}, expected dict",
                "policy_number": policy_number,
                "carrier": carrier_name,
                "phone": phone,
            }
    else:
        dispatch = dispatch_carrier_voice_call(**request)
    branch["attempted"] = True
    branch["set_guard"] = True  # the attempt was made; never re-fire
    branch["dispatch"] = dispatch
    if dispatch.get("call_placed"):
        branch["outcome"] = "dispatched"
        branch["reason"] = f"carrier call placed via Bland (call_id={dispatch.get('call_id')})"
    elif dispatch.get("mode") == "SIMULATION":
        branch["outcome"] = "simulated"
        branch["reason"] = dispatch.get("reason", "simulated - no live call placed")
        branch["set_guard"] = False  # no real attempt; may retry once keyed
    else:
        branch["outcome"] = "failed"
        branch["reason"] = f"carrier call failed: {dispatch.get('error') or dispatch.get('status')}"
    return branch

# ---------------------------------------------------------------------------
# ManualRenewalWorker
# ---------------------------------------------------------------------------

REPORT_ROW_FIELDS = [
    "policy_number",
    "insured_name",
    "applicant_id",
    "carrier_name",
    "line_of_business",
    "expiration_date",
    "source",
    "expiring_premium",
    "underwriter_name",
    "underwriter_email",
    "assigned_agent",
    "department",
    "policy_aliases",
]


def _is_manual_source(row: dict[str, Any]) -> bool:
    source = row.get("source")
    if source is None:
        return True
    text = str(source).strip().lower()
    return text in {"manual", "non-download", "nondownload", ""}


def _row_aliases(row: dict[str, Any]) -> list[str]:
    raw = row.get("policy_aliases") or row.get("aliases") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(a).strip() for a in raw if str(a).strip()]


def _state_keys(policy_number: str, aliases: list[str]) -> list[str]:
    keys = [_normalize_key(policy_number)]
    for alias in aliases:
        key = _normalize_key(alias)
        if key and key not in keys:
            keys.append(key)
    return keys


def _payload_policy_index(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Optional payload scoping: {"policies": [{"policy_number", "policy_aliases"}]}."""
    raw = payload.get("policies")
    if not isinstance(raw, list):
        return []
    index = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        number = _row_str(entry, "policy_number", "policyNumber")
        if not number:
            continue
        aliases = entry.get("policy_aliases") or entry.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        index.append({"policy_number": number, "aliases": [str(a) for a in aliases if a]})
    return index


def _row_matches_scope(row: dict[str, Any], scope: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Match a report row to a payload-scoped policy on any alias. None = no scope."""
    if not scope:
        return None
    row_numbers = {_normalize_key(_row_str(row, "policy_number", "policyNumber"))}
    row_numbers.update(_normalize_key(a) for a in _row_aliases(row))
    row_numbers.discard("")
    for entry in scope:
        candidates = {_normalize_key(entry["policy_number"])}
        candidates.update(_normalize_key(a) for a in entry["aliases"])
        candidates.discard("")
        if row_numbers & candidates:
            return entry
    return None


class ManualRenewalWorker:
    """Work the manual-renewal queue (report 4247) for one daily run.

    ``perform(job, *, idempotency_key) -> WorkerResult``. The job payload may
    carry: report_id (default "4247"), window_min_days (30), window_max_days
    (45), escalation_threshold_days (25), voice_enabled (default True),
    authorized_actions (list), db_path, reference_date, dry_run, policies
    (scoped policy list with aliases), policy_aliases (extra known aliases),
    csr_email_directory, carrier_phone_directory.
    """

    def __init__(
        self,
        *,
        directory: dict[str, str] | None = None,
        voice_dispatcher: Any | None = None,
        note_poster: Any | None = None,
        store: Any | None = None,
    ) -> None:
        """Keyword-only ports (registration-compatible with sibling workers).

        * ``directory``: extra carrier-name -> E.164 phone entries, merged
          under the payload ``carrier_phone_directory`` (carrier numbers
          only; never staff/client numbers).
        * ``voice_dispatcher``: optional port exposing
          ``dispatch_carrier_call(**kwargs)`` with the same kwargs and
          result-dict contract as :func:`dispatch_carrier_voice_call`. When
          None, the built-in Bland path is used (simulated unless
          ``VOICE_AI_API_KEY`` is configured).
        * ``note_poster``: optional port called with the same kwargs as the
          sibling ``post_ezlynx_note`` port. When None, the sibling port is
          used; when neither exists only the note intent is recorded.
        * ``store``: JobStore; its ``path`` is preferred for durable state
          when the payload carries no ``db_path``.
        """
        self._directory = dict(directory) if directory else None
        self._voice_dispatcher = voice_dispatcher
        self._note_poster = note_poster
        self._store = store

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        action_type = str(job.get("action_type") or "")
        if action_type != JOB_TYPE:
            return WorkerResult(
                False,
                action_type or JOB_TYPE,
                {},
                retryable=False,
                error=f"ManualRenewalWorker only handles {JOB_TYPE}",
            )
        payload = dict(job.get("payload") or {})
        today = _today(payload)
        report_id = str(payload.get("report_id") or REPORT_ID_DEFAULT)
        window_min = int(payload.get("window_min_days", WINDOW_MIN_DAYS_DEFAULT))
        window_max = int(payload.get("window_max_days", WINDOW_MAX_DAYS_DEFAULT))
        escalation_threshold = int(
            payload.get("escalation_threshold_days", ESCALATION_THRESHOLD_DAYS_DEFAULT)
        )
        db_path = _resolve_db_path(job, store=self._store)
        dry_run = bool(payload.get("dry_run", False))
        scope = _payload_policy_index(payload)

        detail: dict[str, Any] = {
            "worker": WORKER_NAME,
            "job_type": JOB_TYPE,
            "report_id": report_id,
            "window_days": [window_min, window_max],
            "escalation_threshold_days": escalation_threshold,
            "idempotency_key": idempotency_key,
            "dry_run": dry_run,
            "outcomes": [],
            "login_gaps": [],
            "counts": {"done": 0, "pending": 0, "not_done": 0},
        }

        # -- Fetch report rows (sibling module; never reimplemented here).
        if fetch_report_rows is None:
            return WorkerResult(
                False,
                JOB_TYPE,
                {"report_id": report_id, "worker": WORKER_NAME},
                detail=detail,
                retryable=False,
                error="report_fetcher.fetch_report_rows unavailable - cannot read report 4247",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        try:
            rows = fetch_report_rows(
                report_id=report_id,
                fields=REPORT_ROW_FIELDS,
                filters=None,
                db_path=db_path,
                session=None,
            )
        except Exception as exc:  # noqa: BLE001 - fetch failure is data
            return WorkerResult(
                False,
                JOB_TYPE,
                {"report_id": report_id, "worker": WORKER_NAME},
                detail={**detail, "fetch_error": f"{type(exc).__name__}: {exc}"},
                retryable=True,
                error=f"failed to fetch report {report_id} rows: {exc}",
            )
        if not isinstance(rows, list):
            return WorkerResult(
                False,
                JOB_TYPE,
                {"report_id": report_id, "worker": WORKER_NAME},
                detail=detail,
                retryable=True,
                error=f"fetch_report_rows returned {type(rows).__name__}, expected list[dict]",
            )

        ctx = {
            "job": job,
            "payload": payload,
            "today": today,
            "window_min": window_min,
            "window_max": window_max,
            "escalation_threshold": escalation_threshold,
            "db_path": db_path,
            "dry_run": dry_run,
            "scope": scope,
            "report_id": report_id,
            "worker": self,
        }
        rows_fetched = len(rows)
        rows_filtered = 0
        for row in rows:
            if not isinstance(row, dict):
                rows_filtered += 1
                continue
            if not _is_manual_source(row):
                rows_filtered += 1
                continue
            expiration = _parse_date(row.get("expiration_date") or row.get("expirationDate"))
            if expiration is None:
                rows_filtered += 1
                continue
            days_to_exp = (expiration - today).days
            if days_to_exp < 0 or days_to_exp > window_max:
                rows_filtered += 1
                continue
            if scope and _row_matches_scope(row, scope) is None:
                rows_filtered += 1
                continue
            try:
                outcome = _process_policy(row, ctx, expiration=expiration, days_to_exp=days_to_exp)
            except Exception as exc:  # noqa: BLE001 - per-policy isolation
                log.warning("policy processing failed for row: %s", exc)
                outcome = {
                    "policy_number": _row_str(row, "policy_number", "policyNumber") or "unknown",
                    "status": "not_done",
                    "reason": f"WORKER_EXCEPTION: {type(exc).__name__}: {exc}",
                    "actions_taken": [],
                    "waiting_on": None,
                    "evidence": {},
                    "updated_at": _utcnow(),
                }
            detail["outcomes"].append(outcome)
            status = outcome.get("status")
            if status in detail["counts"]:
                detail["counts"][status] += 1
            # Mirror every outcome to durable storage as it is produced.
            try:
                _mirror_outcomes_local([outcome], db_path=db_path, namespace=NAMESPACE)
            except Exception as exc:  # noqa: BLE001
                log.warning("durable mirror failed for %s: %s", outcome.get("policy_number"), exc)
            for gap in outcome.get("evidence", {}).get("login_gap_records", []):
                detail["login_gaps"].append(gap)

        detail["rows_fetched"] = rows_fetched
        detail["rows_in_window"] = len(detail["outcomes"])
        detail["rows_filtered"] = rows_filtered
        try:
            detail["record_outcomes"] = record_outcomes(
                job, detail["outcomes"], db_path=db_path, namespace=NAMESPACE
            )
        except Exception as exc:  # noqa: BLE001
            detail["record_outcomes"] = {"error": str(exc)}

        destination = {
            "report_id": report_id,
            "worker": WORKER_NAME,
            "sender": SENDER_EMAIL,
            "job_type": JOB_TYPE,
        }
        return WorkerResult(True, JOB_TYPE, destination, detail=detail)


def _process_policy(
    row: dict[str, Any],
    ctx: dict[str, Any],
    *,
    expiration: date,
    days_to_exp: int,
) -> dict[str, Any]:
    """Process one in-window policy row into a PolicyOutcome dict."""
    job = ctx["job"]
    payload = ctx["payload"]
    today: date = ctx["today"]
    db_path: str = ctx["db_path"]
    dry_run: bool = ctx["dry_run"]

    policy_number = _row_str(row, "policy_number", "policyNumber")
    insured_name = _row_str(row, "insured_name", "insuredName") or "Unknown insured"
    carrier_name = _row_str(row, "carrier_name", "carrierName", "carrier") or "Unknown carrier"
    lob = _row_str(row, "line_of_business", "lineOfBusiness") or "Commercial"
    applicant_id = _row_str(row, "applicant_id", "applicantId")
    assigned_agent = _row_str(row, "assigned_agent", "assignedAgent")
    department = _row_str(row, "department") or "Commercial Lines"
    aliases = _row_aliases(row)
    extra_aliases = payload.get("policy_aliases") or []
    if isinstance(extra_aliases, str):
        extra_aliases = [extra_aliases]
    aliases = aliases + [str(a) for a in extra_aliases if str(a).strip()]
    tracking_id = f"RENEWAL-REQ-{applicant_id or _normalize_key(policy_number) or 'unknown'}"

    outcome: dict[str, Any] = {
        "policy_number": policy_number,
        "policy_aliases": aliases,
        "applicant_id": applicant_id or None,
        "insured_name": insured_name,
        "department": department,
        "carrier": carrier_name,
        "line_of_business": lob,
        "expiration_date": expiration.isoformat(),
        "days_to_expiration": days_to_exp,
        "tracking_id": tracking_id,
        "status": "pending",
        "reason": "",
        "actions_taken": [],
        "waiting_on": None,
        "evidence": {},
        "next_action": None,
        "next_followup_due": None,
        "followup_count": 0,
        "initial_sent": False,
        "carrier_voice_attempted": False,
        "updated_at": _utcnow(),
    }
    note_ctx = {
        "policy_number": policy_number,
        "line_of_business": lob,
        "carrier": carrier_name,
        "insured_name": insured_name,
        "expiration_date": expiration.strftime("%m/%d/%Y"),
        "days_to_expiration": days_to_exp,
        "assigned_agent": assigned_agent,
        "tracking_id": tracking_id,
    }

    # -- 1. MANDATORY real-time cancellation gate (fail closed).
    ran, active, gate_reason = check_policy_cancellation(row)
    outcome["evidence"]["cancellation_gate"] = {"ran": ran, "active": active, "reason": gate_reason}
    outcome["actions_taken"].append("cancellation_check")
    if not ran:
        outcome["status"] = "not_done"
        outcome["reason"] = "CANCELLATION_CHECK_UNAVAILABLE - gate could not run; no outreach attempted"
        outcome["next_action"] = "retry cancellation check on the next run"
        return outcome
    if not active:
        outcome["status"] = "not_done"
        outcome["reason"] = "EXCLUDED_INACTIVE_ACCOUNT"
        outcome["evidence"]["cancellation_gate"]["exclusion_reason"] = gate_reason
        outcome["next_action"] = "none - inactive/cancelled policies are never contacted"
        return outcome

    # -- Prior cadence state (fresh read; never trust in-memory claims).
    prior = _read_durable_state(db_path, NAMESPACE, _state_keys(policy_number, aliases)) or {}
    outcome["followup_count"] = int(prior.get("followup_count", 0) or 0)
    outcome["carrier_voice_attempted"] = bool(prior.get("carrier_voice_attempted", False))
    outcome["next_followup_due"] = prior.get("next_followup_due")
    initial_sent = bool(prior.get("initial_sent", False))

    # -- Terms already in hand? Then today's responsibility is done.
    terms_in_hand = bool(
        row.get("renewal_terms_received") or row.get("terms_received") or row.get("renewal_premium")
    )
    if terms_in_hand:
        outcome["status"] = "done"
        outcome["reason"] = "TERMS_ALREADY_ON_FILE"
        outcome["actions_taken"].append("terms_verified_on_row")
        outcome["evidence"]["renewal_terms"] = {
            "renewal_premium": row.get("renewal_premium"),
            "source": "report_row",
        }
        outcome["next_action"] = "none - renewal terms already on file"
        return outcome

    # -- 2. Escalation band: <=25 days with no terms -> ESCALATED_MANUAL.
    if days_to_exp <= ctx["escalation_threshold"]:
        return _escalate_policy(job, row, outcome, note_ctx, ctx, dry_run=dry_run)

    # -- 3. Carrier routing.
    route = route_policy_carrier(carrier_name)
    channel = str(route.get("channel") or "UNKNOWN").upper()
    outcome["evidence"]["carrier_route"] = route
    outcome["actions_taken"].append(f"carrier_routed:{channel}")

    if channel == "PORTAL":
        return _process_portal_policy(job, row, outcome, note_ctx, ctx, route, dry_run=dry_run)
    if channel in ("EMAIL", "EMAIL_ASK_PORTAL"):
        return _process_email_policy(
            job, row, outcome, note_ctx, ctx, route,
            initial_sent=initial_sent, dry_run=dry_run,
        )
    outcome["status"] = "pending"
    outcome["reason"] = "CARRIER_NOT_ROUTED"
    outcome["waiting_on"] = "carrier"
    outcome["actions_taken"].append("routing_gap_noted")
    outcome["next_action"] = f"add '{carrier_name}' to the carrier routing directory"
    return outcome


def _escalate_policy(
    job: dict[str, Any],
    row: dict[str, Any],
    outcome: dict[str, Any],
    note_ctx: dict[str, Any],
    ctx: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """ESCALATED_MANUAL: <=25 days, no terms. Pending, owner CSR, urgent task intent."""
    days_to_exp = outcome["days_to_expiration"]
    outcome["status"] = "pending"
    outcome["reason"] = (
        f"ESCALATED_MANUAL - no renewal terms with {days_to_exp} days to expiration"
    )
    outcome["waiting_on"] = "csr"
    outcome["actions_taken"].append("csr_escalation")
    note_text = build_csr_escalation_note(
        {**note_ctx, "reason": f"No renewal quote received with {days_to_exp} days to expiration."}
    )
    note_evidence = post_ezlynx_note_intent(
        job,
        note_poster=_ctx_note_poster(ctx),
        note_text=note_text,
        applicant_id=outcome.get("applicant_id") or "",
        discussion_title=_row_str(row, "discussion_title") or "Manual Renewal",
        policy_number=outcome["policy_number"],
        line_of_business=outcome.get("line_of_business") or "",
        carrier_name=outcome.get("carrier") or "",
    )
    outcome["evidence"]["escalation_note"] = note_evidence
    task_intent = record_task_intent(
        job,
        title=f"URGENT Review Renewal ({days_to_exp}d to Exp): {outcome.get('insured_name')} ({outcome.get('carrier')})",
        description=(
            f"Policy #{outcome['policy_number']} expires {outcome.get('expiration_date')} "
            f"({days_to_exp} days remaining). No renewal proposal received. Assigned CSR: "
            f"{note_ctx.get('assigned_agent') or 'Unassigned'}."
        ),
        applicant_id=outcome.get("applicant_id") or "",
        assigned=note_ctx.get("assigned_agent"),
    )
    outcome["evidence"]["urgent_csr_task"] = task_intent
    outcome["actions_taken"].append("urgent_csr_task_intent_recorded")
    outcome["next_action"] = "CSR: high-priority follow-up with carrier underwriter / portal"
    return outcome


def _process_portal_policy(
    job: dict[str, Any],
    row: dict[str, Any],
    outcome: dict[str, Any],
    note_ctx: dict[str, Any],
    ctx: dict[str, Any],
    route: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """PORTAL channel: bounded retrieval stub at launch (crawlers are a later milestone)."""
    db_path: str = ctx["db_path"]
    attempt = attempt_portal_retrieval(job, row, route, db_path=db_path)
    outcome["actions_taken"].append("portal_attempted")
    outcome["evidence"]["portal_attempt"] = attempt["evidence"]
    if attempt["evidence"].get("login_gap"):
        outcome["evidence"].setdefault("login_gap_records", []).append(attempt["evidence"]["login_gap"])
    outcome["status"] = "pending"
    outcome["waiting_on"] = "carrier"
    outcome["reason"] = "portal retrieval deferred - per-carrier crawler milestone pending"
    outcome["next_action"] = (
        f"build {attempt['evidence'].get('portal_name')} crawler, then retrieve the renewal packet"
    )
    outcome["next_followup_due"] = (ctx["today"] + timedelta(days=7)).isoformat()
    note_evidence = post_ezlynx_note_intent(
        job,
        note_poster=_ctx_note_poster(ctx),
        note_text=build_portal_check_note(
            {**note_ctx, "portal_url": route.get("portal_url"), "portal_name": attempt["evidence"].get("portal_name"),
             "details": "Portal retrieval deferred until the per-carrier crawler milestone lands."}
        ),
        applicant_id=outcome.get("applicant_id") or "",
        discussion_title=_row_str(row, "discussion_title") or "Manual Renewal",
        policy_number=outcome["policy_number"],
        line_of_business=outcome.get("line_of_business") or "",
        carrier_name=outcome.get("carrier") or "",
    )
    outcome["evidence"]["portal_note"] = note_evidence
    return outcome


def _process_email_policy(
    job: dict[str, Any],
    row: dict[str, Any],
    outcome: dict[str, Any],
    note_ctx: dict[str, Any],
    ctx: dict[str, Any],
    route: dict[str, Any],
    *,
    initial_sent: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """EMAIL / EMAIL_ASK_PORTAL channel: initial email -> 5-7d cadence -> voice give-up."""
    today: date = ctx["today"]
    payload = ctx["payload"]
    expiration = _parse_date(outcome.get("expiration_date"))
    target_email = _row_str(row, "underwriter_email", "underwriterEmail") or str(
        route.get("underwriter_email") or ""
    ).strip()
    outcome["evidence"]["underwriter_email"] = target_email or None

    if not target_email:
        outcome["status"] = "pending"
        outcome["reason"] = "NO_UNDERWRITER_EMAIL"
        outcome["waiting_on"] = "carrier"
        outcome["actions_taken"].append("underwriter_email_missing")
        outcome["next_action"] = "resolve underwriter email for the carrier, then send outreach"
        return outcome

    csr_directory = payload.get("csr_email_directory") or CSR_EMAIL_DIRECTORY
    cc_list = resolve_outreach_cc_list(note_ctx.get("assigned_agent"), csr_directory)
    expiring_premium = row.get("expiring_premium") or row.get("expiringPremium")
    try:
        expiring_premium = float(expiring_premium) if expiring_premium is not None else None
    except (TypeError, ValueError):
        expiring_premium = None

    due_str = outcome.get("next_followup_due")
    due_date = _parse_date(due_str) if due_str else None
    followup_due = bool(initial_sent and due_date and due_date <= today)

    if not initial_sent and outcome["followup_count"] == 0:
        # -- Initial outreach email.
        subject = build_outreach_subject(
            outcome["tracking_id"], outcome["insured_name"], outcome["policy_number"], expiration
        )
        body = build_initial_outreach_body(
            underwriter_name=_row_str(row, "underwriter_name", "underwriterName"),
            insured_name=outcome["insured_name"],
            policy_num=outcome["policy_number"],
            carrier_name=outcome["carrier"],
            line_of_business=outcome.get("line_of_business") or "",
            expiration_date=expiration,
            expiring_premium=expiring_premium,
            ask_portal=str(route.get("channel") or "").upper() == "EMAIL_ASK_PORTAL",
        )
        return _send_outreach(
            job, row, outcome, note_ctx, ctx,
            to_email=target_email, cc_list=cc_list, subject=subject, body=body,
            action_name="send_underwriter_email", is_followup=False, followup_number=0,
            dry_run=dry_run,
        )

    if followup_due and outcome["followup_count"] < MAX_FOLLOWUPS:
        # -- 5-7 day cadence follow-up.
        next_count = outcome["followup_count"] + 1
        subject = "Re: " + build_outreach_subject(
            outcome["tracking_id"], outcome["insured_name"], outcome["policy_number"], expiration
        )
        body = build_followup_body(
            followup_number=next_count,
            underwriter_name=_row_str(row, "underwriter_name", "underwriterName"),
            insured_name=outcome["insured_name"],
            policy_num=outcome["policy_number"],
            expiration_date=expiration,
            days_to_expiration=outcome.get("days_to_expiration"),
        )
        return _send_outreach(
            job, row, outcome, note_ctx, ctx,
            to_email=target_email, cc_list=cc_list, subject=subject, body=body,
            action_name="send_followup_email", is_followup=True, followup_number=next_count,
            dry_run=dry_run,
        )

    if outcome["followup_count"] >= MAX_FOLLOWUPS:
        # -- Give-up branch: exactly one carrier voice call (once-only guard).
        return _process_voice_branch(job, row, outcome, note_ctx, ctx, dry_run=dry_run)

    # -- Follow-up not due yet: await the underwriter reply.
    outcome["status"] = "pending"
    outcome["waiting_on"] = "carrier"
    outcome["reason"] = (
        f"awaiting underwriter reply - follow-up due {due_date.isoformat() if due_date else 'n/a'}"
    )
    outcome["actions_taken"].append("cadence_wait")
    outcome["next_action"] = f"send follow-up on {due_date.isoformat() if due_date else 'n/a'} if no reply"
    return outcome


def _send_outreach(
    job: dict[str, Any],
    row: dict[str, Any],
    outcome: dict[str, Any],
    note_ctx: dict[str, Any],
    ctx: dict[str, Any],
    *,
    to_email: str,
    cc_list: list[str],
    subject: str,
    body: str,
    action_name: str,
    is_followup: bool,
    followup_number: int,
    dry_run: bool,
) -> dict[str, Any]:
    """Send (when authorized) or record (when not) an outreach email."""
    today: date = ctx["today"]
    kind = "follow-up" if is_followup else "initial outreach"
    intended = {"to": to_email, "cc": cc_list, "subject": subject, "from": SENDER_EMAIL}

    if dry_run:
        outcome["status"] = "pending"
        outcome["waiting_on"] = "carrier"
        outcome["reason"] = f"DRY_RUN - {kind} email not sent"
        outcome["actions_taken"].append(f"{kind}_email_intent_recorded")
        outcome["evidence"]["email_intent"] = intended
        outcome["next_action"] = f"send {kind} email on a live run"
        return outcome

    if not is_action_authorized(job, action_name):
        outcome["status"] = "pending"
        outcome["waiting_on"] = "carrier"
        outcome["reason"] = (
            f"EMAIL_NOT_AUTHORIZED - {kind} email recorded but not sent "
            f"(missing authorization: {action_name})"
        )
        outcome["actions_taken"].append(f"{kind}_email_intent_recorded")
        outcome["evidence"]["email_intent"] = intended
        outcome["next_action"] = f"authorize {action_name}, then send {kind} email to {to_email}"
        return outcome

    sent, info = send_outreach_email(to_email=to_email, cc=cc_list, subject=subject, body=body)
    if not sent:
        outcome["status"] = "pending"
        outcome["waiting_on"] = "carrier"
        outcome["reason"] = f"EMAIL_SEND_FAILED - {info.get('error', 'unknown mailer error')}"
        outcome["actions_taken"].append(f"{kind}_email_failed")
        outcome["evidence"]["email_error"] = info
        outcome["next_followup_due"] = (today + timedelta(days=1)).isoformat()
        outcome["next_action"] = "retry email send on the next run"
        return outcome

    # Sent.
    outcome["actions_taken"].append(f"{kind}_email_sent")
    outcome["evidence"]["email"] = {
        "to": to_email,
        "cc": cc_list,
        "subject": subject,
        "email_message_id": info.get("email_message_id"),
    }
    next_due = _next_followup_date(today)
    outcome["next_followup_due"] = next_due.isoformat()
    if is_followup:
        outcome["followup_count"] = followup_number
    else:
        outcome["initial_sent"] = True
    outcome["evidence"]["initial_sent"] = True
    outcome["status"] = "pending"
    outcome["waiting_on"] = "carrier"
    outcome["reason"] = (
        f"{'follow-up #' + str(followup_number) if is_followup else 'initial outreach'} "
        f"email sent - awaiting underwriter reply"
    )
    outcome["next_action"] = f"follow up on {next_due.isoformat()} if no reply"
    note_evidence = post_ezlynx_note_intent(
        job,
        note_poster=_ctx_note_poster(ctx),
        note_text=build_outreach_email_note(
            {
                **note_ctx,
                "recipient_email": to_email,
                "subject": subject,
                "tracking_id": outcome["tracking_id"],
                "is_followup": is_followup,
                "followup_number": followup_number,
                "next_followup_due": next_due.strftime("%m/%d/%Y"),
                "cc_list": cc_list,
            }
        ),
        applicant_id=outcome.get("applicant_id") or "",
        discussion_title=_row_str(row, "discussion_title") or "Manual Renewal",
        policy_number=outcome["policy_number"],
        line_of_business=outcome.get("line_of_business") or "",
        carrier_name=outcome.get("carrier") or "",
    )
    outcome["evidence"]["outreach_note"] = note_evidence
    return outcome


def _process_voice_branch(
    job: dict[str, Any],
    row: dict[str, Any],
    outcome: dict[str, Any],
    note_ctx: dict[str, Any],
    ctx: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Give-up branch mapping: voice dispatch result -> policy outcome."""
    expiration = _parse_date(outcome.get("expiration_date"))
    state = {
        "carrier_voice_attempted": outcome.get("carrier_voice_attempted", False),
        "followup_count": outcome.get("followup_count", 0),
    }
    branch = run_voice_giveup_branch(
        job,
        row,
        state,
        expiration=expiration,
        dry_run=dry_run,
        dispatcher=_ctx_voice_dispatcher(ctx),
        directory=_ctx_carrier_directory(ctx),
    )
    outcome["actions_taken"].append("voice_giveup_branch_evaluated")
    outcome["evidence"]["voice_branch"] = branch
    if branch.get("set_guard"):
        outcome["carrier_voice_attempted"] = True
        outcome["actions_taken"].append("carrier_voice_attempted_guard_set")

    result_kind = branch.get("outcome")
    if result_kind == "dispatched":
        dispatch = branch.get("dispatch") or {}
        outcome["status"] = "pending"
        outcome["waiting_on"] = "carrier"
        outcome["reason"] = branch.get("reason", "carrier voice call dispatched")
        outcome["actions_taken"].append("carrier_voice_call_placed")
        outcome["evidence"]["voice_call"] = {
            "call_id": dispatch.get("call_id"),
            "phone": dispatch.get("phone"),
            # The dial target can only come from the carrier phone directory
            # (resolve_carrier_voice_target); it is never a client number.
            "phone_from_directory": True,
            "mode": dispatch.get("mode"),
            "queue_status": dispatch.get("queue_status"),
            "call_placed": dispatch.get("call_placed"),
        }
        outcome["next_action"] = "monitor call completion; file transcript to the EZLynx discussion"
        note_evidence = post_ezlynx_note_intent(
            job,
            note_poster=_ctx_note_poster(ctx),
            note_text=build_carrier_voice_note(
                {
                    **note_ctx,
                    "phone": dispatch.get("phone"),
                    "call_id": dispatch.get("call_id"),
                    "status": dispatch.get("status"),
                    "attempts": MAX_FOLLOWUPS,
                }
            ),
            applicant_id=outcome.get("applicant_id") or "",
            discussion_title=_row_str(row, "discussion_title") or "Manual Renewal",
            policy_number=outcome["policy_number"],
            line_of_business=outcome.get("line_of_business") or "",
            carrier_name=outcome.get("carrier") or "",
        )
        outcome["evidence"]["voice_note"] = note_evidence
    elif result_kind == "phone_needed":
        outcome["status"] = "pending"
        outcome["waiting_on"] = "carrier"
        outcome["reason"] = branch.get("reason", "no verified carrier phone on file")
        outcome["actions_taken"].append("carrier_phone_missing_noted")
        outcome["next_action"] = "add a verified carrier underwriter number to the voice call directory"
        note_evidence = post_ezlynx_note_intent(
            job,
            note_poster=_ctx_note_poster(ctx),
            note_text=build_voice_phone_needed_note(note_ctx),
            applicant_id=outcome.get("applicant_id") or "",
            discussion_title=_row_str(row, "discussion_title") or "Manual Renewal",
            policy_number=outcome["policy_number"],
            line_of_business=outcome.get("line_of_business") or "",
            carrier_name=outcome.get("carrier") or "",
        )
        outcome["evidence"]["voice_note"] = note_evidence
    else:
        # already_attempted / deferred / simulated / failed
        outcome["status"] = "pending"
        outcome["waiting_on"] = "carrier"
        outcome["reason"] = branch.get("reason", "voice branch deferred")
        outcome["actions_taken"].append(f"voice_branch_{result_kind}")
        outcome["next_action"] = "await carrier response or CSR follow-up"
    return outcome

# ---------------------------------------------------------------------------
# ManualRenewalVerifier - independent fresh read-back
# ---------------------------------------------------------------------------

VALID_WAITING_ON = frozenset({"carrier", "csr"})

# Evidence keys that count as proof for a "done" outcome. At least one must be
# present (truthy) somewhere inside the outcome's evidence tree.
DONE_PROOF_KEYS = frozenset(
    {
        "ezlynx_note_id",
        "document_path",
        "email_message_id",
        "message_id",
        "gmail_message_id",
    }
)


def _evidence_has_done_proof(evidence: Any) -> bool:
    """True when the evidence tree carries at least one done-proof reference."""
    stack = [evidence]
    seen = 0
    while stack and seen < 200:
        node = stack.pop()
        seen += 1
        if isinstance(node, dict):
            for key, value in node.items():
                if key in DONE_PROOF_KEYS and value:
                    return True
                stack.append(value)
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return False


def _valid_updated_at(value: Any) -> bool:
    if not value or not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


class ManualRenewalVerifier:
    """Independently verify manual_renewal_verification outcomes.

    Fresh read-back only: the verifier ignores in-memory worker claims and
    re-reads (1) the job's ``action`` checkpoint from the job DB and (2) each
    outcome's ``durable_work_items`` row (namespace
    ``manual_renewal_verification``). Every outcome needs a policy number, a
    valid status, a nonempty reason, and a valid ``updated_at``; ``done``
    outcomes need evidence carrying at least one of ``ezlynx_note_id``,
    ``document_path``, or an email message id. A voice call recorded as
    placed while ``voice_enabled`` was false, or without proof the number came
    from the carrier directory, is a violation. Malformed or missing data
    returns unverified with specific failures. The worker never claims job
    completion; only the engine, on a verified result, may do that.
    """

    def __init__(
        self,
        *,
        store: Any | None = None,
        durable: Any | None = None,
    ) -> None:
        self._store = store
        self._durable = durable

    def _fresh_action(self, job: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        """Return (fresh action checkpoint, authoritative)."""
        if self._store is None:
            return None, False
        try:
            fresh = self._store.get_checkpoint(job.get("id"), "action")
        except Exception:
            return None, False
        if not isinstance(fresh, dict):
            return None, False
        return fresh, True

    def _durable_get(self, namespace: str, key: str) -> dict[str, Any] | None:
        if self._durable is not None:
            try:
                return self._durable(namespace, key)
            except Exception:
                return None
        store = self._store
        db_path = getattr(store, "path", None)
        if not db_path:
            return None
        try:
            from .idempotency import DurableWorkLedger

            return DurableWorkLedger(db_path).get(namespace, key)
        except Exception:
            return None

    @staticmethod
    def _durable_outcome_dict(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(row, dict):
            return None
        outcome = row.get("outcome")
        if isinstance(outcome, str):
            try:
                outcome = json.loads(outcome)
            except (ValueError, TypeError):
                return None
        return outcome if isinstance(outcome, dict) else None

    def verify(self, job: dict[str, Any], action: dict[str, Any]) -> VerificationResult:
        job_id = job.get("id")
        action_type = str(job.get("action_type") or "")
        captured_at = datetime.now(timezone.utc).isoformat()
        violations: list[str] = []
        observed: dict[str, Any] = {"job_id": job_id, "action_type": action_type}
        # Track per-check outcomes for the expected/observed evidence contract.
        # These feed observed["all_outcomes_well_formed"] etc. below; they must
        # never be left unset (None) or the COMPLETE gate cannot evaluate them.
        well_formed_ok = True
        done_evidence_ok = True
        voice_ok = True
        mirror_ok = True

        if action_type and action_type != JOB_TYPE:
            violations.append(
                f"job action_type {action_type!r} is not {JOB_TYPE!r}"
            )

        fresh, authoritative = self._fresh_action(job)
        if self._store is not None and fresh is None:
            violations.append("action checkpoint missing on fresh store read-back")
        if fresh is None:
            fresh = action if isinstance(action, dict) else {}
            if self._store is None:
                violations.append(
                    "no store wired - verifier fell back to the passed action snapshot"
                )
        detail = dict(fresh.get("detail") or {})
        outcomes = detail.get("outcomes")
        if not isinstance(outcomes, list) or not outcomes:
            violations.append("no per-policy outcomes in the action checkpoint detail")
            outcomes = []
        observed["outcome_count"] = len(outcomes)
        voice_enabled = bool(detail.get("voice_enabled", VOICE_ENABLED_DEFAULT))
        observed["voice_enabled"] = voice_enabled
        observed["report_id"] = detail.get("report_id")

        checked_keys: list[str] = []
        for index, item in enumerate(outcomes):
            label = f"outcomes[{index}]"
            if not isinstance(item, dict):
                violations.append(f"{label} is not an object")
                well_formed_ok = False
                continue
            policy_number = item.get("policy_number")
            identity = policy_number or f"#{index}"
            if not policy_number:
                violations.append(f"{label} missing policy_number")
                well_formed_ok = False
            status = item.get("status")
            if status not in VALID_OUTCOME_STATUSES:
                violations.append(
                    f"{label} ({identity}) has invalid status {status!r}"
                )
                well_formed_ok = False
            if not item.get("reason"):
                violations.append(f"{label} ({identity}) missing reason")
                well_formed_ok = False
            if not _valid_updated_at(item.get("updated_at")):
                violations.append(
                    f"{label} ({identity}) missing or invalid updated_at"
                )
                well_formed_ok = False
            waiting_on = item.get("waiting_on")
            if waiting_on is not None and waiting_on not in VALID_WAITING_ON:
                violations.append(
                    f"{label} ({identity}) has invalid waiting_on {waiting_on!r}"
                )
                well_formed_ok = False
            evidence = item.get("evidence")
            if status == "done" and not _evidence_has_done_proof(evidence):
                violations.append(
                    f"{label} ({identity}) is done but carries no evidence ref "
                    "(need ezlynx_note_id, document_path, or an email message id)"
                )
                well_formed_ok = False
                done_evidence_ok = False
            # Voice authorization + directory-proof checks.
            if isinstance(evidence, dict):
                voice = evidence.get("voice_call") or {}
                if isinstance(voice, dict) and voice.get("call_placed"):
                    if not voice_enabled:
                        violations.append(
                            f"{label} ({identity}) records a placed voice call "
                            "while voice_enabled was false"
                        )
                        voice_ok = False
                    if not voice.get("phone_from_directory"):
                        violations.append(
                            f"{label} ({identity}) records a placed voice call "
                            "without proof the number came from the carrier directory"
                        )
                        voice_ok = False
            # Durable mirror check (fresh read, never the worker's memory).
            key = _normalize_key(policy_number) if policy_number else ""
            checked_keys.append(key)
            if not key:
                violations.append(
                    f"{label} has no policy_number - durable mirror cannot be located"
                )
                mirror_ok = False
                continue
            durable_row = self._durable_get(NAMESPACE, key)
            durable_outcome = self._durable_outcome_dict(durable_row)
            if durable_outcome is None:
                violations.append(
                    f"{label} ({identity}) missing durable_work_items mirror "
                    f"(namespace {NAMESPACE!r})"
                )
                mirror_ok = False
            elif durable_outcome.get("status") != status:
                violations.append(
                    f"{label} ({identity}) status {status!r} != durable mirror "
                    f"{durable_outcome.get('status')!r}"
                )
                mirror_ok = False
        observed["checked_keys"] = checked_keys
        observed["violations"] = violations
        # Populate the expected/observed contract flags. These must always be
        # set (never None) so the COMPLETE gate can evaluate them.
        observed["all_outcomes_well_formed"] = well_formed_ok and bool(outcomes)
        observed["done_outcomes_have_evidence"] = done_evidence_ok
        observed["no_unauthorized_voice"] = voice_ok
        observed["durable_mirror_present"] = mirror_ok and bool(outcomes)

        verified = not violations
        evidence_obj = VerificationEvidence(
            method="FRESH_CHECKPOINT_AND_DURABLE_READBACK",
            source="manual_renewal_verification",
            expected={
                "report_id": REPORT_ID_DEFAULT,
                "outcome_count": len(outcomes),
                "all_outcomes_well_formed": True,
                "done_outcomes_have_evidence": True,
                "no_unauthorized_voice": True,
                "durable_mirror_present": True,
            },
            observed=observed,
            authoritative=authoritative and verified,
            captured_at=captured_at,
            locator=f"{REPORT_ID_DEFAULT}/{JOB_TYPE}",
        )
        return VerificationResult(
            verified,
            evidence_obj,
            retryable=False,
            error=None if verified else "; ".join(violations),
        )
