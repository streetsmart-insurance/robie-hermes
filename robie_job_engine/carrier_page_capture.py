"""Shared screenshot helper for the carrier pull workers.

Live 2026-10-08 (hermes-test-01, headless carrier Chrome on CDP 9223 with
one tab per carrier): ``page.screenshot`` on a tab that is not in front
hangs at "waiting for fonts to load... fonts loaded" until it times out,
even for a viewport-only capture. Bringing the tab to front first fixes it.
"""

from __future__ import annotations

from typing import Any

SCREENSHOT_TIMEOUT_MS = 15000


def bring_to_front(page: Any) -> None:
    front = getattr(page, "bring_to_front", None)
    if callable(front):
        try:
            front()
        except Exception:
            pass


def capture_png(page: Any, *, full_page: bool = True) -> bytes:
    """Front the tab, then screenshot; a hung full-page capture falls back to the viewport."""
    bring_to_front(page)
    try:
        return bytes(page.screenshot(full_page=full_page, type="png", timeout=SCREENSHOT_TIMEOUT_MS) or b"")
    except TypeError:
        # Test fakes without a timeout keyword.
        return bytes(page.screenshot(full_page=full_page, type="png") or b"")
    except Exception as exc:
        if not full_page or "timeout" not in type(exc).__name__.lower():
            raise
    return bytes(page.screenshot(full_page=False, type="png", timeout=SCREENSHOT_TIMEOUT_MS) or b"")
