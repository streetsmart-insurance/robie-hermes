#!/usr/bin/env python3
"""Verify and refresh the dedicated Production Magellan browser state safely.

The installed collector remains the sole owner of credentials and authentication.
This wrapper reports only redacted lifecycle metadata and can prove that a saved
session survives a second, independent browser context.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


APP_HOST = "app.magellan.insure"
LOGIN_PATHS = {"/", "/login", "/sign-in", "/signin"}


def _safe_page_state(page, storage_path: Path) -> dict[str, object]:
    parsed = urlsplit(page.url or "")
    password_visible = page.locator('input[type="password"]:visible').count() > 0
    email_visible = page.locator(
        'input[type="email"]:visible, input[placeholder*="email" i]:visible'
    ).count() > 0
    google_visible = page.get_by_role("button", name="Sign in with Google").count() > 0
    microsoft_visible = page.get_by_role("button", name="Sign in with Microsoft").count() > 0
    result: dict[str, object] = {
        "host": parsed.hostname or "missing",
        "path": parsed.path or "/",
        "password_prompt": password_visible,
        "email_prompt": email_visible,
        "google_sso_offered": google_visible,
        "microsoft_sso_offered": microsoft_visible,
        "storage_state_present": storage_path.is_file(),
    }
    if storage_path.is_file():
        stat = storage_path.stat()
        result["storage_state_age_seconds"] = max(0, int(time.time() - stat.st_mtime))
        result["storage_state_mode"] = oct(stat.st_mode & 0o777)
    return result


def _is_authenticated(page) -> bool:
    parsed = urlsplit(page.url or "")
    if parsed.hostname != APP_HOST:
        return False
    if (parsed.path or "/").rstrip("/") in {p.rstrip("/") for p in LOGIN_PATHS}:
        return False
    if page.locator('input[type="password"]:visible').count() > 0:
        return False
    if page.locator('input[type="email"]:visible, input[placeholder*="email" i]:visible').count() > 0:
        return False
    return True


def _open_context(browser, storage_path: Path):
    return (
        browser.new_context(storage_state=str(storage_path))
        if storage_path.is_file()
        else browser.new_context()
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--reliability-attempts", type=int, default=2)
    args = parser.parse_args()
    if args.reliability_attempts < 1 or args.reliability_attempts > 3:
        raise SystemExit("--reliability-attempts must be between 1 and 3")

    root = Path(args.app_root).expanduser().resolve()
    sys.path.insert(0, str(root))

    from playwright.sync_api import sync_playwright
    from src.extractors.magellan_playwright import STORAGE_STATE_PATH, _ensure_authenticated

    evidence: dict[str, object] = {
        "authenticated": False,
        "dashboard_verified": False,
        "storage_state_refreshed": False,
        "reliability_attempts_requested": args.reliability_attempts,
        "reliability_attempts_passed": 0,
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for attempt in range(1, args.reliability_attempts + 1):
                context = _open_context(browser, STORAGE_STATE_PATH)
                page = context.new_page()
                try:
                    _ensure_authenticated(page, context)
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(1500)
                    state = _safe_page_state(page, STORAGE_STATE_PATH)
                    evidence[f"attempt_{attempt}"] = state
                    if not _is_authenticated(page):
                        evidence["failure_stage"] = f"attempt_{attempt}_authentication"
                        raise RuntimeError("Magellan authentication did not reach an authenticated application route")
                    STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                    context.storage_state(path=str(STORAGE_STATE_PATH))
                    os.chmod(STORAGE_STATE_PATH, 0o600)
                    evidence["reliability_attempts_passed"] = attempt
                except Exception as exc:
                    evidence.setdefault(f"attempt_{attempt}", _safe_page_state(page, STORAGE_STATE_PATH))
                    evidence["error_type"] = type(exc).__name__
                    print(json.dumps(evidence, sort_keys=True))
                    return 1
                finally:
                    context.close()
        finally:
            browser.close()

    evidence.update(
        {
            "authenticated": True,
            "dashboard_verified": True,
            "storage_state_refreshed": True,
            "storage_state_mode_verified": oct(STORAGE_STATE_PATH.stat().st_mode & 0o777) == "0o600",
        }
    )
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
