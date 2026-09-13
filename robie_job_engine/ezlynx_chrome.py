"""Dismiss leftover EZLynx chrome that covers the Edit form.

Live evidence 2026-09-13, applicant 220250093 / policy 83669533 Edit:
the right-side Add Note drawer and the wide left applicant rail sit on top
of Edit Policy across jobs. Dusty closing them in Production Chrome does
not persist. This helper runs after CDP attach (and before the first fill,
screenshot, or Save) so Robie does it on every attach.

It only dismisses chrome that is already open:
- Add Note: click the drawer X. Do not remove Add Note from the product.
  Do not click note_add / the toolbar Add Note control.
- Left applicant rail: click the chevron_left collapse on the expanded
  card. Do not delete the rail. A later job can expand it when it needs
  the applicant card. The leftover thin icon strip is OK.

Already-closed / already-collapsed is a no-op. Never navigates, never
launches or kills Chrome, never binds, never emails, never mints a policy.
"""

from __future__ import annotations

import inspect
from typing import Any


ADD_NOTE_HEADING = "Add Note"
ADD_NOTE_BODY_HINT = "Type your note here"
ASSIGNED_PRODUCER = "Assigned Producer"
COLLAPSE_ICON = "chevron_left"

# Drawer / overlay wrappers observed around the Add Note heading.
ADD_NOTE_PANE = (
    "mat-drawer, mat-sidenav, [role='dialog'], .mat-drawer, "
    ".cdk-overlay-pane, aside"
)
# Left applicant card wrappers. Scoped by Assigned Producer so the
# Overview pager chevron is never a candidate.
APPLICANT_RAIL = (
    "mat-sidenav, mat-drawer, aside, nav, [class*='sidenav'], "
    "[class*='applicant']"
)

# Toolbar / product controls this helper must never touch.
_FORBIDDEN_CLICK_NAMES = frozenset(
    {
        "note_add",
        "add note",
        "save",
        "save & continue edit",
        "bind",
    }
)


def dismiss_ezlynx_chrome(page: Any) -> dict[str, Any]:
    """Sync Playwright: close Add Note if open; collapse the left rail if expanded."""
    return _DismissDriver(page).run_sync()


async def adismiss_ezlynx_chrome(page: Any) -> dict[str, Any]:
    """Async Playwright twin of :func:`dismiss_ezlynx_chrome`."""
    return await _DismissDriver(page).run_async()


class _DismissDriver:
    def __init__(self, page: Any) -> None:
        self.page = page

    def run_sync(self) -> dict[str, Any]:
        try:
            add_note = self._dismiss_add_note_sync()
            left_rail = self._collapse_left_rail_sync()
            return {"add_note": add_note, "left_rail": left_rail}
        except Exception as exc:  # noqa: BLE001
            return {
                "add_note": "skipped",
                "left_rail": "skipped",
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def run_async(self) -> dict[str, Any]:
        try:
            add_note = await self._dismiss_add_note_async()
            left_rail = await self._collapse_left_rail_async()
            return {"add_note": add_note, "left_rail": left_rail}
        except Exception as exc:  # noqa: BLE001
            return {
                "add_note": "skipped",
                "left_rail": "skipped",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _dismiss_add_note_sync(self) -> str:
        if not self._add_note_is_open_sync():
            return "already_closed"
        close = self._add_note_close_sync()
        if close == "ambiguous":
            return "ambiguous"
        if close is None:
            return "close_missing"
        return self._click_allowed_sync(close, "closed")

    async def _dismiss_add_note_async(self) -> str:
        if not await self._add_note_is_open_async():
            return "already_closed"
        close = await self._add_note_close_async()
        if close == "ambiguous":
            return "ambiguous"
        if close is None:
            return "close_missing"
        return await self._click_allowed_async(close, "closed")

    def _collapse_left_rail_sync(self) -> str:
        rail = self._expanded_rail_sync()
        if rail is None:
            return "already_collapsed"
        if rail == "ambiguous":
            return "ambiguous"
        icon = self._unique_visible_sync(_get_by_text(rail, COLLAPSE_ICON, exact=True))
        if icon == "ambiguous":
            return "ambiguous"
        if icon is None:
            return "already_collapsed"
        return self._click_allowed_sync(icon, "collapsed")

    async def _collapse_left_rail_async(self) -> str:
        rail = await self._expanded_rail_async()
        if rail is None:
            return "already_collapsed"
        if rail == "ambiguous":
            return "ambiguous"
        icon = await self._unique_visible_async(
            _get_by_text(rail, COLLAPSE_ICON, exact=True)
        )
        if icon == "ambiguous":
            return "ambiguous"
        if icon is None:
            return "already_collapsed"
        return await self._click_allowed_async(icon, "collapsed")

    def _add_note_is_open_sync(self) -> bool:
        heading = _get_by_role(self.page, "heading", ADD_NOTE_HEADING, exact=True)
        if self._is_visible_sync(heading):
            return True
        body = _get_by_text(self.page, ADD_NOTE_BODY_HINT, exact=False)
        return self._is_visible_sync(body)

    async def _add_note_is_open_async(self) -> bool:
        heading = _get_by_role(self.page, "heading", ADD_NOTE_HEADING, exact=True)
        if await self._is_visible_async(heading):
            return True
        body = _get_by_text(self.page, ADD_NOTE_BODY_HINT, exact=False)
        return await self._is_visible_async(body)

    def _add_note_close_sync(self):
        pane = self._add_note_pane_sync()
        scope = pane if pane not in (None, "ambiguous") else self.page
        if pane == "ambiguous":
            return "ambiguous"
        return self._unique_visible_sync(_close_button(scope))

    async def _add_note_close_async(self):
        pane = await self._add_note_pane_async()
        scope = pane if pane not in (None, "ambiguous") else self.page
        if pane == "ambiguous":
            return "ambiguous"
        return await self._unique_visible_async(_close_button(scope))

    def _add_note_pane_sync(self):
        heading = _get_by_role(self.page, "heading", ADD_NOTE_HEADING, exact=True)
        panes = _locator(self.page, ADD_NOTE_PANE)
        filtered = _filter_has(panes, heading)
        if filtered is None:
            return None
        return self._unique_visible_sync(filtered)

    async def _add_note_pane_async(self):
        heading = _get_by_role(self.page, "heading", ADD_NOTE_HEADING, exact=True)
        panes = _locator(self.page, ADD_NOTE_PANE)
        filtered = _filter_has(panes, heading)
        if filtered is None:
            return None
        return await self._unique_visible_async(filtered)

    def _expanded_rail_sync(self):
        marker = _get_by_text(self.page, ASSIGNED_PRODUCER, exact=True)
        if not self._is_visible_sync(marker):
            return None
        rails = _locator(self.page, APPLICANT_RAIL)
        filtered = _filter_has(rails, marker)
        if filtered is None:
            return None
        return self._unique_visible_sync(filtered)

    async def _expanded_rail_async(self):
        marker = _get_by_text(self.page, ASSIGNED_PRODUCER, exact=True)
        if not await self._is_visible_async(marker):
            return None
        rails = _locator(self.page, APPLICANT_RAIL)
        filtered = _filter_has(rails, marker)
        if filtered is None:
            return None
        return await self._unique_visible_async(filtered)

    def _click_allowed_sync(self, target: Any, success: str) -> str:
        if _looks_forbidden(target):
            return "refused_forbidden_control"
        target.click()
        return success

    async def _click_allowed_async(self, target: Any, success: str) -> str:
        if _looks_forbidden(target):
            return "refused_forbidden_control"
        clicked = target.click()
        if inspect.isawaitable(clicked):
            await clicked
        return success

    def _is_visible_sync(self, loc: Any) -> bool:
        if loc is None:
            return False
        try:
            visible = loc.is_visible()
        except Exception:  # noqa: BLE001
            return False
        return bool(visible)

    async def _is_visible_async(self, loc: Any) -> bool:
        if loc is None:
            return False
        try:
            visible = loc.is_visible()
            if inspect.isawaitable(visible):
                visible = await visible
        except Exception:  # noqa: BLE001
            return False
        return bool(visible)

    def _unique_visible_sync(self, loc: Any):
        if loc is None:
            return None
        try:
            n = int(loc.count())
        except Exception:  # noqa: BLE001
            return self._unique_from_all_sync(loc)
        if n == 0:
            return None
        if n == 1:
            return loc if self._is_visible_sync(loc) else None
        return self._unique_from_all_sync(loc)

    async def _unique_visible_async(self, loc: Any):
        if loc is None:
            return None
        try:
            n = loc.count()
            if inspect.isawaitable(n):
                n = await n
            n = int(n)
        except Exception:  # noqa: BLE001
            return await self._unique_from_all_async(loc)
        if n == 0:
            return None
        if n == 1:
            return loc if await self._is_visible_async(loc) else None
        return await self._unique_from_all_async(loc)

    def _unique_from_all_sync(self, loc: Any):
        collector = getattr(loc, "all", None)
        if not callable(collector):
            return loc if self._is_visible_sync(loc) else None
        visible = [item for item in collector() if self._is_visible_sync(item)]
        if not visible:
            return None
        if len(visible) == 1:
            return visible[0]
        return "ambiguous"

    async def _unique_from_all_async(self, loc: Any):
        collector = getattr(loc, "all", None)
        if not callable(collector):
            return loc if await self._is_visible_async(loc) else None
        items = collector()
        if inspect.isawaitable(items):
            items = await items
        visible = []
        for item in items:
            if await self._is_visible_async(item):
                visible.append(item)
        if not visible:
            return None
        if len(visible) == 1:
            return visible[0]
        return "ambiguous"


def _get_by_role(owner: Any, role: str, name: str, *, exact: bool) -> Any:
    getter = getattr(owner, "get_by_role", None)
    if not callable(getter):
        return None
    try:
        return getter(role, name=name, exact=exact)
    except TypeError:
        return getter(role, name=name)


def _get_by_text(owner: Any, text: str, *, exact: bool) -> Any:
    if owner is None:
        return None
    getter = getattr(owner, "get_by_text", None)
    if not callable(getter):
        return None
    try:
        return getter(text, exact=exact)
    except TypeError:
        return getter(text)


def _locator(owner: Any, selector: str) -> Any:
    getter = getattr(owner, "locator", None)
    if not callable(getter):
        return None
    return getter(selector)


def _filter_has(locator: Any, has: Any) -> Any:
    if locator is None or has is None:
        return None
    filtr = getattr(locator, "filter", None)
    if not callable(filtr):
        return None
    try:
        return filtr(has=has)
    except TypeError:
        return None


def _close_button(owner: Any) -> Any:
    return _get_by_role(owner, "button", "Close", exact=True)


def _looks_forbidden(target: Any) -> bool:
    name = " ".join(
        str(getattr(target, attr, "") or "")
        for attr in ("name", "accessible_name", "aria_label")
    ).casefold()
    text = str(getattr(target, "text", "") or "").casefold()
    blob = f"{name} {text}"
    return any(token in blob for token in _FORBIDDEN_CLICK_NAMES)
