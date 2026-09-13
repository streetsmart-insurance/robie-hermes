"""Preflight helpers for the guarded JE-KILL-01 live proof."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


FORBIDDEN_ACCOUNT_IDS = frozenset({"221398001"})
REQUIRED_PHASES = ("before_action", "after_action", "during_verification")
APPROVER = "Carlo Ferrara"
APPROVAL_SCOPE = "JE-KILL-01"
MAX_APPROVAL_AGE_SECONDS = 7 * 24 * 60 * 60


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
        return [
            "expected exactly one EZLynx page tab for JE-KILL; "
            f"observed {len(eligible)}. Carlo: authenticate Test Chrome "
            "(robie-ezlynx-browser-test) to app.ezlynx.com/web/ (not login)."
        ]
    url = str(eligible[0].get("url") or "").casefold()
    title = str(eligible[0].get("title") or "").casefold()
    if "login" in url or "signin" in url or title == "login":
        return [
            "EZLynx Test session is on the login page. Carlo: complete "
            "Test EZLynx login/MFA on hermes-test-01 before JE-KILL."
        ]
    return []


def read_cdp_tabs(cdp_url: str = "http://127.0.0.1:9222", timeout: float = 5.0) -> list[dict[str, Any]]:
    url = cdp_url.rstrip("/") + "/json/list"
    with urlopen(url, timeout=timeout) as response:  # noqa: S310 — local CDP only
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("CDP /json/list did not return a list")
    return [row for row in payload if isinstance(row, dict)]


def live_preflight_errors(
    *,
    fixture_path: str,
    cdp_url: str = "http://127.0.0.1:9222",
) -> list[str]:
    """Aggregate fixture + CDP blockers before any kill cycle."""
    errors: list[str] = []
    try:
        data = json.loads(open(fixture_path, encoding="utf-8").read())
    except OSError as exc:
        return [f"fixture unreadable: {exc}"]
    except json.JSONDecodeError as exc:
        return [f"fixture JSON invalid: {exc}"]
    if not isinstance(data, dict):
        return ["fixture root must be an object"]
    errors.extend(fixture_approval_errors(data))
    try:
        tabs = read_cdp_tabs(cdp_url)
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        errors.append(
            f"CDP unavailable at {cdp_url}: {type(exc).__name__}: {exc}. "
            "Ensure robie-ezlynx-browser-test is running on hermes-test-01."
        )
        return errors
    errors.extend(ezlynx_auth_tab_errors(tabs))
    return errors
