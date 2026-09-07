"""Fail closed when Playwright would write a field it did not uniquely identify.

Hermes generates locator code at runtime. Playwright strict mode still lets
``.first`` / ``.nth()`` / ``.last`` pick an arbitrary match. That is how a
policy form can be filled on the wrong insured, coverage, or date field.
Writes must name exactly one target; positional guesses are refused.

When a write is PLAYWRIGHT_BLOCKED, the guard may ask Gemini for one unique
visible label and apply that locator only after unique-write still passes.
If Gemini is missing, unsure, or the locator is not unique, the write is
refused and the Job HITLs Carlo. Unique-write is never disabled.

A Playwright TimeoutError on fill / click / select_option / type, or a
target that is hidden / aria-hidden / not visible / combobox-hidden, is
the same PLAYWRIGHT_BLOCKED class. Do not invent the value or retry-loop;
ask Gemini then HITL Carlo.
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
# Unique-write still applies to WRITE_METHODS. These also HITL on timeout /
# hidden / combobox-hidden instead of leaking a bare TimeoutError.
CONTROL_ACTION_METHODS = ("fill", "click", "select_option", "type")
WRAP_METHODS = tuple(dict.fromkeys((*WRITE_METHODS, *CONTROL_ACTION_METHODS)))
POSITIONAL_MARKERS = ("nth=", " >> nth", ".first", ".last")
PLAYWRIGHT_BLOCKED = "PLAYWRIGHT_BLOCKED"
HITL_OPERATOR = "Carlo"
HITL_NO_RETRY = "ask Gemini then HITL Carlo; do not retry-loop"
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


def _is_timeout_error(exc: BaseException) -> bool:
    """True for builtin TimeoutError and playwright.sync_api.TimeoutError."""
    if isinstance(exc, TimeoutError):
        return True
    cls = type(exc)
    if cls.__name__ == "TimeoutError":
        return True
    module = getattr(cls, "__module__", "") or ""
    return cls.__name__.endswith("TimeoutError") and "playwright" in module


def _resolve_write_target(target: Any, selector: str | None) -> Any:
    if selector:
        locator_fn = getattr(target, "locator", None)
        if callable(locator_fn):
            return locator_fn(selector)
    return target


def _locator_attribute(target: Any, name: str) -> str | None:
    getter = getattr(target, "get_attribute", None)
    if callable(getter):
        try:
            value = getter(name)
        except Exception as exc:
            if _is_timeout_error(exc):
                raise
            return None
        if value is None:
            return None
        return str(value)
    raw = getattr(target, name, None)
    if raw is None or callable(raw):
        return None
    return str(raw)


def _locator_bool(target: Any, method_name: str) -> bool | None:
    fn = getattr(target, method_name, None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception as exc:
            if _is_timeout_error(exc):
                raise
            return None
    raw = getattr(target, method_name, None)
    if isinstance(raw, bool):
        return raw
    return None


def unwritable_control_block_reason(
    target: Any,
    *,
    selector: str | None = None,
) -> str | None:
    """PLAYWRIGHT_BLOCKED if the unique target is hidden or not a writable control."""
    resolved = _resolve_write_target(target, selector)
    loc = locator_selector_text(resolved)
    try:
        is_hidden = _locator_bool(resolved, "is_hidden")
        is_visible = _locator_bool(resolved, "is_visible")
        aria_hidden = (_locator_attribute(resolved, "aria-hidden") or "").strip().casefold()
        input_type = (_locator_attribute(resolved, "type") or "").strip().casefold()
        role = (_locator_attribute(resolved, "role") or "").strip().casefold()
    except Exception as exc:
        if _is_timeout_error(exc):
            return (
                f"{PLAYWRIGHT_BLOCKED}: visibility probe timed out on locator {loc}; "
                f"{HITL_NO_RETRY}"
            )
        return None

    loc_l = loc.casefold()
    reasons: list[str] = []
    if is_hidden is True:
        reasons.append("hidden")
    if is_visible is False:
        reasons.append("not visible")
    if aria_hidden in {"true", "1"}:
        reasons.append("aria-hidden")
    if input_type == "hidden":
        reasons.append("hidden")

    comboboxish = (
        role == "combobox"
        or "combobox" in loc_l
        or "role=combobox" in loc_l
    )
    hiddenish = (
        is_hidden is True
        or is_visible is False
        or aria_hidden in {"true", "1"}
        or input_type == "hidden"
        or "type=hidden" in loc_l
        or "aria-hidden" in loc_l
    )
    if comboboxish and hiddenish:
        reasons.append("combobox-hidden value")

    if not reasons:
        return None
    seen: list[str] = []
    for item in reasons:
        if item not in seen:
            seen.append(item)
    return (
        f"{PLAYWRIGHT_BLOCKED}: write target {loc} is {' / '.join(seen)}; "
        f"{HITL_NO_RETRY}"
    )


def action_timeout_block_reason(
    target: Any,
    method_name: str,
    *,
    selector: str | None = None,
    exc: BaseException | None = None,
) -> str:
    """PLAYWRIGHT_BLOCKED reason when a control action times out."""
    loc = locator_selector_text(_resolve_write_target(target, selector))
    extra = f" ({type(exc).__name__})" if exc is not None else ""
    return (
        f"{PLAYWRIGHT_BLOCKED}: {method_name} timed out on locator {loc}{extra}; "
        f"{HITL_NO_RETRY}"
    )


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


def _page_url_text(page: Any) -> str:
    """Best-effort page URL. Host is extracted later; query is never sent."""
    if page is None:
        return ""
    value = getattr(page, "url", None)
    if callable(value):
        try:
            value = value()
        except Exception:
            return ""
    return str(value or "").strip()


def attested_test_form_entry_block_reason(
    page: Any,
    *,
    url: str,
    requested_applicant_id: object,
) -> str | None:
    """Fail closed unless an id-less FormEntry visibly belongs to Robie Test.

    EZLynx's numeric ``/Policy/<id>/FormEntry/Index/<id>`` route omits the
    applicant id. The URL shape is therefore insufficient. Permit this one
    Test workflow only after the server-rendered account link and policy
    header independently identify ROBIE Test LLC, a synthetic Homeowners
    policy number, and the exact $1 Test premium.
    """

    refused = "EZLYNX_WRITE_SCOPE_REFUSED"
    try:
        from robie_job_engine.ezlynx_write_scope import (
            applicant_is_write_allowed,
            is_policy_form_entry_url,
            normalize_applicant_id,
        )
    except Exception:
        return f"{refused}: applicant scope guard is unavailable"
    if not is_policy_form_entry_url(url):
        return f"{refused}: page is not a numeric Policy FormEntry route"
    requested = normalize_applicant_id(requested_applicant_id)
    if requested != "220250093" or not applicant_is_write_allowed(requested):
        return f"{refused}: FormEntry applicant is not the compiled Robie Test account"
    locator_fn = getattr(page, "locator", None)
    if not callable(locator_fn):
        return f"{refused}: FormEntry account evidence is unavailable"
    try:
        account = locator_fn('a[title="Go to Applicant Overview"]')
        if int(account.count()) != 1 or not bool(account.is_visible()):
            return f"{refused}: FormEntry account link is missing or ambiguous"
        account_name = " ".join(str(account.inner_text() or "").split())
        href = str(account.get_attribute("href") or "").strip()
        from urllib.parse import urlparse

        account_url = urlparse(href)
        if (
            account_name != "ROBIE Test LLC"
            or (account_url.hostname or "").casefold() != "app.ezlynx.com"
            or account_url.path.casefold() != "/web/account/220250093/overview"
        ):
            return f"{refused}: FormEntry is not visibly scoped to ROBIE Test LLC"
        body = locator_fn("body")
        if int(body.count()) != 1:
            return f"{refused}: FormEntry policy header is unavailable"
        header = " ".join(str(body.inner_text() or "").split())
    except Exception:
        return f"{refused}: FormEntry account or policy attestation failed"
    if "Line of Business: Homeowners" not in header:
        return f"{refused}: FormEntry is not visibly a Homeowners policy"
    if not re.search(r"\bPolicy Number:\s*TEST-HO-[A-Z0-9-]+\b", header):
        return f"{refused}: FormEntry policy number is not synthetic TEST-HO"
    if not re.search(r"\bFull Term Premium:\s*\$1\.00\b", header):
        return f"{refused}: FormEntry premium is not exactly $1.00"
    return None


def _ezlynx_write_scope_block_reason(
    owner: Any,
    *,
    page_level: bool,
    method_name: str,
    selector: Any = None,
) -> str | None:
    """Apply the compiled applicant allowlist before any generic EZLynx control action."""

    page = _page_from_target(owner, page_level=page_level)
    url = _page_url_text(page)
    if "app.ezlynx.com" not in url.casefold():
        return None
    # Filtering the policy listing is a read operation, not a business-record
    # mutation. Keep this exemption exact; subsequent edit/FormEntry controls
    # remain applicant-scoped by the URL parser below.
    if (
        "/applicantportal/policies" in url.casefold()
        and method_name in {"fill", "type"}
        and str(selector or "").strip().casefold() == "#search"
    ):
        return None
    try:
        from robie_job_engine.ezlynx_write_scope import (
            ezlynx_control_scope_block_reason,
        )
    except Exception:
        return (
            "EZLYNX_WRITE_SCOPE_REFUSED: applicant scope guard is unavailable; "
            "refuse generic EZLynx control action"
        )
    requested = os.environ.get("ROBIE_EZLYNX_WRITE_APPLICANT_ID", "")
    reason = ezlynx_control_scope_block_reason(
        url,
        requested_applicant_id=requested,
    )
    if reason:
        try:
            from robie_job_engine.ezlynx_write_scope import is_policy_form_entry_url
        except Exception:
            return reason
        if is_policy_form_entry_url(url):
            return attested_test_form_entry_block_reason(
                page,
                url=url,
                requested_applicant_id=requested,
            )
    return reason


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
            page_url=_page_url_text(page),
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


def _gemini_then_write_or_hitl(
    *,
    reason: str,
    owner: Any,
    page_level: bool,
    scope: dict[str, Any],
    locator_originals: dict[str, Callable[..., Any]],
    method: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """Existing unique-write Gemini-then-HITL path. Do not invent a value."""
    if scope.get("_robie_gemini_unique_write_attempted"):
        raise _hitl_blocked(f"{reason}; Gemini already consulted")
    scope["_robie_gemini_unique_write_attempted"] = True
    page = _page_from_target(owner, page_level=page_level)
    resolved = consult_gemini_for_blocked_write(
        reason=reason,
        page=page,
        ask_gemini=scope.get("ask_gemini_unique_field"),
    )
    if resolved is None:
        raise _hitl_blocked(f"{reason}; Gemini did not name one unique field")
    require_unique_write_target(resolved)
    hidden = unwritable_control_block_reason(resolved)
    if hidden:
        raise _hitl_blocked(f"{hidden}; Gemini already consulted")
    original = locator_originals.get(getattr(method, "__name__", ""), method)
    write_args = args[1:] if page_level else args
    try:
        result = original(resolved, *write_args, **kwargs)
    except Exception as exc:
        if _is_timeout_error(exc):
            raise _hitl_blocked(
                action_timeout_block_reason(
                    resolved,
                    getattr(method, "__name__", "write"),
                    exc=exc,
                )
            ) from exc
        raise
    _publish_page_hint_from_page(page)
    return result


def _wrap_write(
    method: Callable[..., Any],
    *,
    page_level: bool,
    scope: dict[str, Any],
    locator_originals: dict[str, Callable[..., Any]],
) -> Callable[..., Any]:
    method_name = getattr(method, "__name__", "write")

    def wrapped(self, *args, **kwargs):
        selector = args[0] if page_level and args else None
        scope_reason = _ezlynx_write_scope_block_reason(
            self,
            page_level=page_level,
            method_name=method_name,
            selector=selector,
        )
        if scope_reason:
            raise RuntimeError(scope_reason)
        try:
            reason = unique_write_block_reason(self, selector=selector)
            if reason is None and method_name in CONTROL_ACTION_METHODS:
                reason = unwritable_control_block_reason(self, selector=selector)
        except Exception as exc:
            if _is_timeout_error(exc) and method_name in CONTROL_ACTION_METHODS:
                reason = action_timeout_block_reason(
                    self, method_name, selector=selector, exc=exc
                )
            else:
                raise
        if reason:
            return _gemini_then_write_or_hitl(
                reason=reason,
                owner=self,
                page_level=page_level,
                scope=scope,
                locator_originals=locator_originals,
                method=method,
                args=args,
                kwargs=kwargs,
            )
        try:
            result = method(self, *args, **kwargs)
        except Exception as exc:
            if _is_timeout_error(exc) and method_name in CONTROL_ACTION_METHODS:
                return _gemini_then_write_or_hitl(
                    reason=action_timeout_block_reason(
                        self, method_name, selector=selector, exc=exc
                    ),
                    owner=self,
                    page_level=page_level,
                    scope=scope,
                    locator_originals=locator_originals,
                    method=method,
                    args=args,
                    kwargs=kwargs,
                )
            raise
        _publish_page_hint_from_page(_page_from_target(self, page_level=page_level))
        return result

    wrapped.__name__ = method_name
    wrapped.__qualname__ = getattr(method, "__qualname__", method_name)
    return wrapped


def install_playwright_write_guards(scope: dict[str, Any]) -> dict[str, Any]:
    """Patch Locator/Page write methods in a Playwright exec scope."""
    patched: dict[str, Any] = {}
    locator_originals: dict[str, Callable[..., Any]] = {}
    locator_cls = scope.get("Locator")
    if locator_cls is not None:
        for method_name in WRAP_METHODS:
            original = getattr(locator_cls, method_name, None)
            if callable(original):
                locator_originals[method_name] = original
    for name in ("Locator", "Page"):
        cls = scope.get(name)
        if cls is None:
            continue
        page_level = name == "Page"
        for method_name in WRAP_METHODS:
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
    try:
        from robie_job_engine.ezlynx_account_nav import install_account_nav_guard
    except ImportError:
        install_account_nav_guard = None
    if install_account_nav_guard is not None:
        patched.update(install_account_nav_guard(scope))
    return patched


def _publish_page_hint_from_page(page: Any) -> None:
    """Tell the zip-loaded recorder which tab a write actually hit.

    Must not run at guard install: ``scope['page']`` is often pages[0], the
    stale Policies tab on job 30777947. A listing hint would re-pin capture.
    Never write that listing URL when an Edit/FormEntry/documents page exists.
    """
    try:
        from robie_job_engine.recording_tab import publish_live_playwright_hint

        publish_live_playwright_hint(page=page)
    except Exception:
        return
