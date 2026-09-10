"""Production read ports for Chat-path verifiers.

The Chat verification path historically registered no verifiers, so browser
jobs fell to UNVERIFIED with "no independent verifier registered" even when
the work succeeded. The ports here let ``_default_chat_verifiers()`` register
real verifiers built on the persistent Hermes Chrome session (CDP), the same
session the workers drive.

Read-only by construction: navigate + snapshot only. They never fill, click,
or otherwise write. Any failure raises so the Job Engine fails closed into
WAITING/UNVERIFIED instead of claiming success.
"""

from __future__ import annotations

import os
from typing import Any


def _cdp_url() -> str:
    return (
        os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL")
        or os.environ.get("ROBIE_BROWSER_CDP_URL")
        or "http://127.0.0.1:9222"
    )


class CdpReadPort:
    """Read-only browser port over the persistent Chrome CDP endpoint.

    Implements the ``BrowserReadPort`` protocol used by
    ``BrowserReadVerifier``. Connects to the server-owned Chrome, performs a
    fresh navigation (or reload) of the destination, and snapshots the
    server-backed URL/title. Disconnects without closing Chrome.
    """

    def __init__(self, cdp_url: str | None = None):
        self._cdp_url = cdp_url or _cdp_url()

    def read_fresh(self, locator: dict[str, Any]) -> dict[str, Any]:
        url = str((locator or {}).get("url") or "").strip()
        if not url:
            raise ValueError("browser read requires a destination locator url")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("playwright is required for browser verification") from exc
        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.connect_over_cdp(self._cdp_url, timeout=15000)
            contexts = browser.contexts
            if not contexts:
                raise RuntimeError("Chrome has no browser context")
            context = contexts[0]
            pages = [page for ctx in browser.contexts for page in ctx.pages]
            page = None
            for candidate in pages:
                try:
                    if candidate.url and candidate.url.startswith(url):
                        page = candidate
                        break
                except Exception:
                    continue
            if page is None:
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            else:
                # Fresh, server-backed state: never trust a stale DOM.
                page.reload(wait_until="domcontentloaded", timeout=30000)
            return {
                "url": page.url,
                "title": page.title(),
                "engine": "playwright",
            }
        finally:
            playwright.stop()
