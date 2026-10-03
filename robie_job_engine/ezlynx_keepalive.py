"""Lease-gated EZLynx keepalive for Production and Test.

One implementation, two host profiles (``ROBIE_ENV=PRODUCTION`` or ``TEST``).
Each tick refreshes the SSRobie server session: a credentialed no-store GET of
the dashboard from a tab already on ``https://app.ezlynx.com``, or a
same-origin document navigation onto that dashboard when the tab is
elsewhere. A CDP ``/json/list`` read does not slide the idle timer, and
``page.reload`` can be served from cache, so neither is the refresh.

Prod EZLynx signed out twice on 2026-10-03 ET after light use. The hourly
preflight (tab read only) saw the login page at 13:00, 14:00, 15:00, and
17:00, and a live tab at 12:00 and 16:00. Each drop was inside a ~60 minute
idle window. The timer is 15 minutes (1 minute accuracy). One skipped tick
still leaves about 32 minutes of idle, inside that window.

The tick never logs in, never closes tabs, and never posts to Chat. A
logged-out browser is logged and left for a human (Moe) to re-auth.

The run is skipped unless this VM's driver gate is ALLOWED, and skipped when
a Job holds the browser, a lease, or the local session lock.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

from .ezlynx_driver_gate import DriverDecision, check_driver_gate
from .ezlynx_session import (
    NAVIGATION_SETTLE_ATTEMPTS,
    NAVIGATION_SETTLE_DELAY_SECONDS,
    SessionState,
    read_playwright_snapshot,
    wait_for_settled_session,
)
from .session_preflight import (
    DEFAULT_CDP_URL,
    LOGGED_OUT,
    NO_EZLYNX_TAB,
    NO_TABS,
    SESSION_PRESENT,
    UNREACHABLE,
    check,
)

DASHBOARD_URL = "https://app.ezlynx.com/web/dashboard"
SESSION_REFRESH_URL = DASHBOARD_URL
EZLYNX_APP_ORIGIN = "https://app.ezlynx.com"
# Prod dropped inside ~60 idle minutes on 2026-10-03. Stay at or under 20,
# and keep one missed tick plus timer accuracy inside that window.
KEEPALIVE_INTERVAL_MINUTES = 15
TIMER_ACCURACY_MINUTES = 1
OBSERVED_IDLE_SIGNOUT_MINUTES = 60
ROOTS = {
    "PRODUCTION": "/opt/streetsmart-hermes",
    "TEST": "/opt/streetsmart-hermes-test",
}
ACTIVE_STATUSES = frozenset({"RUNNING", "VERIFYING", "AWAITING_HUMAN_INPUT"})
TERMINAL_STATUSES = frozenset({
    "COMPLETE",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "CANCELED",
    "VERIFIED",
})

EXIT_OK = 0
EXIT_UNKNOWN = 1
EXIT_LOGGED_OUT = 2

LOGGED_OUT_HINT = (
    "EZLynx session is logged out. Keepalive did not log in and did not "
    "close tabs. Re-login is a deliberate human/Moe step."
)

# In-page GET. credentials:include sends the EZLynx session cookie;
# cache:no-store stops the browser from answering without a server hit.
SAME_ORIGIN_GET = """async (url) => {
  const response = await fetch(url, {
    method: "GET",
    credentials: "include",
    cache: "no-store",
    redirect: "follow",
  });
  return { status: response.status, url: response.url };
}"""


def worst_idle_gap_minutes(
    interval_minutes: int = KEEPALIVE_INTERVAL_MINUTES,
    accuracy_minutes: int = TIMER_ACCURACY_MINUTES,
) -> int:
    """Idle minutes if one tick is skipped and both firings wait out accuracy.

    systemd schedules the next tick ``OnUnitActiveSec`` after the previous
    activation, and ``AccuracySec`` may delay it. Skipping one tick (a busy
    lease, or a missed firing) leaves two intervals plus accuracy on each.
    """
    return (int(interval_minutes) + int(accuracy_minutes)) * 2


def _is_login_url(url: str) -> bool:
    folded = (url or "").casefold()
    return "/auth/account/login" in folded or "/auth/account/logout" in folded


def _is_app_origin(url: str) -> bool:
    parts = urlsplit(url or "")
    origin = urlsplit(EZLYNX_APP_ORIGIN)
    return (
        parts.scheme.casefold() == origin.scheme.casefold()
        and parts.netloc.casefold() == origin.netloc.casefold()
    )


def _http_status(value: object) -> int | None:
    try:
        status = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if status < 0:
        return None
    return status


def plan_session_refresh(
    page_url: str,
    refresh_url: str = SESSION_REFRESH_URL,
) -> dict[str, Any]:
    """Pick a read that reaches EZLynx, not a CDP tab-list check.

    A page already on ``https://app.ezlynx.com`` gets an in-page GET so the
    visible tab stays put and the session cookie is sent. Any other tab
    (``about:blank``, another site) is navigated to the dashboard: a fetch
    from a foreign origin would be cross-origin and may not send the cookie.
    A login URL is left alone.
    """
    if _is_login_url(page_url):
        return {
            "method": "none",
            "reason": "already_logged_out",
            "refresh_url": "",
            "credentials": "",
            "cache": "",
        }
    if _is_app_origin(page_url):
        return {
            "method": "same-origin-get",
            "reason": "credentialed_xhr",
            "refresh_url": refresh_url,
            "credentials": "include",
            "cache": "no-store",
        }
    return {
        "method": "same-origin-navigation",
        "reason": "document_navigation",
        "refresh_url": refresh_url,
        "credentials": "include",
        "cache": "no-store",
    }


def interpret_session_refresh(
    plan: dict[str, Any],
    *,
    status: int | None,
    final_url: str,
    error: str = "",
) -> dict[str, Any]:
    """Turn a GET result into a keepalive verdict. Never a login."""
    method = str(plan.get("method") or "none")
    report: dict[str, Any] = {
        "ok": False,
        "reason": "",
        "refresh_method": method,
        "refresh_url": str(plan.get("refresh_url") or ""),
        "refresh_status": status,
        "final_url": final_url,
        "logged_out": False,
        "interactive": False,
        "closed_tabs": 0,
        "created_page": False,
        "logged_in": False,
    }
    if method == "none":
        report["reason"] = "already_logged_out"
        report["logged_out"] = True
        return report
    if error:
        report["reason"] = error
        return report
    if _is_login_url(final_url):
        report["reason"] = "logged_out"
        report["logged_out"] = True
        return report
    if status is not None and status >= 400:
        report["reason"] = f"http_{status}"
        return report
    if (
        status is not None
        and 200 <= status < 300
        and "/web/" in (final_url or "").casefold()
    ):
        report["ok"] = True
        report["reason"] = "session_refreshed"
        return report
    report["reason"] = "unconfirmed"
    return report


def _refresh_report(**extra: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "ok": False,
        "reason": "",
        "refresh_method": "",
        "refresh_url": "",
        "refresh_status": None,
        "final_url": "",
        "logged_out": False,
        "interactive": False,
        "closed_tabs": 0,
        "created_page": False,
        "logged_in": False,
    }
    report.update(extra)
    return report


def _navigate_and_classify(
    page: Any,
    plan: dict[str, Any],
    *,
    attempts: int,
    delay_seconds: float,
    sleeper: Callable[[float], None] | None,
) -> dict[str, Any]:
    """Document GET of the dashboard. Read-only: no form fill, no new tab."""
    refresh_url = str(plan.get("refresh_url") or SESSION_REFRESH_URL)
    status: int | None = None
    try:
        response = page.goto(refresh_url, wait_until="domcontentloaded", timeout=20_000)
        status = _http_status(getattr(response, "status", None))
    except Exception as exc:  # noqa: BLE001 — caller records the tick, no traceback
        return _refresh_report(
            reason=f"{type(exc).__name__}: {exc}",
            refresh_method="same-origin-navigation",
            refresh_url=refresh_url,
            final_url=str(getattr(page, "url", "") or ""),
        )
    state = wait_for_settled_session(
        lambda: read_playwright_snapshot(page),
        attempts=attempts,
        delay_seconds=delay_seconds,
        sleeper=sleeper,
    )
    final_url = str(getattr(page, "url", "") or "")
    logged_out = state is SessionState.LOGIN_REQUIRED or _is_login_url(final_url)
    interactive = state is SessionState.INTERACTIVE_AUTH_REQUIRED
    ok = state is SessionState.SIGNED_IN and not logged_out and not interactive
    if logged_out:
        reason = "logged_out"
    elif interactive:
        reason = "interactive_auth"
    elif ok:
        reason = "session_refreshed"
    else:
        reason = "unconfirmed"
    return _refresh_report(
        ok=ok,
        reason=reason,
        refresh_method="same-origin-navigation",
        refresh_url=refresh_url,
        refresh_status=status,
        final_url=final_url,
        logged_out=logged_out and not interactive,
        interactive=interactive,
        state=state.value,
    )


def apply_session_refresh(
    page: Any,
    *,
    refresh_url: str = SESSION_REFRESH_URL,
    attempts: int = NAVIGATION_SETTLE_ATTEMPTS,
    delay_seconds: float = NAVIGATION_SETTLE_DELAY_SECONDS,
    sleeper: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Refresh one existing page. Does not create, close, or log in."""
    current = str(getattr(page, "url", "") or "")
    plan = plan_session_refresh(current, refresh_url)
    if plan["method"] == "none":
        return _refresh_report(
            reason="already_logged_out",
            refresh_method="none",
            final_url=current,
            logged_out=True,
        )
    if plan["method"] == "same-origin-get":
        fetched: Any = None
        get_error = ""
        try:
            fetched = page.evaluate(SAME_ORIGIN_GET, plan["refresh_url"])
        except Exception as exc:  # noqa: BLE001
            get_error = f"{type(exc).__name__}: {exc}"
        if not get_error:
            if not isinstance(fetched, dict):
                get_error = "fetch_result_unusable"
            else:
                interpreted = interpret_session_refresh(
                    plan,
                    status=_http_status(fetched.get("status")),
                    final_url=str(fetched.get("url") or ""),
                )
                if interpreted["ok"] or interpreted["logged_out"]:
                    return interpreted
                get_error = str(interpreted.get("reason") or "unconfirmed")
        navigated = _navigate_and_classify(
            page,
            {**plan, "method": "same-origin-navigation"},
            attempts=attempts,
            delay_seconds=delay_seconds,
            sleeper=sleeper,
        )
        navigated["fallback_from"] = "same-origin-get"
        if not navigated.get("ok") and not navigated.get("logged_out"):
            navigated["get_error"] = get_error
        return navigated
    return _navigate_and_classify(
        page,
        plan,
        attempts=attempts,
        delay_seconds=delay_seconds,
        sleeper=sleeper,
    )


def normalize_profile(name: str | None = None) -> str:
    """Return PRODUCTION, TEST, or '' when the profile is unset or unknown."""
    raw = (name if name is not None else os.environ.get("ROBIE_ENV", "")).strip().upper()
    if raw == "TEST":
        return "TEST"
    if raw in {"PRODUCTION", "PROD", "LIVE"}:
        return "PRODUCTION"
    return ""


def profile_root(profile: str) -> str:
    return ROOTS[profile]


def jobs_db_path(profile: str, override: str | None = None) -> str:
    chosen = (override if override is not None else os.environ.get("ROBIE_JOB_DB", "")).strip()
    if chosen:
        return chosen
    return f"{profile_root(profile)}/robie-job-engine/data/jobs.db"


def session_lock_path(profile: str, override: str | None = None) -> str:
    chosen = (
        override if override is not None else os.environ.get("ROBIE_EZLYNX_SESSION_LOCK", "")
    ).strip()
    if chosen:
        return chosen
    return f"{profile_root(profile)}/robie-job-engine/data/ezlynx-session.lock"


def cdp_url_from_env(override: str | None = None) -> str:
    if override:
        return override
    return (
        os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL")
        or os.environ.get("ROBIE_BROWSER_CDP_URL")
        or DEFAULT_CDP_URL
    )


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def job_holds_browser_or_lease(
    db_path: str,
    *,
    now: datetime | None = None,
) -> bool:
    """True when a Job is driving the browser or still holds a live lease.

    A missing database is not a hold. An unreadable database fails closed
    (treat it as held) so the keepalive does not drive a browser it cannot
    prove is idle.
    """
    if not db_path or not os.path.isfile(db_path):
        return False
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                """SELECT status, lease_owner, lease_expires_at FROM jobs
                   WHERE status IN ('RUNNING', 'VERIFYING', 'AWAITING_HUMAN_INPUT')
                      OR (lease_owner IS NOT NULL AND TRIM(lease_owner) <> '')"""
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — unreadable ledger: do not touch the browser
        return True
    for status, owner, expires_at in rows:
        state = str(status or "").strip().upper()
        if state in ACTIVE_STATUSES:
            return True
        if state in TERMINAL_STATUSES:
            continue
        if not str(owner or "").strip():
            continue
        expiry = _parse_time(expires_at)
        if expiry is None or expiry > clock:
            return True
    return False


@contextlib.contextmanager
def session_lock(path: str) -> Iterator[bool]:
    """Hold the existing session lock, or yield False when a Job has it.

    A missing lock file means nothing on this host is inside
    ``exclusive_session``. The keepalive does not create the file.
    """
    if not path or not os.path.exists(path):
        yield True
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        yield False
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _base(profile: str, **extra: Any) -> dict[str, Any]:
    report = {
        "host_role": profile.lower() if profile else "unknown",
        "profile": profile or "UNSET",
        "action": "none",
        "logged_in": False,
        "posted_chat": False,
        "closed_tabs": 0,
    }
    report.update(extra)
    return report


def touch_existing_page(
    cdp_url: str,
    dashboard_url: str = DASHBOARD_URL,
    *,
    attempts: int = NAVIGATION_SETTLE_ATTEMPTS,
    delay_seconds: float = NAVIGATION_SETTLE_DELAY_SECONDS,
    sleeper: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Refresh the server session on a tab that already exists.

    Does not create a tab, does not close a tab, and does not type credentials.
    Disconnects CDP when finished; Chrome keeps the page. The CDP tab list is
    only used to find the page — the refresh is ``apply_session_refresh``.
    """
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=15_000)
        pages = [page for context in browser.contexts for page in context.pages]
        if not pages:
            return _refresh_report(reason="no_page")
        ezlynx_pages = [page for page in pages if "ezlynx.com" in str(page.url or "").casefold()]
        page = (ezlynx_pages or pages)[0]
        return apply_session_refresh(
            page,
            refresh_url=dashboard_url,
            attempts=attempts,
            delay_seconds=delay_seconds,
            sleeper=sleeper,
        )
    except Exception as exc:  # noqa: BLE001 — a timer tick must not traceback
        return _refresh_report(reason=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            playwright.stop()
        except Exception:  # noqa: BLE001
            pass


def _logged_out(profile: str, **extra: Any) -> dict[str, Any]:
    return _base(
        profile,
        verdict="LOGGED_OUT",
        action="log_logged_out",
        exit_code=EXIT_LOGGED_OUT,
        hint=LOGGED_OUT_HINT,
        **extra,
    )


def run_keepalive(
    *,
    profile: str | None = None,
    cdp_url: str | None = None,
    jobs_db: str | None = None,
    lock_path: str | None = None,
    dashboard_url: str = DASHBOARD_URL,
    gate: Callable[[], DriverDecision] | None = None,
    holds_browser: Callable[[str], bool] | None = None,
    lock: Callable[[str], Any] | None = None,
    checker: Callable[[str], dict[str, Any]] | None = None,
    touch: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One keepalive tick. Never logs in. Never posts to Chat."""
    chosen = normalize_profile(profile)
    if chosen not in ROOTS:
        return _base(
            chosen,
            verdict="UNKNOWN",
            action="skip_unknown_profile",
            exit_code=EXIT_UNKNOWN,
            reason="Set ROBIE_ENV=PRODUCTION or ROBIE_ENV=TEST",
        )

    decision = (gate or check_driver_gate)()
    if not decision.allowed:
        return _base(
            chosen,
            verdict="DRIVER_NOT_IN",
            action="skip_driver_gate",
            exit_code=EXIT_OK,
            gate="BLOCKED",
            gate_reason=decision.reason,
        )

    db_path = jobs_db_path(chosen, jobs_db)
    if (holds_browser or job_holds_browser_or_lease)(db_path):
        return _base(
            chosen,
            verdict="SKIPPED",
            action="skip_busy_lease",
            exit_code=EXIT_OK,
            gate="ALLOWED",
            jobs_db=db_path,
        )

    lock_file = session_lock_path(chosen, lock_path)
    endpoint = cdp_url_from_env(cdp_url)
    read_tabs = checker or check
    warm = touch or touch_existing_page
    holder = lock or session_lock
    with holder(lock_file) as acquired:
        if not acquired:
            return _base(
                chosen,
                verdict="SKIPPED",
                action="skip_browser_lock",
                exit_code=EXIT_OK,
                gate="ALLOWED",
            )
        report = read_tabs(endpoint)
        state = str(report.get("state") or "")
        if state == UNREACHABLE:
            return _base(
                chosen,
                verdict="UNREACHABLE",
                action="log_cdp_down",
                exit_code=EXIT_UNKNOWN,
                gate="ALLOWED",
                preflight=report,
            )
        if state == LOGGED_OUT:
            return _logged_out(chosen, gate="ALLOWED", preflight=report)
        if state not in {SESSION_PRESENT, NO_EZLYNX_TAB, NO_TABS}:
            return _base(
                chosen,
                verdict="UNKNOWN",
                action="none",
                exit_code=EXIT_UNKNOWN,
                gate="ALLOWED",
                preflight=report,
            )
        warmed = warm(endpoint, dashboard_url)
        closed = int(warmed.get("closed_tabs") or 0)
        if warmed.get("logged_out"):
            return _logged_out(
                chosen,
                gate="ALLOWED",
                preflight=report,
                navigate=warmed,
                closed_tabs=closed,
            )
        if warmed.get("interactive"):
            return _base(
                chosen,
                verdict="INTERACTIVE_AUTH_REQUIRED",
                action="log_interactive_auth",
                exit_code=EXIT_LOGGED_OUT,
                gate="ALLOWED",
                hint=LOGGED_OUT_HINT,
                preflight=report,
                navigate=warmed,
                closed_tabs=closed,
            )
        if not warmed.get("ok"):
            return _base(
                chosen,
                verdict="UNKNOWN",
                action="touch_failed",
                exit_code=EXIT_UNKNOWN,
                gate="ALLOWED",
                preflight=report,
                navigate=warmed,
                closed_tabs=closed,
            )
        return _base(
            chosen,
            verdict="AUTHENTICATED",
            action="touch_existing_page",
            exit_code=EXIT_OK,
            gate="ALLOWED",
            preflight=report,
            navigate=warmed,
            closed_tabs=closed,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lease-gated EZLynx keepalive (no login, no tab close, no Chat)"
    )
    parser.add_argument("--profile", choices=("PRODUCTION", "TEST"), default=None)
    parser.add_argument("--cdp-url", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = run_keepalive(profile=args.profile, cdp_url=args.cdp_url)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("EZLynx keepalive")
        print(f"  profile : {result.get('profile')}")
        print(f"  gate    : {result.get('gate', '')}")
        print(f"  verdict : {result.get('verdict')}")
        print(f"  action  : {result.get('action')}")
        print(f"  login   : {result.get('logged_in')}")
        print(f"  closed  : {result.get('closed_tabs')}")
        print(f"  chat    : {result.get('posted_chat')}")
        if result.get("hint"):
            print(f"  hint    : {result['hint']}")
        print(f"  exit    : {result.get('exit_code')}")
    code = result.get("exit_code")
    return int(EXIT_UNKNOWN if code is None else code)


if __name__ == "__main__":
    raise SystemExit(main())
