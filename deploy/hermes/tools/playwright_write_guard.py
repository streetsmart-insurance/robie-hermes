"""Fail closed when Playwright would write a field it did not uniquely identify.

Hermes generates locator code at runtime. Playwright strict mode still lets
``.first`` / ``.nth()`` / ``.last`` pick an arbitrary match. That is how a
policy form can be filled on the wrong insured, coverage, or date field.
Writes must name exactly one target; positional guesses are refused.

When a write is PLAYWRIGHT_BLOCKED, the guard may ask Gemini for one unique
visible label and apply that locator only after unique-write still passes.
If Gemini is missing, unsure, or the locator is not unique, the write is
refused and the Job HITLs Carlo. Unique-write is never disabled.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
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
HITL_OPERATOR = "Carlo"
_SECRET_LABEL = re.compile(
    r"\b(password|passwd|pwd|mfa|otp|totp|one[- ]time|secret|token|ssn|fein)\b",
    re.IGNORECASE,
)


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


def _safe_visible_label(value: str) -> str | None:
    text = str(value or "").strip()
    if not text or _SECRET_LABEL.search(text):
        return None
    return text[:160]


def _page_from_target(target: Any, *, page_level: bool) -> Any | None:
    if page_level:
        return target
    for attr in ("page", "_page"):
        page = getattr(target, attr, None)
        if page is not None:
            return page
    return None


def collect_blocked_dialog(page: Any) -> tuple[str, list[str]]:
    """Read dialog title and visible labels only. Never include passwords."""
    title = ""
    labels: list[str] = []
    if page is None:
        return title, labels
    for attr in ("dialog_title", "title"):
        value = getattr(page, attr, None)
        if callable(value):
            try:
                title = str(value() or "").strip()
            except Exception:
                title = ""
        elif value:
            title = str(value).strip()
        if title:
            break
    preset = getattr(page, "visible_labels", None)
    if preset:
        raw_labels = list(preset)
    else:
        raw_labels = []
        locator_fn = getattr(page, "locator", None)
        if callable(locator_fn):
            try:
                nodes = locator_fn("label, legend")
                all_texts = getattr(nodes, "all_inner_texts", None)
                if callable(all_texts):
                    raw_labels.extend(all_texts())
            except Exception:
                raw_labels = []
    seen: set[str] = set()
    for raw in raw_labels:
        label = _safe_visible_label(raw)
        if not label:
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        labels.append(label)
    return _safe_visible_label(title) or "", labels


def unique_locator_from_gemini_label(page: Any, field_label: str) -> Any | None:
    """Build one get_by_label / role locator. Apply only if unique-write passes."""
    label = str(field_label or "").strip()
    if page is None or not label:
        return None
    candidates: list[Any] = []
    get_by_label = getattr(page, "get_by_label", None)
    if callable(get_by_label):
        try:
            candidates.append(get_by_label(label, exact=True))
        except TypeError:
            candidates.append(get_by_label(label))
        except Exception:
            pass
    get_by_role = getattr(page, "get_by_role", None)
    if callable(get_by_role):
        try:
            candidates.append(get_by_role("textbox", name=label, exact=True))
        except TypeError:
            try:
                candidates.append(get_by_role("textbox", name=label))
            except Exception:
                pass
        except Exception:
            pass
    for candidate in candidates:
        if unique_write_block_reason(candidate) is None:
            return candidate
    return None


def consult_gemini_for_blocked_write(
    *,
    reason: str,
    page: Any,
    ask_gemini: Callable[..., Any] | None,
) -> Any | None:
    """Ask Gemini once for one unique label. Return that locator or None (HITL)."""
    if not callable(ask_gemini):
        return None
    title, labels = collect_blocked_dialog(page)
    if not labels:
        return None
    try:
        decision = ask_gemini(
            dialog_title=title,
            visible_labels=labels,
            block_reason=reason,
        )
    except Exception:
        return None
    if decision is None:
        return None
    if isinstance(decision, dict):
        action = decision.get("action")
        field_label = decision.get("field_label")
    else:
        action = getattr(decision, "action", None)
        field_label = getattr(decision, "field_label", None)
    if str(action or "").strip() != "APPLY" or not field_label:
        return None
    return unique_locator_from_gemini_label(page, str(field_label))


def _hitl_blocked(reason: str) -> RuntimeError:
    detail = reason if reason.startswith(PLAYWRIGHT_BLOCKED) else f"{PLAYWRIGHT_BLOCKED}: {reason}"
    return RuntimeError(f"{detail}; HITL {HITL_OPERATOR}")


def _wrap_write(
    method: Callable[..., Any],
    *,
    page_level: bool,
    scope: dict[str, Any],
    locator_originals: dict[str, Callable[..., Any]],
) -> Callable[..., Any]:
    def wrapped(self, *args, **kwargs):
        selector = args[0] if page_level and args else None
        reason = unique_write_block_reason(self, selector=selector)
        if reason:
            if scope.get("_robie_gemini_unique_write_attempted"):
                raise _hitl_blocked(f"{reason}; Gemini already consulted")
            scope["_robie_gemini_unique_write_attempted"] = True
            page = _page_from_target(self, page_level=page_level)
            resolved = consult_gemini_for_blocked_write(
                reason=reason,
                page=page,
                ask_gemini=scope.get("ask_gemini_unique_field"),
            )
            if resolved is None:
                raise _hitl_blocked(
                    f"{reason}; Gemini did not name one unique field"
                )
            require_unique_write_target(resolved)
            original = locator_originals.get(getattr(method, "__name__", ""), method)
            write_args = args[1:] if page_level else args
            result = original(resolved, *write_args, **kwargs)
            _publish_page_hint_from_page(page)
            return result
        result = method(self, *args, **kwargs)
        _publish_page_hint_from_page(_page_from_target(self, page_level=page_level))
        return result

    wrapped.__name__ = getattr(method, "__name__", "write")
    wrapped.__qualname__ = getattr(method, "__qualname__", wrapped.__name__)
    return wrapped


def install_playwright_write_guards(scope: dict[str, Any]) -> dict[str, Any]:
    """Patch Locator/Page write methods in a Playwright exec scope."""
    patched: dict[str, Any] = {}
    locator_originals: dict[str, Callable[..., Any]] = {}
    locator_cls = scope.get("Locator")
    if locator_cls is not None:
        for method_name in WRITE_METHODS:
            original = getattr(locator_cls, method_name, None)
            if callable(original):
                locator_originals[method_name] = original
    for name in ("Locator", "Page"):
        cls = scope.get(name)
        if cls is None:
            continue
        page_level = name == "Page"
        for method_name in WRITE_METHODS:
            original = getattr(cls, method_name, None)
            if not callable(original):
                continue
            setattr(
                cls,
                method_name,
                _wrap_write(
                    original,
                    page_level=page_level,
                    scope=scope,
                    locator_originals=locator_originals,
                ),
            )
            patched[f"{name}.{method_name}"] = True
    scope["_robie_unique_write_guard"] = True
    scope["require_unique_write_target"] = require_unique_write_target
    scope["consult_gemini_for_blocked_write"] = consult_gemini_for_blocked_write
    return patched


def _publish_page_hint_from_page(page: Any) -> None:
    """Tell the zip-loaded recorder which tab a write actually hit.

    Must not run at guard install: ``scope['page']`` is often pages[0], the
    stale Policies tab on job 30777947. A listing hint would re-pin capture.
    """
    url = str(getattr(page, "url", "") or "")
    if not url:
        return
    hint = os.environ.get("ROBIE_RECORDING_HINT_FILE", "").strip()
    if not hint:
        return
    try:
        from robie_job_engine.recording_tab import write_page_hint

        write_page_hint(Path(hint), url=url)
    except Exception:
        return
