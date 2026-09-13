"""Dismiss leftover EZLynx Add Note + left applicant rail after CDP attach.

Live hole 2026-09-13: Production Edit Policy (applicant 220250093 /
policy 83669533) stayed covered by the Add Note drawer and the wide left
rail. Dusty closing them in attached Chrome does not persist. This file
proves the helper with mocked Playwright locators.

No live EZLynx. No Production SSH. No Chrome launch, restart, or kill.
No bind, client email, or policy mint.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from robie_job_engine.ezlynx_chrome import (
    ADD_NOTE_BODY_HINT,
    ADD_NOTE_HEADING,
    ASSIGNED_PRODUCER,
    COLLAPSE_ICON,
    adismiss_ezlynx_chrome,
    dismiss_ezlynx_chrome,
)
from robie_job_engine.ezlynx_session import PlaywrightEzlynxSession


ROOT = Path(__file__).resolve().parents[1]
SESSION = ROOT / "robie_job_engine" / "ezlynx_session.py"
CHROME = ROOT / "robie_job_engine" / "ezlynx_chrome.py"
POLICY_SETUP = ROOT / "robie_job_engine" / "ezlynx_policy_setup.py"
PLAYWRIGHT_TOOL = ROOT / "deploy" / "hermes" / "tools" / "playwright_tool.py"
POLICY_TOOL = ROOT / "deploy" / "hermes" / "tools" / "policy_setup_tool.py"
EDIT_URL = (
    "https://app.ezlynx.com/applicantportal/Policy/Actions/Edit/"
    "220250093/83669533"
)


class Element:
    def __init__(
        self,
        *,
        role: str | None = None,
        name: str = "",
        text: str = "",
        tag: str = "div",
        classes: tuple[str, ...] = (),
        kind: str = "",
        visible: bool = True,
        children: list["Element"] | None = None,
    ) -> None:
        self.role = role
        self.name = name
        self.text = text
        self.tag = tag
        self.classes = classes
        self.kind = kind
        self.visible = visible
        self.children = list(children or [])
        self.page: "ChromePage" | None = None
        self.parent: "Element" | None = None
        for child in self.children:
            child.parent = self

    @property
    def accessible_name(self) -> str:
        return self.name

    def attach(self, page: "ChromePage") -> None:
        self.page = page
        for child in self.children:
            child.attach(page)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def click(self) -> None:
        assert self.page is not None
        self.page.clicks.append(self.kind or self.name or self.text)
        if self.kind == "add_note_close":
            self.page.close_add_note()
        elif self.kind == "rail_collapse":
            self.page.collapse_rail()
        elif self.kind == "note_add":
            self.page.note_add_clicks += 1
        elif self.kind == "overview_prev":
            self.page.overview_clicks += 1
        elif self.kind == "save":
            self.page.save_clicks += 1


class Locator:
    def __init__(self, page: "ChromePage", predicate) -> None:
        self.page = page
        self.predicate = predicate
        self.name = ""
        self.text = ""
        self.accessible_name = ""

    def _all_nodes(self) -> list[Element]:
        return [node for node in self.page.walk() if self.predicate(node)]

    def _visible(self) -> list[Element]:
        return [node for node in self._all_nodes() if node.visible]

    def count(self) -> int:
        return len(self._visible())

    def is_visible(self) -> bool:
        return bool(self._visible())

    def all(self) -> list["Locator"]:
        return [Locator(self.page, _is(node)) for node in self._visible()]

    def click(self) -> None:
        visible = self._visible()
        if len(visible) != 1:
            raise RuntimeError(f"locator is not unique ({len(visible)} visible)")
        target = visible[0]
        self.name = target.name
        self.text = target.text
        self.accessible_name = target.name
        target.click()

    def filter(self, has=None) -> "Locator":
        outer = self.predicate

        def pred(node: Element) -> bool:
            if not outer(node):
                return False
            if has is None:
                return True
            return any(has.predicate(child) for child in node.walk())

        return Locator(self.page, pred)

    def get_by_role(self, role: str, name: str | None = None, exact: bool = True) -> "Locator":
        scoped = self

        def pred(node: Element) -> bool:
            if not any(node is item or _is_descendant(node, item) for item in scoped._all_nodes()):
                return False
            return _role_match(node, role, name, exact)

        return Locator(self.page, pred)

    def get_by_text(self, text: str, exact: bool = True) -> "Locator":
        scoped = self

        def pred(node: Element) -> bool:
            if not any(node is item or _is_descendant(node, item) for item in scoped._all_nodes()):
                return False
            return _text_match(node, text, exact)

        return Locator(self.page, pred)

    def locator(self, selector: str) -> "Locator":
        scoped = self

        def pred(node: Element) -> bool:
            if not any(node is item or _is_descendant(node, item) for item in scoped._all_nodes()):
                return False
            return _selector_match(node, selector)

        return Locator(self.page, pred)


class ChromePage:
    def __init__(self, *, add_note_open: bool = False, rail_expanded: bool = False) -> None:
        self.url = EDIT_URL
        self.clicks: list[str] = []
        self.note_add_clicks = 0
        self.overview_clicks = 0
        self.save_clicks = 0
        self.add_note_open = add_note_open
        self.rail_expanded = rail_expanded
        self.elements = _build_tree(self)
        for node in self.elements:
            node.attach(self)

    def walk(self):
        for node in self.elements:
            yield from node.walk()

    def close_add_note(self) -> None:
        self.add_note_open = False
        for node in self.walk():
            if node.kind in {"add_note_pane", "add_note_heading", "add_note_close", "add_note_body"}:
                node.visible = False

    def collapse_rail(self) -> None:
        self.rail_expanded = False
        for node in self.walk():
            if node.kind in {"rail_collapse", "rail_marker"}:
                node.visible = False

    def get_by_role(self, role: str, name: str | None = None, exact: bool = True) -> Locator:
        return Locator(self, lambda node: _role_match(node, role, name, exact))

    def get_by_text(self, text: str, exact: bool = True) -> Locator:
        return Locator(self, lambda node: _text_match(node, text, exact))

    def locator(self, selector: str) -> Locator:
        return Locator(self, lambda node: _selector_match(node, selector))


class AsyncLocator:
    """Wraps a sync locator so adismiss_ezlynx_chrome can await count/click."""

    def __init__(self, inner: Locator) -> None:
        self._inner = inner
        self.name = inner.name
        self.text = inner.text
        self.accessible_name = inner.accessible_name

    async def count(self) -> int:
        return self._inner.count()

    async def is_visible(self) -> bool:
        return self._inner.is_visible()

    async def click(self) -> None:
        self._inner.click()
        self.name = self._inner.name
        self.text = self._inner.text
        self.accessible_name = self._inner.accessible_name

    def all(self) -> list["AsyncLocator"]:
        return [AsyncLocator(item) for item in self._inner.all()]

    def filter(self, has=None) -> "AsyncLocator":
        inner_has = has._inner if isinstance(has, AsyncLocator) else has
        return AsyncLocator(self._inner.filter(has=inner_has))

    def get_by_role(self, role: str, name: str | None = None, exact: bool = True) -> "AsyncLocator":
        return AsyncLocator(self._inner.get_by_role(role, name=name, exact=exact))

    def get_by_text(self, text: str, exact: bool = True) -> "AsyncLocator":
        return AsyncLocator(self._inner.get_by_text(text, exact=exact))

    def locator(self, selector: str) -> "AsyncLocator":
        return AsyncLocator(self._inner.locator(selector))


class AsyncChromePage:
    def __init__(self, *, add_note_open: bool = False, rail_expanded: bool = False) -> None:
        self.inner = ChromePage(add_note_open=add_note_open, rail_expanded=rail_expanded)
        self.url = self.inner.url

    def get_by_role(self, role: str, name: str | None = None, exact: bool = True) -> AsyncLocator:
        return AsyncLocator(self.inner.get_by_role(role, name=name, exact=exact))

    def get_by_text(self, text: str, exact: bool = True) -> AsyncLocator:
        return AsyncLocator(self.inner.get_by_text(text, exact=exact))

    def locator(self, selector: str) -> AsyncLocator:
        return AsyncLocator(self.inner.locator(selector))


def _is(target: Element):
    return lambda node, item=target: node is item


def _is_descendant(node: Element, ancestor: Element) -> bool:
    current = node.parent
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


def _role_match(node: Element, role: str, name: str | None, exact: bool) -> bool:
    if node.role != role:
        return False
    if name is None:
        return True
    if exact:
        return node.name == name
    return name.casefold() in node.name.casefold()


def _text_match(node: Element, text: str, exact: bool) -> bool:
    blob = node.text or node.name
    if exact:
        return blob == text
    return text.casefold() in blob.casefold()


def _selector_match(node: Element, selector: str) -> bool:
    return any(_one_selector(node, part.strip()) for part in selector.split(","))


def _one_selector(node: Element, spec: str) -> bool:
    if spec.startswith("[role="):
        role = spec.split("'", 2)[1] if "'" in spec else spec.split('"', 2)[1]
        return node.role == role
    if spec.startswith("[class*="):
        token = spec.split("'", 2)[1] if "'" in spec else spec.split('"', 2)[1]
        return any(token in item for item in node.classes)
    if spec.startswith("."):
        return spec[1:] in node.classes
    if "." in spec:
        tag, cls = spec.split(".", 1)
        return node.tag == tag and cls in node.classes
    return node.tag == spec


def _build_tree(page: ChromePage) -> list[Element]:
    rail = Element(
        tag="aside",
        classes=("sidenav", "applicant"),
        kind="applicant_rail",
        children=[
            Element(role="heading", name="ROBIE Test LLC", text="ROBIE Test LLC"),
            Element(
                role="button",
                name=COLLAPSE_ICON,
                text=COLLAPSE_ICON,
                kind="rail_collapse",
                visible=page.rail_expanded,
            ),
            Element(
                text=ASSIGNED_PRODUCER,
                kind="rail_marker",
                visible=page.rail_expanded,
            ),
        ],
    )
    main = Element(
        tag="main",
        children=[
            Element(role="heading", name="Edit Policy", text="Edit Policy"),
            Element(role="button", name="Save", text="Save", kind="save"),
            Element(
                role="button",
                name="Add Note",
                text="note_add",
                kind="note_add",
            ),
            Element(
                role="button",
                name="Previous",
                text=COLLAPSE_ICON,
                kind="overview_prev",
            ),
        ],
    )
    drawer = Element(
        tag="aside",
        role="dialog",
        classes=("mat-drawer",),
        kind="add_note_pane",
        visible=page.add_note_open,
        children=[
            Element(
                role="heading",
                name=ADD_NOTE_HEADING,
                text=ADD_NOTE_HEADING,
                kind="add_note_heading",
                visible=page.add_note_open,
            ),
            Element(
                role="button",
                name="Close",
                text="close",
                kind="add_note_close",
                visible=page.add_note_open,
            ),
            Element(
                text=f"{ADD_NOTE_BODY_HINT}...",
                kind="add_note_body",
                visible=page.add_note_open,
            ),
        ],
    )
    return [rail, main, drawer]


class DismissChromeTests(unittest.TestCase):
    def test_open_add_note_drawer_is_closed(self):
        page = ChromePage(add_note_open=True, rail_expanded=False)
        result = dismiss_ezlynx_chrome(page)
        self.assertEqual(result["add_note"], "closed")
        self.assertFalse(page.add_note_open)
        self.assertIn("add_note_close", page.clicks)
        self.assertEqual(page.note_add_clicks, 0)

    def test_already_closed_add_note_is_noop(self):
        page = ChromePage(add_note_open=False, rail_expanded=False)
        result = dismiss_ezlynx_chrome(page)
        self.assertEqual(result["add_note"], "already_closed")
        self.assertEqual(page.clicks, [])
        self.assertEqual(page.note_add_clicks, 0)

    def test_expanded_left_rail_is_collapsed(self):
        page = ChromePage(add_note_open=False, rail_expanded=True)
        result = dismiss_ezlynx_chrome(page)
        self.assertEqual(result["left_rail"], "collapsed")
        self.assertFalse(page.rail_expanded)
        self.assertIn("rail_collapse", page.clicks)
        self.assertEqual(page.overview_clicks, 0)

    def test_already_collapsed_left_rail_is_noop(self):
        page = ChromePage(add_note_open=False, rail_expanded=False)
        result = dismiss_ezlynx_chrome(page)
        self.assertEqual(result["left_rail"], "already_collapsed")
        self.assertEqual(page.clicks, [])
        self.assertEqual(page.overview_clicks, 0)

    def test_both_panels_open_are_cleared(self):
        page = ChromePage(add_note_open=True, rail_expanded=True)
        result = dismiss_ezlynx_chrome(page)
        self.assertEqual(result, {"add_note": "closed", "left_rail": "collapsed"})
        self.assertFalse(page.add_note_open)
        self.assertFalse(page.rail_expanded)
        self.assertEqual(page.note_add_clicks, 0)
        self.assertEqual(page.overview_clicks, 0)
        self.assertEqual(page.save_clicks, 0)

    def test_toolbar_note_add_is_never_clicked(self):
        page = ChromePage(add_note_open=True, rail_expanded=True)
        dismiss_ezlynx_chrome(page)
        self.assertEqual(page.note_add_clicks, 0)
        note_add = page.get_by_role("button", name="Add Note", exact=True)
        self.assertEqual(note_add.count(), 1)
        self.assertTrue(note_add.is_visible())

    def test_overview_pager_chevron_is_never_clicked(self):
        page = ChromePage(add_note_open=False, rail_expanded=True)
        dismiss_ezlynx_chrome(page)
        self.assertEqual(page.overview_clicks, 0)
        leftovers = page.get_by_text(COLLAPSE_ICON, exact=True)
        self.assertEqual(leftovers.count(), 1)
        self.assertEqual(leftovers._visible()[0].kind, "overview_prev")

    def test_async_helper_matches_sync_outcomes(self):
        page = AsyncChromePage(add_note_open=True, rail_expanded=True)
        result = asyncio.run(adismiss_ezlynx_chrome(page))
        self.assertEqual(result, {"add_note": "closed", "left_rail": "collapsed"})
        self.assertFalse(page.inner.add_note_open)
        self.assertFalse(page.inner.rail_expanded)
        self.assertEqual(page.inner.note_add_clicks, 0)

    def test_session_attach_dismisses_without_cdp(self):
        page = ChromePage(add_note_open=True, rail_expanded=True)
        session = object.__new__(PlaywrightEzlynxSession)
        session._page = page
        session._dismiss_blocking_chrome()
        self.assertFalse(page.add_note_open)
        self.assertFalse(page.rail_expanded)

    def test_policy_setup_prepares_edit_surface(self):
        from robie_job_engine.ezlynx_policy_setup import EzlynxPolicySetupPage

        page = AsyncChromePage(add_note_open=True, rail_expanded=True)
        setup = EzlynxPolicySetupPage(page)
        result = asyncio.run(setup._prepare_edit_surface())
        self.assertEqual(result, {"add_note": "closed", "left_rail": "collapsed"})


class AttachWiringTests(unittest.TestCase):
    def test_attach_paths_call_the_helper_once(self):
        session = SESSION.read_text()
        playwright_tool = PLAYWRIGHT_TOOL.read_text()
        policy_tool = POLICY_TOOL.read_text()
        policy_setup = POLICY_SETUP.read_text()
        self.assertIn("dismiss_ezlynx_chrome", session)
        self.assertIn("connect_over_cdp", session)
        self.assertIn("dismiss_ezlynx_chrome(self._page)", session)
        self.assertIn("dismiss_ezlynx_chrome", playwright_tool)
        self.assertIn("adismiss_ezlynx_chrome", policy_tool)
        self.assertIn("_prepare_edit_surface", policy_setup)
        self.assertIn("adismiss_ezlynx_chrome", policy_setup)
        self.assertGreaterEqual(policy_setup.count("await self._prepare_edit_surface()"), 2)
        self.assertIn("await self._prepare_edit_surface()", policy_setup.split("Pre-click: scan every open tab")[0])
        self.assertLess(
            playwright_tool.index("dismiss_ezlynx_chrome(page)"),
            playwright_tool.index("install_playwright_write_guards"),
        )

    def test_chrome_is_never_launched_or_killed(self):
        chrome = CHROME.read_text()
        session = SESSION.read_text()
        for source in (chrome, session):
            self.assertNotIn("chromium.launch", source)
            self.assertNotIn("os.kill", source)
            self.assertNotIn("pkill", source)
        self.assertNotIn("chromium.launch", chrome)
        self.assertNotIn("browser.close", chrome)
        self.assertIn("Disconnect from CDP without closing the server-owned Chrome process", session)
        self.assertIn("self._playwright.stop()", session)
        self.assertIn("connect_over_cdp", session)
        self.assertIn('"note_add"', chrome)
        self.assertNotIn('get_by_text("note_add"', chrome)
        self.assertNotIn('get_by_role("button", name="Add Note"', chrome)

    def test_helper_does_not_mix_department_or_billing_aliases(self):
        chrome = CHROME.read_text()
        self.assertNotIn("Billing Type", chrome)
        self.assertNotIn("Department", chrome)
        self.assertNotIn("Direct Bill", chrome)


if __name__ == "__main__":
    unittest.main()
