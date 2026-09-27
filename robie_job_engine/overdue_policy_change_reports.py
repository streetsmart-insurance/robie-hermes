"""Overdue policy-change (EZLynx report 4359) CSR status-update reports.

Mirrors the Submission Center overdue worker pattern (fail-closed contract,
approved-roster recipient resolution, one email per person working the queue,
destination-verified receipts) for the Policy Change Request Confirmation
Queue (report 4359).

Pipeline (all API, no browser):
  1. Daily 4359 CSV from Gmail (subject "ROBIE daily CSV - 4359 Policy Change").
  2. Qualify: Request Status = Open AND age > 14 days (Carlo's standing bar).
  3. Liveness gate per policy number via PolicyApi read-back:
     exact match first, applicant/account-anchored; de-concatenated variant
     fallback (e.g. queue "BDG-312624001" -> live "BDG-3126240-02"); dead
     policies excluded; no-result/ambiguous items go to HOLD, never emailed.
  4. DiscussionApi context per applicant (title / noteCount / lastModified
     only — note BODIES are not API-readable): discussion title, note count,
     days since last activity. Missing discussion -> "no discussion found";
     zero notes -> "no notes yet"; per-applicant lookup failure -> item
     marked UNVERIFIED. If the Discussion API itself is down, the whole
     run fails closed and nothing is sent.
  5. Email the CSR (the "CSR" column), CC the Department Manager of the
     CSR's Department + carlo@streetsmart.insurance, asking for a status
     update. Plain English, extremely concise.
  6. Dedupe: notification key = sha256(CSR + policy number + created date);
     re-nag only when still open after RENAG_DAYS (7).

Dry-run safe: read-only everywhere (Gmail read, PolicyApi search,
DiscussionApi list, Sheets roster read). The only write is the outbound
email. Note-body inspection needs the browser and is a documented future
enhancement, not part of this worker.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import logging
import os
import re
import urllib.parse as parse
import urllib.request as urlrequest
from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .models import JobStatus, WorkerResult

JOB_TYPE = "ezlynx.overdue_policy_change_reports"
ACTION = "send_csr_reports"
RESOURCE_ID = "ezlynx:report:4359:policy-change"
REPORT_KEY = "4359"
REPORT_SUBJECT_LABEL = "4359"
REPORT_MAILBOX = "robie@streetsmart.insurance"
OVERDUE_DAYS = 14
RENAG_DAYS = 7
AGENCY_EMAIL_SUFFIX = "@streetsmart.insurance"
CC_CARLO = "carlo@streetsmart.insurance"
SENDER = "robie@streetsmart.insurance"
SUBJECT = "Action needed: policy change requests waiting on your update"
# HOST-ONLY DiscussionApi base. Never derive this from document_base_url —
# that produces .../DocumentApi/DiscussionApi/ and 404s every call.
DISCUSSION_BASE_URL = "https://app.ezlynx.com/DiscussionApi/"
POLICY_API_BASE_URL = "https://app.ezlynx.com/PolicyApi/"
ROSTER_SPREADSHEET_ID = "1ZyX1rUzDmRLMLMGrRzS48BlwvUdfkBd060ekZqoKsvo"
ROSTER_TAB = "Employees"
logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = [
    "Account Name",
    "Applicant ID",
    "Policy Number",
    "Line Of Business",
    "Effective Date",
    "Master Company",
    "Request Status",
    "Created By",
    "Written Premium",
    "Premium - Annualized",
    "Branch",
    "Department",
    "Service Team",
    "Assigned Producer",
    "CSR",
    "Preferred Language",
    "Applicant Labels",
    "Policy Labels",
    "Change Request Created Date",
]

DEAD_POLICY_STATUSES = {"cancelled", "canceled", "deleted", "expired", "inactive"}


class PolicyChangeReportContractError(RuntimeError):
    """Queue, liveness, roster, or discussion evidence did not satisfy the send contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _name_key(name: str) -> str:
    return " ".join(str(name or "").casefold().split())


# -- queue ingestion --------------------------------------------------------


def parse_4359_rows(rows: list[list[Any]]) -> list[dict[str, str]]:
    """Validate the 19-column 4359 schema and return row dicts.

    Raises PolicyChangeReportContractError on header mismatch or ragged rows.
    """
    if not rows:
        raise PolicyChangeReportContractError("4359 report has no rows")
    headers = [str(cell or "").strip() for cell in rows[0]]
    missing = [column for column in REQUIRED_COLUMNS if column not in headers]
    if missing:
        raise PolicyChangeReportContractError(
            "4359 report is missing required columns: " + ", ".join(missing)
        )
    records: list[dict[str, str]] = []
    for lineno, raw in enumerate(rows[1:], start=2):
        cells = [str(cell or "").strip() for cell in raw]
        if all(not cell for cell in cells):
            continue
        if len(cells) != len(headers):
            raise PolicyChangeReportContractError(
                f"4359 report row {lineno} has {len(cells)} cells, expected {len(headers)}"
            )
        record = dict(zip(headers, cells))
        records.append({column: record.get(column, "") for column in REQUIRED_COLUMNS})
    return records


def parse_4359_csv(content: bytes) -> list[dict[str, str]]:
    text = content.decode("utf-8-sig")
    return parse_4359_rows(list(csv.reader(io.StringIO(text))))


def row_created_date(row: Mapping[str, Any]) -> date:
    raw = str(row.get("Change Request Created Date") or "").strip()
    if not raw:
        raise PolicyChangeReportContractError(
            "4359 row has a blank Change Request Created Date "
            f"(policy {row.get('Policy Number') or 'unknown'})"
        )
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            continue
    raise PolicyChangeReportContractError(
        f"4359 row has an unreadable Change Request Created Date: {raw!r}"
    )


def qualify_rows(rows: list[dict[str, str]], today: date) -> list[dict[str, Any]]:
    """Open requests older than Carlo's 14-day bar, with age attached."""
    qualified: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("Request Status") or "").strip().casefold() != "open":
            continue
        created = row_created_date(row)
        age = (today - created).days
        if age < 0:
            raise PolicyChangeReportContractError(
                f"4359 row created date is in the future: {created.isoformat()}"
            )
        if age > OVERDUE_DAYS:
            qualified.append({**row, "created_date": created.isoformat(), "age_days": age})
    return qualified


# -- PolicyApi liveness gate -------------------------------------------------


def normalize_policy_number(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def deconcatenated_variants(policy_number: str) -> list[str]:
    """Queue numbers are sometimes concatenations: base + "01"/"02".

    Proven case: queue "BDG-312624001" is not a real policy; the live
    policies are "BDG-3126240-01" / "BDG-3126240-02". A naive exact search
    matched a DELETED suffixed variant instead. Try the de-concatenated
    forms before concluding anything.
    """
    number = normalize_policy_number(policy_number)
    match = re.fullmatch(r"(.*?)(0[12])", number)
    if not match:
        return []
    base = match.group(1).rstrip("-")
    variants = [f"{base}-01", f"{base}-02"]
    return [variant for variant in variants if variant != number]


def _policy_expiration(row: Mapping[str, Any]) -> str:
    return str(row.get("expirationDate") or row.get("expiration_date") or "")[:10]


def _is_dead_policy(row: Mapping[str, Any], today: date) -> bool:
    status = str(row.get("policyStatus") or row.get("status") or "").strip().casefold()
    if status in DEAD_POLICY_STATUSES:
        return True
    expiration = _policy_expiration(row)
    if expiration:
        try:
            if datetime.strptime(expiration, "%Y-%m-%d").date() < today:
                return True
        except ValueError:
            pass
    return False


def classify_policy_liveness(
    policy_search: Callable[[str], list[dict[str, Any]]],
    policy_number: str,
    applicant_id: str,
    today: date,
) -> dict[str, Any]:
    """LIVE / DEAD / HOLD verdict for one queue row.

    Exact policy-number match first, anchored to the queue's Applicant ID.
    De-concatenated variants only when no exact live result. Never
    fuzzy-matches deleted suffixed variants. Zero results or ambiguity ->
    HOLD (flagged, never emailed, never auto-closed).
    """
    number = normalize_policy_number(policy_number)
    applicant = str(applicant_id or "").strip()
    if not number:
        return {"verdict": "HOLD", "reason": "blank policy number"}
    if not applicant:
        return {"verdict": "HOLD", "reason": "blank applicant id"}

    def account_ok(row: Mapping[str, Any]) -> bool:
        return str(row.get("accountId") or row.get("account_id") or "").strip() == applicant

    def is_active(row: Mapping[str, Any]) -> bool:
        return str(row.get("policyStatus") or row.get("status") or "").strip().casefold() == "active"

    def summarize(row: Mapping[str, Any], via: str) -> dict[str, Any]:
        return {
            "verdict": "LIVE",
            "reason": via,
            "matched_policy_number": normalize_policy_number(row.get("policyNumber") or row.get("policy_number")),
            "policy_status": str(row.get("policyStatus") or row.get("status") or "").strip(),
            "expiration_date": _policy_expiration(row),
            "premium": row.get("premium"),
        }

    exact_search_rows = policy_search(number)
    exact = [
        row for row in exact_search_rows
        if normalize_policy_number(row.get("policyNumber") or row.get("policy_number")) == number
    ]
    # A live verdict requires Active status, applicant-account anchor, and
    # not dead by status or expiration. (An "Active" row past its
    # expiration date is stale API data, not a live policy.)
    live_exact = [
        row for row in exact
        if is_active(row) and account_ok(row) and not _is_dead_policy(row, today)
    ]
    if live_exact:
        return summarize(live_exact[0], "exact policy-number match, applicant account confirmed")

    variant_numbers = {normalize_policy_number(variant) for variant in deconcatenated_variants(number)}
    variant_search_rows: list[dict[str, Any]] = []
    for variant in deconcatenated_variants(number):
        variant_search_rows.extend(policy_search(variant))
    live_variant = [
        row for row in variant_search_rows
        if is_active(row) and account_ok(row) and not _is_dead_policy(row, today)
        and normalize_policy_number(row.get("policyNumber") or row.get("policy_number")) in variant_numbers
    ]
    if live_variant:
        return summarize(live_variant[0], "live policy found via de-concatenated variant")

    if exact and all(_is_dead_policy(row, today) for row in exact):
        return {
            "verdict": "DEAD",
            "reason": "exact policy-number match is dead ("
            + ", ".join(sorted({str(row.get("policyStatus") or row.get("status") or "?") for row in exact}))
            + ")",
        }
    all_rows = exact_search_rows + variant_search_rows
    if all_rows and all(_is_dead_policy(row, today) for row in all_rows):
        # Only deleted history variants (e.g. "CX132093_15195201") came
        # back and no live policy was found under the exact number or any
        # de-concatenated variant. This matches the manual trial's "proven
        # dead" verdict. Consequence here is only exclusion from CSR nags
        # plus a cleanup-candidate count — this worker never closes
        # anything, so a wrong DEAD here costs a human review, not data.
        return {
            "verdict": "DEAD",
            "reason": "only deleted history variants returned; no live policy found",
        }
    if not all_rows:
        return {"verdict": "HOLD", "reason": "PolicyApi returned no results for this policy number"}
    return {"verdict": "HOLD", "reason": "policy identity is ambiguous (account mismatch or no live match)"}


# -- DiscussionApi context ---------------------------------------------------

PCR_TITLE_HINT = "policy change request"


def select_change_discussion(
    discussions: list[dict[str, Any]], policy_number: str
) -> dict[str, Any] | None:
    """Pick the discussion most likely to be the change request thread.

    Matches the policy-number digits in the title, else the first
    "Policy Change Request" discussion; most recently active wins.
    """
    digits = re.sub(r"\D", "", str(policy_number or ""))

    def title_of(disc: Mapping[str, Any]) -> str:
        return str(disc.get("title") or "")

    def last_modified_of(disc: Mapping[str, Any]) -> str:
        return str(disc.get("lastModified") or disc.get("last_modified") or "")

    def by_recency(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(pool, key=last_modified_of, reverse=True)

    if digits:
        digit_hits = [
            disc for disc in discussions
            if digits and digits in re.sub(r"\D", "", title_of(disc))
        ]
        if digit_hits:
            return by_recency(digit_hits)[0]
    pcr_hits = [
        disc for disc in discussions if PCR_TITLE_HINT in title_of(disc).casefold()
    ]
    if pcr_hits:
        return by_recency(pcr_hits)[0]
    return None


def _days_since_last_activity(discussion: Mapping[str, Any], today: date) -> int | None:
    raw = str(discussion.get("lastModified") or discussion.get("last_modified") or "").strip()
    if not raw:
        return None
    stamp = raw[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return (today - datetime.strptime(stamp, fmt).date()).days
        except ValueError:
            continue
    return None


def discussion_context_line(discussion: Mapping[str, Any] | None, today: date) -> str:
    """One-line, plain-English activity signal for the CSR email."""
    if discussion is None:
        return "No EZLynx discussion found for this change request."
    title = str(discussion.get("title") or "Untitled discussion").strip()
    count = discussion.get("noteCount", discussion.get("note_count"))
    try:
        notes = int(count) if count is not None else None
    except (TypeError, ValueError):
        notes = None
    if not notes:
        return f'Discussion "{title}": no notes yet.'
    days = _days_since_last_activity(discussion, today)
    if days is None:
        return f'Discussion "{title}": {notes} notes, last activity date unknown.'
    if days == 0:
        when = "today"
    elif days == 1:
        when = "yesterday"
    else:
        when = f"{days} days ago"
    return f'Discussion "{title}": {notes} notes, last activity {when}.'


# -- roster: CSR directory + department managers ------------------------------


def _roster_skip_reason(name: str, raw: Mapping[str, Any] | None) -> str | None:
    details = dict(raw or {})
    email = str(details.get("email") or "").strip().casefold()
    display = " ".join(str(name).split()) or "unnamed"
    if not email:
        return f"{display}: missing work email"
    if not email.endswith(AGENCY_EMAIL_SUFFIX):
        return f"{display}: non-agency work email ({email})"
    return None


def build_roster_maps(registry: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """CSR directory + department-manager map from the approved roster.

    Fail-closed: roster unavailable/partial, ambiguous employee names, or a
    department with no Department Manager all raise the contract error.
    """
    if registry.get("source_status") != "available":
        raise PolicyChangeReportContractError(
            "approved active-employee roster is unavailable or partial"
        )
    employees = dict(registry.get("employees") or {})
    directory: dict[str, str] = {}
    departments: dict[str, str] = {}
    managers: dict[str, dict[str, str]] = {}
    for name, raw in employees.items():
        details = dict(raw or {})
        key = _name_key(name)
        skip_reason = _roster_skip_reason(name, details)
        if skip_reason:
            logger.warning("Skipping roster row from CSR directory: %s", skip_reason)
            continue
        if not key or key in directory:
            raise PolicyChangeReportContractError(
                "approved roster contains an ambiguous employee name"
            )
        directory[key] = str(details.get("email") or "").strip().casefold()
        departments[key] = str(details.get("department") or "").strip()
        role = str(details.get("role") or "")
        if "department manager" in role.casefold():
            dept_key = _name_key(departments[key])
            if dept_key and dept_key in managers:
                raise PolicyChangeReportContractError(
                    f"approved roster has more than one department manager for {departments[key]}"
                )
            if dept_key:
                managers[dept_key] = {"name": " ".join(str(name).split()), "email": directory[key]}
    if not directory:
        raise PolicyChangeReportContractError("approved roster contains no active employees")
    return {"directory": directory, "departments": departments, "managers": managers}


def load_approved_csr_directory(manifest_path: str) -> dict[str, dict[str, str]]:
    """Fetch the approved employee roster and build CSR/manager maps."""
    path = Path(manifest_path).expanduser().resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    config = dict(manifest.get("google_sheets") or {})
    if not config.get("enabled"):
        raise PolicyChangeReportContractError("approved active-employee roster is not enabled")
    from .google_sheets_accountability import (
        SheetsRosterAccessError,
        classify_sheets_auth_error,
        collect_allowlisted_tables,
        role_registry_from_snapshot,
    )

    try:
        snapshot = collect_allowlisted_tables(config, as_of=datetime.now(timezone.utc))
        registry = role_registry_from_snapshot(snapshot, config)
    except SheetsRosterAccessError as exc:
        raise PolicyChangeReportContractError(str(exc)) from exc
    except PolicyChangeReportContractError:
        raise
    except Exception as exc:
        raise PolicyChangeReportContractError(str(classify_sheets_auth_error(exc))) from exc
    return build_roster_maps(registry)


def resolve_nag_targets(
    items: list[dict[str, Any]], roster: Mapping[str, Mapping[str, str]]
) -> dict[str, dict[str, Any]]:
    """Group items by CSR; resolve CSR mailbox + department manager.

    Fail-closed: blank CSR, unresolvable CSR mailbox, or no department
    manager for the CSR's department raises the contract error and nothing
    is emailed.
    """
    directory = dict(roster.get("directory") or {})
    departments = dict(roster.get("departments") or {})
    managers = dict(roster.get("managers") or {})
    targets: dict[str, dict[str, Any]] = {}
    missing_csrs: list[str] = []
    missing_managers: list[str] = []
    for item in items:
        csr = str(item.get("CSR") or "").strip()
        if not csr:
            raise PolicyChangeReportContractError(
                f"qualifying 4359 row has no CSR (policy {item.get('Policy Number') or 'unknown'})"
            )
        key = _name_key(csr)
        email = directory.get(key, "")
        if not email:
            missing_csrs.append(csr)
            continue
        dept = departments.get(key, "")
        manager = managers.get(_name_key(dept))
        if not manager:
            missing_managers.append(f"{csr} (department: {dept or 'unknown'})")
            continue
        entry = targets.setdefault(
            csr,
            {"email": email, "manager_name": manager["name"],
             "manager_email": manager["email"], "items": []},
        )
        entry["items"].append(item)
    if missing_csrs:
        raise PolicyChangeReportContractError(
            "CSR work email could not be resolved for: " + ", ".join(sorted(set(missing_csrs)))
        )
    if missing_managers:
        raise PolicyChangeReportContractError(
            "no department manager found for: " + ", ".join(sorted(set(missing_managers)))
        )
    return targets


# -- dedupe: don't re-nag ------------------------------------------------------

NOTIFICATION_KEY_FIELDS = ("CSR", "Policy Number", "created_date")


def notification_key(csr: str, policy_number: str, created_date: str) -> str:
    return _canonical_hash({
        "csr": _name_key(csr),
        "policy_number": normalize_policy_number(policy_number),
        "created_date": str(created_date or "").strip(),
    })


class NotificationStore:
    """Persists sent-notification dates so reruns don't re-nag.

    Re-nag only when the change is still open after RENAG_DAYS.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (ValueError, OSError):
            data = {}
        self._sent: dict[str, str] = dict(data or {})

    def is_due(self, item: Mapping[str, Any], today: date) -> bool:
        key = notification_key(
            str(item.get("CSR") or ""),
            str(item.get("Policy Number") or ""),
            str(item.get("created_date") or ""),
        )
        last = self._sent.get(key)
        if not last:
            return True
        try:
            return (today - datetime.strptime(last[:10], "%Y-%m-%d").date()).days >= RENAG_DAYS
        except ValueError:
            return True

    def mark_sent(self, item: Mapping[str, Any], today: date) -> None:
        key = notification_key(
            str(item.get("CSR") or ""),
            str(item.get("Policy Number") or ""),
            str(item.get("created_date") or ""),
        )
        self._sent[key] = today.isoformat()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._sent, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


# -- email ---------------------------------------------------------------------


def _item_bullets(items: list[Mapping[str, Any]], today: date) -> list[str]:
    bullets = []
    for item in sorted(items, key=lambda row: int(row.get("age_days") or 0), reverse=True):
        account = str(item.get("Account Name") or "Unknown account").strip()
        policy = str(item.get("Policy Number") or "").strip()
        carrier = str(item.get("Master Company") or "").strip()
        lob = str(item.get("Line Of Business") or "").strip()
        created = str(item.get("created_date") or "").strip()
        age = int(item.get("age_days") or 0)
        head = f"{account} — policy {policy}"
        detail = ", ".join(part for part in (carrier, lob) if part)
        if detail:
            head += f" ({detail})"
        head += f". Request opened {created} ({age} days ago)."
        if item.get("discussion_unverified"):
            bullets.append(head + " Discussion activity: UNVERIFIED (lookup failed).")
        else:
            bullets.append(head + " " + discussion_context_line(item.get("discussion"), today))
    return bullets


def build_csr_report(csr: str, items: list[Mapping[str, Any]], manager_name: str, today: date) -> str:
    first = " ".join(str(csr).split()).split(" ")[0]
    lines = [
        f"Hi {first},",
        "",
        "The following policy change requests are still open and waiting on an update:",
        "",
        *[f"- {bullet}" for bullet in _item_bullets(items, today)],
        "",
        "Please reply with a quick status update on each one — what's done, what's blocked, and what it needs next.",
        "",
        f"CC'ing {manager_name} so they're in the loop.",
        "",
        "-ROBIE AI on behalf of Carlo",
    ]
    return "\n".join(lines)


def build_csr_report_html(csr: str, items: list[Mapping[str, Any]], manager_name: str, today: date) -> str:
    bullets = "".join(f"<li>{html.escape(bullet)}</li>" for bullet in _item_bullets(items, today))
    return "\n".join([
        "<div>",
        f"<p>Hi {html.escape(' '.join(str(csr).split()).split(' ')[0])},</p>",
        "<p>The following policy change requests are still open and waiting on an update:</p>",
        f"<ul>{bullets}</ul>",
        "<p>Please reply with a quick status update on each one — what's done, what's blocked, and what it needs next.</p>",
        f"<p>CC'ing {html.escape(manager_name)} so they're in the loop.</p>",
        "<p>-ROBIE AI on behalf of Carlo</p>",
        "</div>",
    ])


# -- default injected dependencies (all replaceable in tests) ------------------


def default_queue_reader(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """Read the newest 4359 CSV from the report mailbox via scheduled-report sync."""
    from .ringcentral_email_sync import build_keyless_report_mailbox_service
    from .scheduled_report_email_sync import (
        ScheduledReportEvidenceError,
        collect_scheduled_tabular_reports,
    )

    mailbox = str(payload.get("report_mailbox") or REPORT_MAILBOX).strip()
    service_account = os.environ.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", "").strip()
    if not service_account:
        raise PolicyChangeReportContractError(
            "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT is not configured"
        )
    output_dir = Path(str(payload.get("report_output_dir") or "/tmp/overdue-policy-change-reports")).expanduser()
    config = {
        "max_age_hours": int(payload.get("report_max_age_hours") or 48),
        "allowed_senders": [str(s) for s in (payload.get("report_allowed_senders") or []) if str(s).strip()],
        "allowed_sender_domains": [
            str(d) for d in (payload.get("report_allowed_sender_domains") or ["ezlynx.com"]) if str(d).strip()
        ],
        "reports": {
            REPORT_KEY: {"label": REPORT_SUBJECT_LABEL, "required_columns": REQUIRED_COLUMNS},
        },
    }
    service = build_keyless_report_mailbox_service(service_account, mailbox)
    try:
        collected = collect_scheduled_tabular_reports(service, output_dir=output_dir, config=config)
    except ScheduledReportEvidenceError as exc:
        raise PolicyChangeReportContractError(str(exc)) from exc
    sources = dict(collected.get("sources") or {})
    csv_path = sources.get(REPORT_KEY)
    if not csv_path:
        raise PolicyChangeReportContractError("no fresh 4359 report was collected")
    return parse_4359_csv(Path(csv_path).read_bytes())


class _OAuthClientCredentials:
    """Minimal client-credentials token fetch (urllib, stdlib only)."""

    def __init__(self, *, token_endpoint: str, client_id: str, client_secret: str, scope: str) -> None:
        self.token_endpoint = token_endpoint
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self._token: str | None = None

    def get_token(self) -> str:
        if self._token:
            return self._token
        body = parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": self.scope,
        }).encode("ascii")
        request = urlrequest.Request(
            self.token_endpoint, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
        try:
            with urlrequest.urlopen(request, timeout=30) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise PolicyChangeReportContractError(f"EZLynx token endpoint transport failed: {exc}") from exc
        token = str(parsed.get("access_token") or "").strip()
        if not token:
            raise PolicyChangeReportContractError("EZLynx token endpoint returned no access_token")
        self._token = token
        return token


class PolicyApiSearchClient:
    """Read-only OAuth PolicyApi search (GET /PolicyApi/policy/v1/search).

    Same shape as the deployed EzlynxApiClient.search_policy_by_number:
    returns the result rows (policyNumber, accountId, policyStatus,
    expirationDate, premium, ...). Host-only base; never delete anything.
    """

    def __init__(self, *, base_url: str = POLICY_API_BASE_URL,
                 token_endpoint: str, client_id: str, client_secret: str) -> None:
        missing = [name for name, value in (
            ("token_endpoint", token_endpoint), ("client_id", client_id),
            ("client_secret", client_secret),
        ) if not str(value or "").strip()]
        if missing:
            raise PolicyChangeReportContractError(
                "PolicyApi search is not configured (missing: " + ", ".join(missing) + ")"
            )
        self._base = base_url.rstrip("/") + "/"
        self._auth = _OAuthClientCredentials(
            token_endpoint=token_endpoint, client_id=client_id,
            client_secret=client_secret, scope="PolicyApi openid",
        )

    def search_by_number(self, policy_number: str) -> list[dict[str, Any]]:
        url = self._base + "policy/v1/search?" + parse.urlencode(
            {"PolicyNumber": normalize_policy_number(policy_number)}
        )
        request = urlrequest.Request(
            url, headers={
                "Authorization": f"Bearer {self._auth.get_token()}",
                "Accept": "application/json",
            }
        )
        try:
            with urlrequest.urlopen(request, timeout=30) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise PolicyChangeReportContractError(f"PolicyApi search transport failed: {exc}") from exc
        data = parsed.get("data") if isinstance(parsed, dict) else None
        results = (data or {}).get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise PolicyChangeReportContractError("PolicyApi search returned an unexpected shape")
        return [dict(row) for row in results if isinstance(row, dict)]


def default_policy_search(policy_number: str) -> list[dict[str, Any]]:
    client = PolicyApiSearchClient(
        token_endpoint=os.environ.get("EZLYNX_POLICY_API_TOKEN_ENDPOINT", ""),
        client_id=os.environ.get("EZLYNX_POLICY_API_CLIENT_ID", ""),
        client_secret=os.environ.get("EZLYNX_POLICY_API_CLIENT_SECRET", ""),
    )
    return client.search_by_number(policy_number)


def default_discussion_lookup(applicant_id: str) -> list[dict[str, Any]]:
    """DiscussionApi list for one applicant.

    CRITICAL: the config uses the HOST-ONLY DiscussionApi base
    (https://app.ezlynx.com/DiscussionApi/). Deriving the base from
    document_base_url produces .../DocumentApi/DiscussionApi/ and 404s.
    """
    from .ezlynx_discussions import DiscussionApiClient, DiscussionApiConfig, DiscussionApiError

    missing = [
        name for name, var in (
            ("EZLYNX_DISCUSSION_TOKEN_ENDPOINT", os.environ.get("EZLYNX_DISCUSSION_TOKEN_ENDPOINT", "")),
            ("EZLYNX_DISCUSSION_CLIENT_ID", os.environ.get("EZLYNX_DISCUSSION_CLIENT_ID", "")),
            ("EZLYNX_DISCUSSION_CLIENT_SECRET", os.environ.get("EZLYNX_DISCUSSION_CLIENT_SECRET", "")),
            ("EZLYNX_DISCUSSION_USERNAME", os.environ.get("EZLYNX_DISCUSSION_USERNAME", "")),
            ("EZLYNX_DISCUSSION_INTEGRATION_GROUP_ID", os.environ.get("EZLYNX_DISCUSSION_INTEGRATION_GROUP_ID", "")),
        ) if not str(var or "").strip()
    ]
    if missing:
        raise PolicyChangeReportContractError(
            "DiscussionApi lookup is not configured (missing: " + ", ".join(missing) + ")"
        )
    config = DiscussionApiConfig(
        discussion_base_url=DISCUSSION_BASE_URL,
        token_endpoint=os.environ["EZLYNX_DISCUSSION_TOKEN_ENDPOINT"],
        client_id=os.environ["EZLYNX_DISCUSSION_CLIENT_ID"],
        client_secret=os.environ["EZLYNX_DISCUSSION_CLIENT_SECRET"],
        username=os.environ["EZLYNX_DISCUSSION_USERNAME"],
        integration_group_id=os.environ["EZLYNX_DISCUSSION_INTEGRATION_GROUP_ID"],
    )
    try:
        return DiscussionApiClient(config).get_discussions(applicant_id)
    except DiscussionApiError as exc:
        # The Discussion API layer itself failed (auth/transport/shape):
        # the whole source is untrustworthy -> fail closed, send nothing.
        raise PolicyChangeReportContractError(f"DiscussionApi lookup failed: {exc}") from exc


def default_mailer(*, to: list[str], cc: list[str], subject: str,
                   text_body: str, html_body: str) -> dict[str, Any]:
    """Send one CSR report from robie@ via keyless-delegated Gmail (read-back verified later)."""
    import base64
    from email.message import EmailMessage

    import google.auth
    from google.auth import iam
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    service_account_email = os.environ.get("ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", "").strip()
    if not service_account_email:
        raise PolicyChangeReportContractError(
            "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT is not configured"
        )
    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    signer = iam.Signer(Request(), source, service_account_email)
    delegated = service_account.Credentials(
        signer=signer,
        service_account_email=service_account_email,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.send"],
        subject=SENDER,
    )
    gmail = build("gmail", "v1", credentials=delegated, cache_discovery=False)
    message = EmailMessage()
    message["To"] = ", ".join(to)
    message["Cc"] = ", ".join(cc)
    message["From"] = SENDER
    message["Subject"] = subject
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    sent = gmail.users().messages().send(
        userId="me",
        body={"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")},
    ).execute()
    return {
        "kind": "gmail",
        "destination": list(to),
        "cc": list(cc),
        "message_id": sent.get("id"),
        "sender": SENDER,
    }


# -- worker --------------------------------------------------------------------


class OverduePolicyChangeReportWorker:
    """Email CSRs (CC department manager + Carlo) about their overdue 4359 changes.

    All external access is injected for tests; defaults are read-only and
    fail closed. The worker never writes to EZLynx and never deletes
    anything.
    """

    def __init__(
        self,
        *,
        queue_reader: Callable[[Mapping[str, Any]], list[dict[str, str]]] | None = None,
        policy_search: Callable[[str], list[dict[str, Any]]] | None = None,
        discussion_lookup: Callable[[str], list[dict[str, Any]]] | None = None,
        directory_loader: Callable[[str], dict[str, dict[str, str]]] = load_approved_csr_directory,
        mailer: Callable[..., dict[str, Any]] | None = None,
        sent_store: NotificationStore | None = None,
    ) -> None:
        self.queue_reader = queue_reader or default_queue_reader
        self.policy_search = policy_search or default_policy_search
        self.discussion_lookup = discussion_lookup or default_discussion_lookup
        self.directory_loader = directory_loader
        self.mailer = mailer or default_mailer
        self.sent_store = sent_store

    def _enrich_with_discussions(
        self, items: list[dict[str, Any]], today: date
    ) -> None:
        for item in items:
            applicant_id = str(item.get("Applicant ID") or "").strip()
            try:
                discussions = self.discussion_lookup(applicant_id)
            except PolicyChangeReportContractError:
                raise
            except Exception as exc:
                # Isolated per-applicant failure: keep the item, mark its
                # context UNVERIFIED in the email. A DiscussionApiError means
                # the API layer itself failed -> default_discussion_lookup
                # already converted it to the contract error above.
                logger.warning(
                    "Discussion lookup failed for applicant %s: %s", applicant_id, exc
                )
                item["discussion"] = None
                item["discussion_unverified"] = f"{type(exc).__name__}: {exc}"
                continue
            if not isinstance(discussions, list):
                item["discussion"] = None
                item["discussion_unverified"] = "unexpected discussion list shape"
                continue
            item["discussion"] = select_change_discussion(discussions, str(item.get("Policy Number") or ""))

    def perform(self, job: dict[str, Any], *, idempotency_key: str) -> WorkerResult:
        action = str(job.get("action_type") or "")
        if action != ACTION:
            return WorkerResult(
                False, action, {}, retryable=False,
                error=f"policy-change report delivery is not authorized (action {action!r})",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        payload = dict(job.get("payload") or {})
        manifest_path = str(payload.get("manifest_path") or "").strip()
        if not manifest_path:
            return WorkerResult(
                False, ACTION, {}, retryable=False,
                error="approved roster manifest_path is required",
                hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        today = date.today()
        store = self.sent_store or NotificationStore(
            payload.get("sent_store_path")
            or Path("~/.robie/overdue_policy_change_reports/sent.json").expanduser()
        )
        try:
            rows = self.queue_reader(payload)
            qualified = qualify_rows(rows, today)
            live_items: list[dict[str, Any]] = []
            held: list[dict[str, Any]] = []
            dead_count = 0
            for row in qualified:
                verdict = classify_policy_liveness(
                    self.policy_search,
                    str(row.get("Policy Number") or ""),
                    str(row.get("Applicant ID") or ""),
                    today,
                )
                if verdict["verdict"] == "LIVE":
                    live_items.append({**row, "liveness": verdict})
                elif verdict["verdict"] == "DEAD":
                    dead_count += 1
                else:
                    held.append({
                        "account_name": row.get("Account Name"),
                        "policy_number": row.get("Policy Number"),
                        "reason": verdict.get("reason"),
                    })
            self._enrich_with_discussions(live_items, today)
            due_items = [item for item in live_items if store.is_due(item, today)]
            summary = {
                "resource_id": RESOURCE_ID,
                "queue_rows": len(rows),
                "overdue_live": len(live_items),
                "overdue_dead_excluded": dead_count,
                "on_hold": len(held),
                "due_for_nag": len(due_items),
            }
            if not due_items:
                return WorkerResult(
                    True, JOB_TYPE,
                    {**summary, "delivery_receipts": [], "csr_count": 0},
                    {"idempotency_key": idempotency_key, "held": held},
                    retryable=False,
                )
            roster = self.directory_loader(manifest_path)
            targets = resolve_nag_targets(due_items, roster)
            receipts: list[dict[str, Any]] = []
            for csr in sorted(targets):
                target = targets[csr]
                body = build_csr_report(csr, target["items"], target["manager_name"], today)
                html_body = build_csr_report_html(csr, target["items"], target["manager_name"], today)
                receipts.append(
                    self.mailer(
                        to=[target["email"]],
                        cc=[target["manager_email"], CC_CARLO],
                        subject=SUBJECT,
                        text_body=body,
                        html_body=html_body,
                    )
                )
                for item in target["items"]:
                    store.mark_sent(item, today)
            store.save()
        except PolicyChangeReportContractError as exc:
            return WorkerResult(
                False, JOB_TYPE, {}, retryable=False,
                error=str(exc), hold_status=JobStatus.NEEDS_CLARIFICATION,
            )
        except Exception as exc:
            return WorkerResult(
                False, JOB_TYPE, {}, retryable=False,
                error=f"policy-change report delivery failed: {type(exc).__name__}: {exc}",
                hold_status=JobStatus.AWAITING_HUMAN_INPUT,
            )
        return WorkerResult(
            True, JOB_TYPE,
            {**summary, "delivery_receipts": receipts, "csr_count": len(targets)},
            {"idempotency_key": idempotency_key, "held": held},
            retryable=False,
        )


class OverduePolicyChangeReportVerifier:
    """Destination verification: Gmail sent read-back for the CSR receipts."""

    def __init__(
        self,
        *,
        delivery_readback: Callable[
            [list[dict[str, Any]]], tuple[bool, list[dict[str, Any]]]
        ] | None = None,
    ) -> None:
        if delivery_readback is None:
            from .accountability_delivery import verify_delivery_receipts

            delivery_readback = verify_delivery_receipts
        self.delivery_readback = delivery_readback

    def verify(self, job: dict[str, Any], action: dict[str, Any]):
        from .models import VerificationEvidence, VerificationResult

        destination = dict(action.get("destination") or {})
        receipts = list(destination.get("delivery_receipts") or [])
        expected_count = int(destination.get("csr_count") or 0)
        try:
            if expected_count:
                unique_ids = {str(item.get("message_id") or "") for item in receipts}
                delivery_ok, observed_receipts = self.delivery_readback(receipts)
                mailbox_ok = bool(observed_receipts) and all(
                    bool(item.get("exists_in_sent_mailbox") or item.get("exists"))
                    for item in observed_receipts
                )
                verified = (
                    len(receipts) == expected_count
                    and len(unique_ids) == expected_count
                    and "" not in unique_ids
                    and delivery_ok
                    and mailbox_ok
                )
                expected = {
                    "resource_id": RESOURCE_ID,
                    "exists_in_sent_mailbox": True,
                    "gmail_receipt_count": expected_count,
                    "csr_count": expected_count,
                }
                observed = {
                    "resource_id": RESOURCE_ID,
                    "exists_in_sent_mailbox": verified,
                    "gmail_receipt_count": len(receipts),
                    "unique_message_ids": len(unique_ids),
                    "delivery": observed_receipts,
                }
            else:
                verified = not receipts
                expected = {"resource_id": RESOURCE_ID, "gmail_receipt_count": 0}
                observed = {"resource_id": RESOURCE_ID, "gmail_receipt_count": len(receipts)}
            error = None if verified else "Gmail delivery read-back did not verify"
        except Exception as exc:
            expected = {"resource_id": RESOURCE_ID, "gmail_receipt_count": expected_count}
            observed = {"error": f"{type(exc).__name__}: {exc}"}
            verified = False
            error = "CSR report destination could not be independently verified"
        evidence = VerificationEvidence(
            method="GMAIL_SENT_READBACK",
            source="gmail-delegated-sent-mailbox",
            expected=expected,
            observed=observed,
            authoritative=True,
            captured_at=_utc_now(),
            locator=RESOURCE_ID,
        )
        return VerificationResult(verified, evidence, retryable=False, error=error)
