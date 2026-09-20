"""Preflight helpers for the guarded JE-KILL-01 live proof."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


FORBIDDEN_ACCOUNT_IDS = frozenset({"221398001"})
REQUIRED_PHASES = ("before_action", "after_action", "during_verification")
APPROVER = "Carlo Ferrara"
APPROVAL_SCOPE = "JE-KILL-01"
MAX_APPROVAL_AGE_SECONDS = 7 * 24 * 60 * 60
ACTIVE_STATUSES = ("RUNNING", "VERIFYING")


def job_inventory_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Split Test Jobs into lease-holding blockers and unleased parked rows.

    Only a worker that claimed a lease can be driving the shared Test Chrome.
    A RUNNING/VERIFYING row with an empty ``lease_owner`` is orphaned or
    parked for HITL (for example generic Chat Jobs that JobEngine never
    claims, such as c282de98) and must not block the disposable kill proof.
    """
    blocking: list[str] = []
    unleased: list[str] = []
    for row in rows:
        job_id = str(row.get("id") or "").strip()
        status = str(row.get("status") or "").strip().upper()
        lease_owner = str(row.get("lease_owner") or "").strip()
        if lease_owner:
            blocking.append(f"{job_id} {status} lease={lease_owner}")
        elif status in ACTIVE_STATUSES:
            unleased.append(f"{job_id} {status}")
    return {"blocking": blocking, "unleased": unleased}


def job_inventory_errors(rows: list[dict[str, Any]]) -> list[str]:
    report = job_inventory_report(rows)
    if not report["blocking"]:
        return []
    return [
        f"{len(report['blocking'])} active Test Job lease(s): "
        + "; ".join(report["blocking"])
    ]


def read_job_inventory(db_path: str) -> list[dict[str, Any]]:
    """Read-only snapshot of non-terminal Jobs and every lease holder."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT id, status, lease_owner FROM jobs
               WHERE status IN ('RUNNING','VERIFYING')
                  OR (lease_owner IS NOT NULL AND lease_owner <> '')"""
        ).fetchall()
    finally:
        con.close()
    return [dict(row) for row in rows]


def fixture_approval_errors(data: dict[str, Any], *, now: datetime | None = None) -> list[str]:
    """Return human-readable blockers for fixture approval freshness/identity."""
    errors: list[str] = []
    if data.get("test_only") is not True or data.get("disposable") is not True:
        errors.append("fixture must declare test_only=true and disposable=true")
    if str(data.get("approved_by") or "").strip() != APPROVER:
        errors.append(f"approved_by must be {APPROVER!r}")
    if data.get("approval_scope") != APPROVAL_SCOPE:
        errors.append(f"approval_scope must be {APPROVAL_SCOPE!r}")
    approved_at = str(data.get("approved_at") or "").strip()
    try:
        approval_time = datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
    except ValueError:
        errors.append("approved_at must be an ISO-8601 timestamp")
        return errors
    if approval_time.tzinfo is None:
        errors.append("approved_at must include a timezone")
        return errors
    current = now or datetime.now(timezone.utc)
    age = (current - approval_time.astimezone(timezone.utc)).total_seconds()
    if age < -300 or age > MAX_APPROVAL_AGE_SECONDS:
        days = round(age / 86400, 2)
        errors.append(
            f"fixture approval expired ({days} days old); "
            "Carlo must re-stamp approved_at within seven days "
            "(bash scripts/refresh-je-kill-fixture-approval.sh REAPPROVE_JE_KILL_01_FIXTURE)"
        )
    rows = data.get("scenarios")
    if not isinstance(rows, dict) or set(rows) != set(REQUIRED_PHASES):
        errors.append(f"scenarios must be exactly {', '.join(REQUIRED_PHASES)}")
        return errors
    resource_ids: set[str] = set()
    for phase in REQUIRED_PHASES:
        raw = rows[phase]
        if not isinstance(raw, dict):
            errors.append(f"scenario {phase} must be an object")
            continue
        account_id = str(raw.get("account_id") or "").strip()
        resource_id = str(raw.get("resource_id") or "").strip()
        if account_id in FORBIDDEN_ACCOUNT_IDS:
            errors.append(f"scenario {phase} uses a forbidden account")
        if not resource_id:
            errors.append(f"scenario {phase} missing resource_id")
        elif resource_id in resource_ids:
            errors.append("each kill phase requires a distinct disposable document")
        resource_ids.add(resource_id)
    return errors


def ezlynx_auth_tab_errors(tabs: list[dict[str, Any]]) -> list[str]:
    """Require exactly one authenticated EZLynx page tab."""
    pages = [t for t in tabs if str(t.get("type") or "") == "page"]
    eligible = [
        t
        for t in pages
        if "ezlynx.com" in str(t.get("url") or "").casefold()
    ]
    if len(eligible) != 1:
        page_urls = [str(t.get("url") or "") for t in pages[:5]]
        blank_only = pages and all(
            str(t.get("url") or "").casefold() in {"about:blank", "about:blank/", ""}
            for t in pages
        )
        detail = (
            "CDP shows only about:blank — Test Chrome has no EZLynx tab. "
            if blank_only
            else f"observed URLs sample={page_urls}. "
        )
        return [
            "CDP AUTHENTICATED required: expected exactly one EZLynx page tab "
            f"for JE-KILL; observed {len(eligible)}. {detail}"
            "Carlo/Dusty: login SSRobie on hermes-test-01 "
            "(robie-ezlynx-browser-test) to app.ezlynx.com/web/ (not login) "
            "and leave one authenticated tab open."
        ]
    url = str(eligible[0].get("url") or "").casefold()
    title = str(eligible[0].get("title") or "").casefold()
    if "login" in url or "signin" in url or title == "login":
        return [
            "CDP AUTHENTICATED required: EZLynx Test session is on the login "
            "page. Carlo: complete Test EZLynx login/MFA on hermes-test-01 "
            "before JE-KILL (leave one app.ezlynx.com/web/ tab open)."
        ]
    return []


def read_cdp_tabs(cdp_url: str = "http://127.0.0.1:9222", timeout: float = 5.0) -> list[dict[str, Any]]:
    url = cdp_url.rstrip("/") + "/json/list"
    with urlopen(url, timeout=timeout) as response:  # noqa: S310 — local CDP only
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("CDP /json/list did not return a list")
    return [row for row in payload if isinstance(row, dict)]


def release_pointer_errors(
    *,
    test_root: str,
    expected_sha: str,
) -> list[str]:
    """Fail closed when Test release pointers disagree with the workflow SHA."""
    from pathlib import Path

    errors: list[str] = []
    root = Path(test_root)
    sha = str(expected_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        return [f"EXPECTED_SHA invalid: {expected_sha!r}"]
    short = sha[:12]
    try:
        current = root.joinpath("current").resolve()
        releases_current = root.joinpath("releases", "current").resolve()
    except OSError as exc:
        return [f"Test release pointers unreadable: {exc}"]
    if not current.exists() or not releases_current.exists():
        return ["Test release pointers missing under current/ or releases/current"]
    if current != releases_current:
        errors.append(
            f"Test release pointers disagree: current={current} "
            f"releases/current={releases_current}"
        )
    expected_prefix = str((root / "releases" / short).resolve())
    live = str(current)
    if not (live == expected_prefix or live.startswith(expected_prefix + os.sep)):
        errors.append(
            f"Test release pointer SHA mismatch: live={live} "
            f"expected under releases/{short}/ (workflow commit). Deploy Test "
            "to match before JE-KILL."
        )
    return errors


def live_preflight_errors(
    *,
    fixture_path: str,
    cdp_url: str = "http://127.0.0.1:9222",
    job_db: str | None = None,
    test_root: str | None = None,
    expected_sha: str | None = None,
) -> list[str]:
    """Aggregate fixture + CDP + inventory + release-pointer blockers.

    Each error is one clear line. Callers print ``JE-KILL PREFLIGHT BLOCKED:``
    then each line. Fail closed before any kill cycle.
    """
    errors: list[str] = []
    if test_root and expected_sha:
        errors.extend(
            release_pointer_errors(test_root=test_root, expected_sha=expected_sha)
        )
    if job_db:
        try:
            rows = read_job_inventory(job_db)
        except (OSError, sqlite3.Error) as exc:
            errors.append(f"job inventory unreadable: {type(exc).__name__}: {exc}")
        else:
            report = job_inventory_report(rows)
            if report["blocking"]:
                errors.append(
                    f"{len(report['blocking'])} active Test Job lease(s): "
                    + "; ".join(report["blocking"])
                )
            # Unleased orphans are ignored for the kill gate (logged by caller).
    try:
        data = json.loads(open(fixture_path, encoding="utf-8").read())
    except OSError as exc:
        errors.append(f"fixture unreadable: {exc}")
        return errors
    except json.JSONDecodeError as exc:
        errors.append(f"fixture JSON invalid: {exc}")
        return errors
    if not isinstance(data, dict):
        errors.append("fixture root must be an object")
        return errors
    errors.extend(fixture_approval_errors(data))
    try:
        tabs = read_cdp_tabs(cdp_url)
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        errors.append(
            f"CDP unavailable at {cdp_url}: {type(exc).__name__}: {exc}. "
            "Ensure robie-ezlynx-browser-test is running on hermes-test-01 "
            "and the SSRobie session is AUTHENTICATED (not login)."
        )
        return errors
    errors.extend(ezlynx_auth_tab_errors(tabs))
    return errors
