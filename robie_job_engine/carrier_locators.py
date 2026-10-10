"""Case-insensitive locator helpers shared by the carrier document pull workers.

Carrier portals change label casing without notice: Utica First's UFirst Now
shows "Policy Transactions" and "Filter List" where the worker was written
against "POLICY TRANSACTIONS" and "FILTER LIST" (found live on hermes-test-01,
2026-10-07). Every helper here tries the label exactly first (so existing
behaviour and fixtures are unchanged), then a case- and whitespace-insensitive
full-label match. Zero or several matches still hold: these helpers never
guess between two different controls.
"""
from __future__ import annotations

import re
from typing import Any

from .intake_core import IntakeHold


def ci_label(label: str) -> re.Pattern[str]:
    """Full-label, case-insensitive, whitespace-tolerant pattern for ``label``."""
    words = [re.escape(part) for part in str(label or "").split()]
    if not words:
        raise ValueError("locator label must not be empty")
    return re.compile(r"^\s*" + r"\s+".join(words) + r"\s*$", re.IGNORECASE)


def locator_count(locator: Any) -> int:
    try:
        return int(locator.count())
    except Exception:
        return -1


def _visible(locator: Any, count: int) -> list[Any]:
    visible: list[Any] = []
    for index in range(max(count, 0)):
        try:
            node = locator.nth(index)
            if node.is_visible():
                visible.append(node)
        except Exception:
            continue
    return visible


def unique_control_ci(
    page: Any,
    role: str,
    label: str,
    *,
    carrier: str,
    text_fallback: bool = False,
    first_visible_text: bool = False,
) -> Any:
    """Return the one control with ``role`` named ``label`` in any casing.

    Order: exact accessible name, then case-insensitive accessible name.
    With ``text_fallback`` the visible text is tried last (ExtJS tabs often
    expose no ARIA role). ``first_visible_text`` is only for pure navigation
    clicks (a tab), where a wrong click is caught by the next page check:
    if the text matches several nodes, the first visible one is used.
    """
    for locator in (
        page.get_by_role(role, name=label, exact=True),
        page.get_by_role(role, name=ci_label(label)),
    ):
        count = locator_count(locator)
        if count == 1:
            return locator
        if count > 1:
            raise IntakeHold(f"{carrier} control {label!r} is missing or ambiguous")
    if text_fallback:
        locator = page.get_by_text(ci_label(label))
        count = locator_count(locator)
        if count == 1:
            return locator
        if count > 1:
            visible = _visible(locator, count)
            if len(visible) == 1 or (visible and first_visible_text):
                return visible[0]
    raise IntakeHold(f"{carrier} control {label!r} is missing or ambiguous")


def wait_for_text_ci(page: Any, text: str, *, timeout_ms: int) -> bool:
    """Wait for ``text`` (any casing, substring) to be visible. Never raises."""
    words = [re.escape(part) for part in str(text or "").split()]
    pattern = re.compile(r"\s*".join(words), re.IGNORECASE)
    try:
        page.get_by_text(pattern).first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False
