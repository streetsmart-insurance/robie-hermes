"""Shared live-option fill for any identified dropdown / select2 / combobox.

Identify the widget, read THAT widget's live options, exact match, else
Gemini names one live option, apply, retry once. HITL if Gemini is unsure
or the retry fails. No alias tables. Never scan every native <select> for
a substring (that is how Line of Business was mistaken for Department).

Department is the first caller. Billing Type uses the same function.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .gemini_field_helper import (
    ask_gemini_live_option,
    exact_live_option,
    normalize_option_text,
)


LOB_WIDGET_ROOT = "#mergeSplitLOB"


@dataclass(frozen=True)
class FieldWidget:
    """Identity of one EZLynx control. Selectors target this widget only."""

    name: str
    root: str
    kind: str  # "select2" | "combobox" | "native_select"
    trigger: str
    option_rows: str
    search: str | None = None
    forbidden_roots: tuple[str, ...] = (LOB_WIDGET_ROOT,)


DEPARTMENT_WIDGET = FieldWidget(
    name="Department",
    root="#Department",
    kind="select2",
    trigger="#Department .select2-choice, #Department a.ui-select-match",
    option_rows="#Department .ui-select-choices-row",
    search="#Department input.ui-select-search",
)

BILLING_TYPE_WIDGET = FieldWidget(
    name="Billing Type",
    root="#BillingType",
    kind="native_select",
    trigger="#BillingType",
    option_rows="#BillingType option",
)


@dataclass
class WidgetFillResult:
    widget: str
    wanted: str
    live_options: list[str] = field(default_factory=list)
    selected: str | None = None
    via: str | None = None
    gemini_asked: bool = False
    gemini_applied: bool = False
    hitl: bool = False
    error: str | None = None
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "widget": self.widget,
            "wanted": self.wanted,
            "live_options": list(self.live_options),
            "selected": self.selected,
            "via": self.via,
            "gemini_asked": self.gemini_asked,
            "gemini_applied": self.gemini_applied,
            "hitl": self.hitl,
            "error": self.error,
            "attempts": self.attempts,
        }


def _hitl_result(
    widget: FieldWidget,
    wanted: str,
    live_options: Iterable[str],
    reason: str,
    *,
    gemini_asked: bool,
) -> WidgetFillResult:
    options = [str(item).strip() for item in live_options if str(item).strip()]
    return WidgetFillResult(
        widget=widget.name,
        wanted=wanted,
        live_options=options,
        gemini_asked=gemini_asked,
        gemini_applied=False,
        hitl=True,
        error=(
            f"{reason} Live options on {widget.root}: {options}. "
            "HITL Carlo. Never guess."
        ),
    )


def _selector_is_forbidden(selector: str, widget: FieldWidget) -> bool:
    text = str(selector or "")
    for root in widget.forbidden_roots:
        token = root.lstrip("#")
        if token and token in text and widget.root.lstrip("#") not in text:
            return True
    return False


def _locator_id(locator: Any) -> str:
    getter = getattr(locator, "get_attribute", None)
    if getter is None:
        return ""
    try:
        value = getter("id")
    except TypeError:
        return ""
    if hasattr(value, "__await__"):
        return ""
    return str(value or "")


async def _count(locator: Any) -> int:
    if locator is None:
        return 0
    count = getattr(locator, "count", None)
    if count is None:
        return 0
    value = count()
    if hasattr(value, "__await__"):
        value = await value
    return int(value or 0)


async def _click(locator: Any) -> None:
    click = getattr(locator, "click", None)
    if click is None:
        raise RuntimeError("locator has no click")
    value = click()
    if hasattr(value, "__await__"):
        await value


async def _inner_text(locator: Any) -> str:
    reader = getattr(locator, "inner_text", None)
    if reader is None:
        return ""
    value = reader()
    if hasattr(value, "__await__"):
        value = await value
    return str(value or "").strip()


async def _all_option_texts(locator: Any) -> list[str]:
    all_inner = getattr(locator, "all_inner_texts", None)
    if all_inner is not None:
        value = all_inner()
        if hasattr(value, "__await__"):
            value = await value
        return [str(item).strip() for item in (value or []) if str(item).strip()]
    all_fn = getattr(locator, "all", None)
    if all_fn is None:
        text = await _inner_text(locator)
        return [text] if text else []
    rows = all_fn()
    if hasattr(rows, "__await__"):
        rows = await rows
    texts: list[str] = []
    for row in rows or []:
        text = await _inner_text(row)
        if text:
            texts.append(text)
    return texts


async def _nth(locator: Any, index: int) -> Any:
    nth = getattr(locator, "nth", None)
    if nth is None:
        return locator
    return nth(index)


async def _require_identified_root(page: Any, widget: FieldWidget) -> Any:
    if _selector_is_forbidden(widget.root, widget):
        raise RuntimeError(
            f"refusing forbidden widget {widget.root}; {widget.name} is {widget.root}"
        )
    root = page.locator(widget.root)
    if await _count(root) < 1:
        raise RuntimeError(
            f"Could not find {widget.name} widget {widget.root}. "
            "Never scan other selects."
        )
    return root


async def read_live_options(page: Any, widget: FieldWidget) -> list[str]:
    """Open the identified widget and return its live option texts."""
    await _require_identified_root(page, widget)
    if widget.kind in {"select2", "combobox"}:
        trigger = page.locator(widget.trigger)
        if await _count(trigger) > 0:
            await _click(trigger)
            wait = getattr(page, "wait_for_timeout", None)
            if wait is not None:
                paused = wait(250)
                if hasattr(paused, "__await__"):
                    await paused
        rows = page.locator(widget.option_rows)
        return await _all_option_texts(rows)
    select = page.locator(widget.root)
    options = select.locator("option") if hasattr(select, "locator") else page.locator(widget.option_rows)
    return await _all_option_texts(options)


async def _select_live_option(page: Any, widget: FieldWidget, option: str) -> None:
    if widget.kind in {"select2", "combobox"}:
        rows = page.locator(widget.option_rows)
        count = await _count(rows)
        matches: list[Any] = []
        for index in range(count):
            row = await _nth(rows, index)
            text = await _inner_text(row)
            if normalize_option_text(text) == normalize_option_text(option):
                matches.append(row)
        if len(matches) != 1:
            raise RuntimeError(
                f"live option {option!r} is not unique on {widget.root} "
                f"({len(matches)} rows)"
            )
        await _click(matches[0])
        return
    select = page.locator(widget.root)
    select_option = getattr(select, "select_option", None)
    if select_option is None:
        raise RuntimeError(f"{widget.root} has no select_option")
    value = select_option(label=option)
    if hasattr(value, "__await__"):
        await value


async def _selected_visible(page: Any, widget: FieldWidget) -> str:
    if widget.kind in {"select2", "combobox"}:
        trigger = page.locator(widget.trigger)
        if await _count(trigger) > 0:
            return await _inner_text(trigger)
        return ""
    select = page.locator(widget.root)
    checked = None
    if hasattr(select, "locator"):
        checked = select.locator("option:checked")
    if checked is not None and await _count(checked) > 0:
        return await _inner_text(checked)
    reader = getattr(select, "input_value", None)
    if reader is None:
        return ""
    value = reader()
    if hasattr(value, "__await__"):
        value = await value
    return str(value or "").strip()


def _choice_matches(selected: str, option: str) -> bool:
    return bool(selected) and normalize_option_text(selected) == normalize_option_text(option)


async def fill_identified_widget(
    page: Any,
    widget: FieldWidget,
    wanted: str,
    *,
    gemini_client: Any | None = None,
) -> WidgetFillResult:
    """Exact live match, else Gemini names one live option, apply, retry once."""
    live_options = await read_live_options(page, widget)
    decision = ask_gemini_live_option(
        widget_name=widget.name,
        wanted=wanted,
        live_options=live_options,
        client=gemini_client,
    )
    if decision.action != "APPLY" or not decision.option:
        return _hitl_result(
            widget,
            wanted,
            live_options,
            decision.reason,
            gemini_asked=decision.gemini_asked,
        )

    last_error = ""
    selected_visible = ""
    for attempt in (1, 2):
        try:
            await _select_live_option(page, widget, decision.option)
            selected_visible = await _selected_visible(page, widget)
            if _choice_matches(selected_visible, decision.option):
                return WidgetFillResult(
                    widget=widget.name,
                    wanted=wanted,
                    live_options=list(live_options),
                    selected=decision.option,
                    via="exact" if not decision.gemini_asked else "gemini",
                    gemini_asked=decision.gemini_asked,
                    gemini_applied=decision.gemini_asked,
                    hitl=False,
                    attempts=attempt,
                )
            last_error = (
                f"selected visible {selected_visible!r} does not match "
                f"live option {decision.option!r}"
            )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt == 1:
            live_options = await read_live_options(page, widget)
            if exact_live_option(decision.option, live_options) is None:
                break

    return _hitl_result(
        widget,
        wanted,
        live_options,
        f"retry still failed after applying {decision.option!r}: {last_error}",
        gemini_asked=decision.gemini_asked,
    )


fill_live_dropdown = fill_identified_widget


