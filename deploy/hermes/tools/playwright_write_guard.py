"""Fail closed when Playwright would write a field it did not uniquely identify.

Hermes generates locator code at runtime. Playwright strict mode still lets
``.first`` / ``.nth()`` / ``.last`` pick an arbitrary match. That is how a
policy form can be filled on the wrong insured, coverage, or date field.
Writes must name exactly one target; positional guesses are refused.
"""

from __future__ import annotations

from typing import Any, Callable


WRITE_METHODS = (
    "fill",
    "type",
    "press_sequentially",
    "select_option",
    "check",
    "uncheck",
    "set_input_files",
    "clear",
)
POSITIONAL_MARKERS = ("nth=", " >> nth", ".first", ".last")
PLAYWRIGHT_BLOCKED = "PLAYWRIGHT_BLOCKED"


def locator_selector_text(target: Any) -> str:
    """Best-effort selector string from a Playwright locator or test double."""
    for attr in ("_selector", "selector"):
        value = getattr(target, attr, None)
        if value:
            return str(value)
    impl = getattr(target, "_impl_obj", None)
    if impl is not None:
        for attr in ("_selector", "selector"):
            value = getattr(impl, attr, None)
            if value:
                return str(value)
    return str(target)


def locator_is_positional_guess(target: Any) -> bool:
    """True when the locator resolved ambiguity by position instead of identity."""
    text = locator_selector_text(target).casefold()
    return any(marker in text for marker in POSITIONAL_MARKERS)


def unique_write_block_reason(
    target: Any,
    *,
    count: int | None = None,
    selector: str | None = None,
) -> str | None:
    """Return a PLAYWRIGHT_BLOCKED reason if this write must not proceed."""
    resolved = target
    if selector:
        locator_fn = getattr(target, "locator", None)
        if callable(locator_fn):
            resolved = locator_fn(selector)
        elif locator_is_positional_guess(selector):
            return (
                f"{PLAYWRIGHT_BLOCKED}: write target was chosen by position, "
                "not unique identity"
            )
    if locator_is_positional_guess(resolved) or (
        selector and locator_is_positional_guess(selector)
    ):
        return (
            f"{PLAYWRIGHT_BLOCKED}: write target was chosen by position, "
            "not unique identity"
        )
    observed = count
    if observed is None:
        count_fn = getattr(resolved, "count", None)
        if not callable(count_fn):
            return (
                f"{PLAYWRIGHT_BLOCKED}: write target could not be uniquely "
                "identified; refuse to guess"
            )
        observed = count_fn()
    try:
        observed_n = int(observed)
    except (TypeError, ValueError):
        return (
            f"{PLAYWRIGHT_BLOCKED}: write target count is unusable; "
            "refuse to guess"
        )
    if observed_n != 1:
        return (
            f"{PLAYWRIGHT_BLOCKED}: write target matched {observed_n} fields; "
            "refuse to guess"
        )
    return None


def require_unique_write_target(
    target: Any,
    *,
    count: int | None = None,
    selector: str | None = None,
) -> None:
    """Raise if a form write would guess among zero or many fields."""
    reason = unique_write_block_reason(target, count=count, selector=selector)
    if reason:
        raise RuntimeError(reason)


def _wrap_write(method: Callable[..., Any], *, page_level: bool) -> Callable[..., Any]:
    def wrapped(self, *args, **kwargs):
        selector = args[0] if page_level and args else None
        require_unique_write_target(self, selector=selector)
        return method(self, *args, **kwargs)

    wrapped.__name__ = getattr(method, "__name__", "write")
    wrapped.__qualname__ = getattr(method, "__qualname__", wrapped.__name__)
    return wrapped


def install_playwright_write_guards(scope: dict[str, Any]) -> dict[str, Any]:
    """Patch Locator/Page write methods in a Playwright exec scope."""
    patched: dict[str, Any] = {}
    for name in ("Locator", "Page"):
        cls = scope.get(name)
        if cls is None:
            continue
        page_level = name == "Page"
        for method_name in WRITE_METHODS:
            original = getattr(cls, method_name, None)
            if not callable(original):
                continue
            setattr(cls, method_name, _wrap_write(original, page_level=page_level))
            patched[f"{name}.{method_name}"] = True
    scope["_robie_unique_write_guard"] = True
    scope["require_unique_write_target"] = require_unique_write_target
    return patched
