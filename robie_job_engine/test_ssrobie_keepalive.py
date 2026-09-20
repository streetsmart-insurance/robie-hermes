"""Test-only SSRobie CDP keep-alive (hermes-test-01).

Why sessions die on Test
------------------------
1. ``robie-ezlynx-browser-test`` starts Chrome on ``about:blank``. Any service
   restart drops the authenticated tab even when profile cookies remain.
2. Test has no Prod-style ``robie-ezlynx-session.timer`` — idle EZLynx cookies
   expire and the next navigation lands on ``/auth/account/login``.
3. Auto MFA bootstrap on Test often cannot run (missing
   ``robie_google_token.json`` / DWD) → Dusty must re-auth by hand.

This keep-alive never restarts Chrome (restart → blank). It only:
- reports AUTHENTICATED / LOGGED_OUT / BLANK from CDP
- if the sole page is ``about:blank`` but cookies may still work, navigates
  that page to the Test dashboard URL
- if already on ``app.ezlynx.com/web/``, soft-reloads dashboard to refresh idle
- if on login, exits fail-closed and tells operators to ping Dusty

Prod is out of scope. No credential printing.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.request import urlopen

from .session_preflight import (
    DEFAULT_CDP_URL,
    LOGGED_OUT,
    LOGIN_PATH,
    NO_EZLYNX_TAB,
    NO_TABS,
    SESSION_PRESENT,
    UNREACHABLE,
    check,
)

TEST_DASHBOARD_URL = "https://app.ezlynx.com/web/dashboard"
BLANK_URLS = frozenset({"", "about:blank", "about:blank/", "about:blank#blocked"})

EXIT_OK = 0
EXIT_UNKNOWN = 1
EXIT_NEEDS_AUTH = 2


def cdp_auth_verdict(report: dict[str, Any]) -> str:
    """Map session_preflight state to Stage 4 operator language."""
    state = str(report.get("state") or "")
    if state == LOGGED_OUT:
        return "NEEDS_AUTH"
    if state == SESSION_PRESENT:
        pages = list(report.get("pages") or [])
        ez = [
            p
            for p in pages
            if "ezlynx.com" in str(p.get("url") or "").casefold()
        ]
        if len(ez) == 1:
            url = str(ez[0].get("url") or "").casefold()
            title = str(ez[0].get("title") or "").casefold()
            if LOGIN_PATH in url or "signin" in url or title == "login":
                return "NEEDS_AUTH"
            if "/web/" in url:
                return "AUTHENTICATED"
        return "SESSION_PRESENT"
    if state in {NO_TABS, NO_EZLYNX_TAB}:
        return "BLANK"
    if state == UNREACHABLE:
        return "UNREACHABLE"
    return "UNKNOWN"


def _page_urls(report: dict[str, Any]) -> list[str]:
    return [str(p.get("url") or "") for p in report.get("pages") or []]


def should_restore_blank(report: dict[str, Any]) -> bool:
    """True when Chrome is up but only blank / non-EZLynx pages."""
    if cdp_auth_verdict(report) != "BLANK":
        return False
    urls = [u.casefold().rstrip("/") for u in _page_urls(report)]
    if not urls:
        return True
    return all(u in BLANK_URLS or "ezlynx.com" not in u for u in urls)


def navigate_existing_page(cdp_url: str, target_url: str, timeout_ms: int = 30_000) -> dict[str, Any]:
    """Navigate the first shared page (or create one) to ``target_url``.

    Does not restart Chrome. Closes only a page we created; never closes the
    operator/Dusty tab if we reused it.
    """
    from playwright.sync_api import sync_playwright

    created = False
    playwright = sync_playwright().start()
    page = None
    try:
        browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=15_000)
        contexts = browser.contexts
        if not contexts:
            return {"ok": False, "reason": "no browser context"}
        ctx = contexts[0]
        pages = list(ctx.pages)
        if pages:
            page = pages[0]
        else:
            page = ctx.new_page()
            created = True
        page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1_500)
        final = str(page.url or "")
        title = str(page.title() or "")
        logged_out = LOGIN_PATH in final.casefold()
        return {
            "ok": not logged_out and "/web/" in final.casefold(),
            "created_page": created,
            "final_url": final,
            "title": title,
            "logged_out": logged_out,
        }
    except Exception as exc:  # noqa: BLE001 — keep-alive must never crash the timer
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        if created and page is not None:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            playwright.stop()
        except Exception:  # noqa: BLE001
            pass


def has_blocking_test_leases(
    db_path: str = "/opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db",
) -> bool:
    """True when a lease-holding Job owns the shared Test Chrome."""
    import sqlite3
    from pathlib import Path

    path = Path(db_path)
    if not path.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                """SELECT lease_owner FROM jobs
                   WHERE status IN ('RUNNING','VERIFYING')
                      OR (lease_owner IS NOT NULL AND lease_owner <> '')"""
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — fail closed: skip warm if DB unreadable
        return True
    return any(str(r[0] or "").strip() for r in rows)


def run_keepalive(
    *,
    cdp_url: str = DEFAULT_CDP_URL,
    dashboard_url: str = TEST_DASHBOARD_URL,
    warm: bool = True,
    jobs_db: str = "/opt/streetsmart-hermes-test/robie-job-engine/data/jobs.db",
) -> dict[str, Any]:
    """One keep-alive tick. Returns a JSON-serializable report + exit_code."""
    report = check(cdp_url)
    verdict = cdp_auth_verdict(report)
    out: dict[str, Any] = {
        "host_role": "test",
        "cdp_url": cdp_url,
        "verdict": verdict,
        "preflight": report,
        "action": "none",
        "exit_code": EXIT_OK,
    }

    if verdict == "UNREACHABLE":
        out["exit_code"] = EXIT_UNKNOWN
        out["action"] = "ping_dusty_cdp_down"
        return out

    if verdict == "NEEDS_AUTH":
        out["exit_code"] = EXIT_NEEDS_AUTH
        out["action"] = "ping_dusty_reauth"
        out["hint"] = (
            "CDP tab is on EZLynx login. Do not restart Chrome (that yields "
            "about:blank). Re-auth SSRobie on hermes-test-01 and leave one "
            "app.ezlynx.com/web/ tab open."
        )
        return out

    if has_blocking_test_leases(jobs_db):
        out["action"] = "skip_busy_lease"
        out["exit_code"] = EXIT_OK if verdict == "AUTHENTICATED" else EXIT_UNKNOWN
        return out

    if verdict == "BLANK" or should_restore_blank(report):
        nav = navigate_existing_page(cdp_url, dashboard_url)
        out["action"] = "restore_from_blank"
        out["navigate"] = nav
        if nav.get("logged_out"):
            out["verdict"] = "NEEDS_AUTH"
            out["exit_code"] = EXIT_NEEDS_AUTH
            out["hint"] = "Cookies expired; ping Dusty to re-auth SSRobie on Test."
            return out
        if not nav.get("ok"):
            out["exit_code"] = EXIT_UNKNOWN
            return out
        report = check(cdp_url)
        out["preflight_after"] = report
        out["verdict"] = cdp_auth_verdict(report)
        out["exit_code"] = (
            EXIT_OK if out["verdict"] == "AUTHENTICATED" else EXIT_UNKNOWN
        )
        return out

    if verdict == "AUTHENTICATED" and warm:
        nav = navigate_existing_page(cdp_url, dashboard_url)
        out["action"] = "warm_dashboard"
        out["navigate"] = nav
        if nav.get("logged_out"):
            out["verdict"] = "NEEDS_AUTH"
            out["exit_code"] = EXIT_NEEDS_AUTH
            out["hint"] = "Session died mid-warm; ping Dusty."
            return out
        report = check(cdp_url)
        out["preflight_after"] = report
        out["verdict"] = cdp_auth_verdict(report)
        out["exit_code"] = (
            EXIT_OK if out["verdict"] == "AUTHENTICATED" else EXIT_UNKNOWN
        )
        return out

    out["exit_code"] = EXIT_OK if verdict == "AUTHENTICATED" else EXIT_UNKNOWN
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Test SSRobie CDP keep-alive (hermes-test-01 only)"
    )
    parser.add_argument("--cdp-url", default=os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", DEFAULT_CDP_URL))
    parser.add_argument("--dashboard-url", default=TEST_DASHBOARD_URL)
    parser.add_argument(
        "--no-warm",
        action="store_true",
        help="check/restore only; do not soft-navigate when already AUTHENTICATED",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = run_keepalive(
        cdp_url=args.cdp_url,
        dashboard_url=args.dashboard_url,
        warm=not args.no_warm,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Test SSRobie keep-alive")
        print(f"  verdict : {result.get('verdict')}")
        print(f"  action  : {result.get('action')}")
        if result.get("hint"):
            print(f"  hint    : {result['hint']}")
        nav = result.get("navigate") or {}
        if nav:
            print(f"  nav.url : {nav.get('final_url')}")
            print(f"  nav.ok  : {nav.get('ok')}")
        print(f"  exit    : {result.get('exit_code')}")
    code = result.get("exit_code")
    return int(EXIT_UNKNOWN if code is None else code)


if __name__ == "__main__":
    raise SystemExit(main())
