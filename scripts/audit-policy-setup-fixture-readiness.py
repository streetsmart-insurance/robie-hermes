#!/usr/bin/env python3
"""Read-only readiness audit for sanitized EZLynx Policy Setup fixtures.

The script is intentionally incapable of filling or clicking a consequential
control. It opens a fresh page in the already-authenticated Test browser,
reports only generic locator metadata, and closes that page before exiting.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
from pathlib import Path
from urllib.parse import urlparse


EXPECTED_HOST = "hermes-test-01"
EXPECTED_ENV = "TEST"
EXPECTED_ROOT = Path("/opt/streetsmart-hermes-test")
EZLYNX_ROOT = "https://app.ezlynx.com/web/"
SAFE_TERMS = ("applicant", "account", "policy", "add", "new", "search")


def _safe_path(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    redacted = ["<id>" if part.isdigit() and len(part) >= 6 else part for part in parts]
    return "/" + "/".join(redacted)


def _safe_candidate(text: str, href: str | None, role: str) -> dict[str, str | None]:
    folded = " ".join(str(text or "").split())
    if len(folded) > 80:
        folded = folded[:77] + "..."
    if not any(term in folded.casefold() for term in SAFE_TERMS):
        folded = ""
    safe_href = None
    if href:
        parsed = urlparse(href)
        if not parsed.hostname or parsed.hostname.casefold().endswith("ezlynx.com"):
            safe_href = _safe_path(href)
    return {"role": role, "text": folded, "href_path": safe_href}


def require_test_runtime(expected_sha: str) -> Path:
    if socket.gethostname().split(".", 1)[0] != EXPECTED_HOST:
        raise RuntimeError("fixture audit refuses every host except hermes-test-01")
    if str(os.environ.get("ROBIE_ENV") or "").strip().upper() != EXPECTED_ENV:
        raise RuntimeError("fixture audit requires ROBIE_ENV=TEST")
    current = (EXPECTED_ROOT / "current").resolve(strict=True)
    releases_current = (EXPECTED_ROOT / "releases" / "current").resolve(strict=True)
    if current != releases_current:
        raise RuntimeError("Test release pointers disagree")
    expected_prefix = EXPECTED_ROOT / "releases" / expected_sha
    if expected_prefix not in current.parents:
        raise RuntimeError("Test release does not match the approved fixture-audit revision")
    return current


def audit(cdp_url: str) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=15_000)
        pages = [page for context in browser.contexts for page in context.pages]
        ezlynx_pages = [
            page
            for page in pages
            if (urlparse(str(page.url or "")).hostname or "").casefold().endswith(
                "ezlynx.com"
            )
        ]
        if len(ezlynx_pages) != 1:
            raise RuntimeError(
                "PLAYWRIGHT_BLOCKED: expected exactly one EZLynx Test tab; "
                f"observed {len(ezlynx_pages)}"
            )
        seed = ezlynx_pages[0]
        seed_url = str(seed.url or "")
        if "login" in seed_url.casefold() or "signin" in seed_url.casefold():
            raise RuntimeError("AUTH_CHALLENGE: EZLynx Test session is expired")
        if seed.get_by_role("textbox", name="Password", exact=True).count():
            raise RuntimeError("AUTH_CHALLENGE: EZLynx Test session is expired")

        context = seed.context
        page = context.new_page()
        try:
            page.goto(EZLYNX_ROOT, wait_until="domcontentloaded", timeout=20_000)
            if "login" in page.url.casefold() or "signin" in page.url.casefold():
                raise RuntimeError("AUTH_CHALLENGE: Test navigation reached login")
            raw: list[dict[str, str | None]] = []
            for role in ("link", "button"):
                for locator in page.get_by_role(role).all():
                    text = " ".join((locator.inner_text(timeout=2_000) or "").split())
                    if not any(term in text.casefold() for term in SAFE_TERMS):
                        continue
                    raw.append(
                        _safe_candidate(text, locator.get_attribute("href"), role)
                    )
            candidates = [item for item in raw if item["text"]]
            unique = {
                (item["role"], item["text"], item["href_path"]): item
                for item in candidates
            }
            return {
                "result": "TEST READINESS VERIFIED",
                "browser_tabs": len(pages),
                "ezlynx_tabs": len(ezlynx_pages),
                "authenticated": True,
                "seed_host": urlparse(seed_url).hostname,
                "seed_path": _safe_path(seed_url),
                "landing_host": urlparse(page.url).hostname,
                "landing_path": _safe_path(page.url),
                "locator_candidates": list(unique.values()),
                "consequential_writes": 0,
                "production_touched": False,
            }
        finally:
            page.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    args = parser.parse_args()
    current = require_test_runtime(args.expected_sha)
    result = audit(args.cdp_url)
    result["test_release"] = str(current)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
