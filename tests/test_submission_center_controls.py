"""Mocked MDC activation path for the EZLynx Submission Center runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from robie_job_engine.submission_center_controls import (
    MDC_CHECKBOX_INPUT,
    MDC_SORT_HEADER_CONTAINER,
    activate_mdc_checkbox,
    activate_mdc_combobox,
    activate_sort_header,
    force_click,
    pick_exact_labeled_option,
)


class FakeLocator:
    def __init__(
        self,
        *,
        visible: bool = True,
        count: int = 1,
        children: dict | None = None,
        aria_sort: str = "none",
        click_error: Exception | None = None,
        press_error: Exception | None = None,
        press_noop: bool = False,
    ) -> None:
        self._visible = visible
        self._count = count
        self._children = children or {}
        self._aria_sort = aria_sort
        self.click_error = click_error
        self.press_error = press_error
        self.press_noop = press_noop
        self.clicks: list[dict] = []
        self.presses: list[str] = []
        self.scrolled = False

    def count(self) -> int:
        return self._count

    def nth(self, index: int) -> FakeLocator:
        return self

    def is_visible(self) -> bool:
        return self._visible

    def scroll_into_view_if_needed(self) -> None:
        self.scrolled = True

    def press(self, key: str, timeout: int | None = None) -> None:
        if self.press_error:
            raise self.press_error
        self.presses.append(key)
        if self.press_noop:
            return
        cycle = {"none": "ascending", "ascending": "descending", "descending": "ascending"}
        self._aria_sort = cycle.get(self._aria_sort, "ascending")

    def click(self, timeout: int | None = None, force: bool = False) -> None:
        if self.click_error:
            raise self.click_error
        self.clicks.append({"force": force, "timeout": timeout})
        cycle = {"none": "ascending", "ascending": "descending", "descending": "ascending"}
        self._aria_sort = cycle.get(self._aria_sort, "ascending")

    def locator(self, selector: str) -> FakeLocator:
        if selector in self._children:
            return self._children[selector]
        return FakeLocator(count=0, visible=False)

    def get_attribute(self, name: str) -> str | None:
        if name == "aria-sort":
            return self._aria_sort
        return None


def test_mdc_checkbox_force_clicks_nested_input_not_space():
    nested = FakeLocator()
    option = FakeLocator(children={MDC_CHECKBOX_INPUT: nested})
    activate_mdc_checkbox(option, "My Submissions checkbox")
    assert nested.clicks == [{"force": True, "timeout": 5000}]
    assert option.clicks == []
    assert option.presses == []


def test_mdc_combobox_force_clicks_page_size_control():
    control = FakeLocator()
    activate_mdc_combobox(control, "page-size control")
    option = FakeLocator()
    activate_mdc_combobox(option, "100 page-size option")
    assert control.clicks == [{"force": True, "timeout": 5000}]
    assert option.clicks == [{"force": True, "timeout": 5000}]


def test_sort_header_force_clicks_container_when_enter_is_noop():
    container = FakeLocator()
    header = FakeLocator(
        aria_sort="none",
        press_noop=True,
        children={MDC_SORT_HEADER_CONTAINER: container},
    )
    activate_sort_header(header, "Status sort header")
    assert header.presses == []
    assert container.clicks == [{"force": True, "timeout": 5000}]


def test_sort_header_force_clicks_container_even_when_enter_would_work():
    """Live Status sort ignores Enter. The one-off launcher force-clicks first."""
    container = FakeLocator()
    header = FakeLocator(
        aria_sort="none",
        press_noop=False,
        children={MDC_SORT_HEADER_CONTAINER: container},
    )
    activate_sort_header(header, "Status sort header")
    assert header.presses == []
    assert container.clicks == [{"force": True, "timeout": 5000}]


def test_sort_header_uses_enter_when_mdc_container_is_absent():
    header = FakeLocator(aria_sort="none", press_noop=False)
    activate_sort_header(header, "Status sort header")
    assert header.presses == ["Enter"]
    assert header.get_attribute("aria-sort") == "ascending"


class _Option:
    def __init__(self, text: str) -> None:
        self._text = text

    def inner_text(self) -> str:
        return self._text


class _Options:
    def __init__(self, texts: list[str]) -> None:
        self._items = [_Option(text) for text in texts]

    def count(self) -> int:
        return len(self._items)

    def nth(self, index: int) -> _Option:
        return self._items[index]


def test_page_size_option_scan_accepts_exact_100_only():
    chosen = pick_exact_labeled_option(_Options(["10", " 100 ", "1000"]), "100")
    assert chosen is not None
    assert chosen.inner_text() == " 100 "
    assert pick_exact_labeled_option(_Options(["10", "25"]), "100") is None


def test_force_click_stays_playwright_blocked_when_verify_target_fails():
    locator = FakeLocator(click_error=RuntimeError("intercepted"))
    with pytest.raises(RuntimeError, match="PLAYWRIGHT_BLOCKED: visible page-size control could not be activated"):
        force_click(locator, "page-size control")


def test_runner_wires_mdc_helpers_and_keeps_page_size_verify():
    source = (
        Path(__file__).resolve().parents[1]
        / "robie_job_engine"
        / "submission_audit_runner.py"
    ).read_text()
    assert "activate_mdc_checkbox(mine, \"My Submissions checkbox\")" in source
    assert "activate_mdc_checkbox(agency, \"Streetsmart Insurance checkbox\")" in source
    assert "activate_mdc_combobox(selector.first, \"page-size control\")" in source
    assert "activate_sort_header(header, \"Status sort header\")" in source
    assert "_verify_page_size_result(page)" in source
    assert "PLAYWRIGHT_BLOCKED: page-size selection expected" in source
