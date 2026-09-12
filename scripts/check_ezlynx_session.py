#!/usr/bin/env python3
"""Is the EZLynx browser session on this host alive? Read-only.

Run this on a schedule so a dead session announces itself, instead of being
discovered by the next job that trips over it. On 2026-09-11 the session
expired quietly and three jobs failed over five hours; each reported a
different downstream symptom and none reported the session.

Two levels of answer:

  tab check (default)  Reads CDP /json/list. Cheap, no side effects, and
                       enough to PROVE a logged-out session — the tab sits on
                       /auth/account/login. Cannot prove the opposite: a URL
                       can look fine over a dead cookie.

  --probe              Opens its own new page, navigates to an authenticated
                       EZLynx URL, records whether it is redirected to the
                       login endpoint, and closes that page. Authoritative in
                       both directions. Needs playwright. Never touches the
                       tabs a job may be using, and never closes the shared
                       browser.

Exit codes are the contract:
  0  session looks usable
  2  session is PROVABLY logged out
  1  could not determine (CDP unreachable, playwright missing, probe failed)

Exit 1 and exit 2 are deliberately different. "I could not tell" and "it is
dead" have different fixes, and collapsing them is how the wrong person gets
paged.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robie_job_engine.session_preflight import (  # noqa: E402
    DEFAULT_CDP_URL,
    LOGGED_OUT,
    LOGIN_PATH,
    check,
)

PROBE_URL = "https://app.ezlynx.com/web/account/220250093/policies"

EXIT_OK = 0
EXIT_UNKNOWN = 1
EXIT_LOGGED_OUT = 2


def probe(cdp_url: str, url: str, timeout_ms: int = 20000) -> dict:
    """Navigate a page of our own and report where we land.

    Authoritative: EZLynx redirects an unauthenticated request to the login
    endpoint, so the final URL answers the question outright.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return {"probe": "UNAVAILABLE", "reason": f"playwright not importable: {exc}"}

    playwright = sync_playwright().start()
    page = None
    try:
        browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=15000)
        contexts = browser.contexts
        if not contexts:
            return {"probe": "UNAVAILABLE", "reason": "Chrome has no browser context"}
        # Our own page. Never reuse or close a tab a job may be holding.
        page = contexts[0].new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(2000)
        final_url = str(page.url or "")
        title = str(page.title() or "")
        logged_out = LOGIN_PATH in final_url
        return {
            "probe": "LOGGED_OUT" if logged_out else "AUTHENTICATED",
            "requested_url": url,
            "final_url": final_url,
            "title": title,
        }
    except Exception as exc:  # noqa: BLE001 - a probe must not take the checker down
        return {"probe": "FAILED", "reason": f"{exc.__class__.__name__}: {exc}"}
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            playwright.stop()
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cdp-url", default=DEFAULT_CDP_URL)
    parser.add_argument(
        "--probe",
        action="store_true",
        help="also navigate an authenticated URL in a page of our own (authoritative)",
    )
    parser.add_argument("--probe-url", default=PROBE_URL)
    parser.add_argument("--json", action="store_true", help="machine-readable output only")
    args = parser.parse_args(argv)

    report = check(args.cdp_url)
    exit_code = EXIT_LOGGED_OUT if report["state"] == LOGGED_OUT else EXIT_OK
    if report["state"] in {"UNREACHABLE", "NO_TABS", "NO_EZLYNX_TAB"}:
        exit_code = EXIT_UNKNOWN

    if args.probe:
        probe_result = probe(args.cdp_url, args.probe_url)
        report["probe_result"] = probe_result
        verdict = probe_result.get("probe")
        if verdict == "LOGGED_OUT":
            exit_code = EXIT_LOGGED_OUT
        elif verdict == "AUTHENTICATED":
            exit_code = EXIT_OK
        elif exit_code == EXIT_OK:
            # Tabs looked fine but the authoritative check could not run.
            # Do not upgrade an unproven "fine" into a pass.
            exit_code = EXIT_UNKNOWN

    report["exit_code"] = exit_code
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return exit_code

    print("EZLynx browser session check")
    print(f"  cdp_url : {report['cdp_url']}")
    print(f"  state   : {report['state']}")
    print(f"  reason  : {report['reason']}")
    for page in report.get("pages", []):
        print(f"  tab     : {page.get('title')!r} {page.get('url')}")
    if "probe_result" in report:
        for key, value in sorted(report["probe_result"].items()):
            print(f"  probe.{key} : {value}")
    print(f"  exit    : {exit_code} "
          f"({ {0: 'usable', 1: 'undetermined', 2: 'LOGGED OUT'}[exit_code] })")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
