#!/usr/bin/env python3
"""Verify and refresh the dedicated Production Magellan browser state.

This script imports the installed standalone collector so credentials continue to
come from its existing Secret Manager implementation. It prints no credential,
cookie, call, employee, or client data.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-root", required=True)
    args = parser.parse_args()

    root = Path(args.app_root).expanduser().resolve()
    sys.path.insert(0, str(root))

    from playwright.sync_api import sync_playwright
    from src.extractors.magellan_playwright import (
        STORAGE_STATE_PATH,
        _ensure_authenticated,
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = (
            browser.new_context(storage_state=str(STORAGE_STATE_PATH))
            if STORAGE_STATE_PATH.exists()
            else browser.new_context()
        )
        page = context.new_page()
        _ensure_authenticated(page, context)
        page.wait_for_load_state("domcontentloaded")
        authenticated = (
            page.url.startswith("https://app.magellan.insure/dashboard")
            and page.locator('input[type="password"]').count() == 0
        )
        if not authenticated:
            raise RuntimeError("Magellan authentication did not reach the verified dashboard")
        STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(STORAGE_STATE_PATH))
        os.chmod(STORAGE_STATE_PATH, 0o600)
        browser.close()

    print(
        json.dumps(
            {
                "authenticated": True,
                "dashboard_verified": True,
                "storage_state_refreshed": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
