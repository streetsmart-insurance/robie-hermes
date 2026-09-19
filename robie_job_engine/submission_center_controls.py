"""MDC / Material UI activation helpers for the EZLynx Submission Center.

Live ``mat-mdc-checkbox``, paginator comboboxes, and Status sort headers ignore
Space/Enter. Force-click the nested native control (or sort container) and keep
fail-closed ``PLAYWRIGHT_BLOCKED`` when the caller’s post-action verify fails.
These helpers are duck-typed so unit tests do not import Playwright.
"""

from __future__ import annotations

from typing import Any

PLAYWRIGHT_BLOCKED_PREFIX = "PLAYWRIGHT_BLOCKED:"
MDC_CHECKBOX_INPUT = "input[type=checkbox], input.mdc-checkbox__native-control"
MDC_SORT_HEADER_CONTAINER = (
    ".mat-sort-header-container, .mat-mdc-sort-header-container"
)


def blocked(message: str) -> RuntimeError:
    return RuntimeError(f"{PLAYWRIGHT_BLOCKED_PREFIX} {message}")


def first_visible(locator: Any, label: str) -> Any:
    for index in range(locator.count()):
        candidate = locator.nth(index)
        if candidate.is_visible():
            return candidate
    raise blocked(f"visible {label} not found")


def force_click(locator: Any, label: str) -> None:
    """Click through the MDC touch-target layer. Keyboard activation is a no-op."""
    target = first_visible(locator, label)
    scroll = getattr(target, "scroll_into_view_if_needed", None)
    if callable(scroll):
        scroll()
    try:
        try:
            target.click(timeout=5_000, force=True)
        except TypeError:
            target.click(force=True)
    except Exception as exc:
        raise blocked(f"visible {label} could not be activated") from exc


def nested_checkbox_input(option: Any) -> Any | None:
    locator = getattr(option, "locator", None)
    if not callable(locator):
        return None
    nested = locator(MDC_CHECKBOX_INPUT)
    if nested is not None and nested.count() > 0:
        return nested
    return None


def activate_mdc_checkbox(option: Any, label: str) -> None:
    """Toggle a live ``mat-mdc-checkbox``. Space/Enter do not change state."""
    nested = nested_checkbox_input(option)
    if nested is not None:
        force_click(nested, f"{label} input")
        return
    force_click(option, label)


def activate_mdc_combobox(control: Any, label: str) -> None:
    """Open or choose a live MDC paginator/select option by force-click."""
    force_click(control, label)


def activate_sort_header(header: Any, label: str) -> None:
    """Force-click ``.mat-sort-header-container`` when Enter is a no-op."""
    before = str(header.get_attribute("aria-sort") or "none").casefold()
    target = first_visible(header, label)
    scroll = getattr(target, "scroll_into_view_if_needed", None)
    if callable(scroll):
        scroll()
    pressed = False
    try:
        try:
            target.press("Enter", timeout=5_000)
        except TypeError:
            target.press("Enter")
        pressed = True
    except Exception:
        pressed = False
    after = str(header.get_attribute("aria-sort") or "none").casefold()
    if pressed and after != before:
        return
    container = header.locator(MDC_SORT_HEADER_CONTAINER)
    if container is not None and container.count() > 0:
        force_click(container, f"{label} container")
        return
    force_click(header, label)
