"""Test-only Gemini rescue for one missing Progressive FAO UI control.

Production skips this module. A pull may ask Gemini once, after a named
control is missing or ambiguous or a Playwright timeout hits that step.
Gemini may name one locator. The step retries only when that locator is
unique and visible. Positional ``.first`` / ``.nth`` answers are refused.
A missing API key holds with ``gemini: not_configured``.

The key is Secret Manager secret id ``gemini-api-key`` (the same resource
``staff_jobs_common`` already uses). This module does not file EZLynx
documents or notes and does not read the document-retrieval kill switch.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator

from .intake_core import IntakeHold
from .playwright_write_guard import locator_is_positional_guess
from .runtime_env import PRODUCTION_ENV_NAMES, TEST_ENV_NAME, current_robie_env
from .secrets import redact_text
from .staff_jobs_common import DEFAULT_GEMINI_KEY_SECRET, GEMINI_MODEL


GEMINI_API_KEY_SECRET_ID = "gemini-api-key"
GEMINI_NOT_CONFIGURED = "gemini: not_configured"
GEMINI_UNSURE = "gemini: unsure"
GEMINI_AMBIGUOUS = "gemini: ambiguous"
GEMINI_REQUEST_FAILED = "gemini: request_failed"
GEMINI_RETRY_FAILED = "gemini: retry_failed"
RESCUE_FLAG = "ROBIE_FAO_GEMINI_UI_RESCUE"
MEMO_OPEN_HOLD = "Memo open control is missing or ambiguous"
_PASSWORD_SELECTOR = "input[type='password']"
_MAX_LABELS = 40
_MAX_LABEL_LEN = 160
_MAX_LOCATOR_LEN = 300
_ROLE_LOCATOR = re.compile(
    r"^(link|button|textbox|searchbox|combobox|option):([^\r\n]+)$"
)
_CONTROL_HOLD = re.compile(
    r"^Progressive control '(?P<label>[^']*)' is missing or ambiguous"
)
_POSITIONAL = (
    ".first",
    ".nth",
    ".last",
    "nth=",
    ">> nth",
    ":nth-child",
    ":nth-of-type",
    ":first-child",
    ":last-child",
    ":first-of-type",
    ":last-of-type",
    "position()",
)
_SECRET_LABEL = re.compile(
    r"\b(password|passwd|pwd|mfa|otp|totp|one[- ]time|secret|token|ssn|fein)\b",
    re.IGNORECASE,
)
_SAFE_CONTEXT_JS = """() => {
  const norm = (value) => String(value || "").replace(/\\s+/g, " ").trim().slice(0, 160);
  const secret = /password|passwd|pwd|otp|mfa|totp|ssn|fein|secret|token|one[- ]time/i;
  const nodes = Array.from(document.querySelectorAll(
    "a, button, input, select, textarea, [role='button'], [role='link'], [role='tab']"
  ));
  const labels = [];
  for (const el of nodes) {
    if (labels.length >= 40) break;
    const type = norm(el.getAttribute("type")).toLowerCase();
    if (type === "password" || type === "hidden") continue;
    const nameAttr = norm(el.getAttribute("name"));
    if (secret.test(nameAttr)) continue;
    let label = norm(el.getAttribute("aria-label"));
    if (!label && el.labels && el.labels.length === 1) {
      label = norm(el.labels[0].innerText || el.labels[0].textContent);
    }
    const tag = el.tagName;
    if (!label && (tag === "A" || tag === "BUTTON" || type === "submit" || type === "button")) {
      label = norm(el.innerText || el.textContent || el.getAttribute("value"));
    }
    if (!label || secret.test(label)) continue;
    labels.push(tag.toLowerCase() + " " + label);
  }
  return { title: norm(document.title), labels: labels };
}"""


class RescueBudget:
    """One Gemini question per Progressive FAO pull."""

    def __init__(self) -> None:
        self.used = False


_BUDGET: ContextVar[RescueBudget | None] = ContextVar(
    "robie_fao_gemini_ui_rescue",
    default=None,
)


@contextmanager
def gemini_ui_rescue_budget(budget: RescueBudget | None = None) -> Iterator[RescueBudget]:
    """Activate the one-shot budget for a pull. Re-entry keeps ``used``."""
    active = budget if budget is not None else RescueBudget()
    token = _BUDGET.set(active)
    try:
        yield active
    finally:
        _BUDGET.reset(token)


def rescue_enabled() -> bool:
    """True only on Test (or an explicit Test flag) and never in Production."""
    env = current_robie_env()
    if env in PRODUCTION_ENV_NAMES:
        return False
    flag = os.environ.get(RESCUE_FLAG, "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    if env == TEST_ENV_NAME:
        return True
    return flag in {"1", "true", "yes", "on"}


def load_gemini_api_key() -> str:
    """Read ``gemini-api-key``. Empty string when the secret is unavailable."""
    resource = (
        os.environ.get("ROBIE_GEMINI_API_KEY_SECRET", "").strip()
        or DEFAULT_GEMINI_KEY_SECRET
    )
    if GEMINI_API_KEY_SECRET_ID not in resource:
        return ""
    try:
        from .staff_jobs_common import read_secret

        value = read_secret(resource)
    except Exception:
        return ""
    return str(value or "").strip()


class ApiKeyGeminiClient:
    """generateContent via the existing API-key secret. Fail closed on errors."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str | None = None,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._api_key = str(api_key or "").strip()
        self.model = (model or GEMINI_MODEL or "gemini-2.5-flash").strip()
        self._opener = opener

    def generate_unique_locator(self, prompt: str) -> str:
        if not self._api_key:
            raise RuntimeError("Gemini API key is empty")
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self._api_key}"
        )
        body = json.dumps(
            {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": 256,
                    "responseMimeType": "application/json",
                },
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        opener = self._opener or urllib.request.urlopen
        try:
            with opener(request, timeout=20) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Gemini generateContent failed: HTTP {exc.code}") from None
        except Exception as exc:
            raise RuntimeError(
                f"Gemini generateContent failed: {type(exc).__name__}"
            ) from None
        try:
            payload = json.loads(raw.decode("utf-8"))
            parts = (
                ((payload.get("candidates") or [{}])[0].get("content") or {}).get("parts")
            ) or []
        except Exception as exc:
            raise RuntimeError(
                f"Gemini generateContent failed: {type(exc).__name__}"
            ) from None
        texts = [str(part.get("text") or "") for part in parts if isinstance(part, dict)]
        text = "\n".join(item for item in texts if item).strip()
        if not text:
            raise RuntimeError("Gemini generateContent returned no text")
        return text


def build_default_client() -> ApiKeyGeminiClient | None:
    key = load_gemini_api_key()
    if not key:
        return None
    return ApiKeyGeminiClient(key)


def safe_page_reference(url: str) -> tuple[str, str]:
    """Host and URL with userinfo, query, and fragment removed."""
    raw = str(url or "").strip()
    if not raw:
        return "", ""
    parsed = urllib.parse.urlsplit(raw)
    host = (parsed.hostname or "").strip()
    if not host or _SECRET_LABEL.search(host):
        return "", ""
    scheme = parsed.scheme if parsed.scheme in {"http", "https"} else "https"
    path = parsed.path or ""
    if len(path) > 300:
        path = path[:300]
    safe = redact_text(urllib.parse.urlunsplit((scheme, host, path, "", "")))
    safe_host = redact_text(host)
    if not safe or safe == "[REDACTED]" or safe_host == "[REDACTED]":
        return "", ""
    return safe_host, safe


def sanitize_visible_label(value: str) -> str | None:
    text = redact_text(str(value or "")).strip()
    if not text or text == "[REDACTED]" or _SECRET_LABEL.search(text):
        return None
    return text[:_MAX_LABEL_LEN]


def sanitize_visible_labels(labels: Any) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    if not isinstance(labels, (list, tuple)):
        return cleaned
    for raw in labels:
        label = sanitize_visible_label(str(raw or ""))
        if not label:
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(label)
        if len(cleaned) >= _MAX_LABELS:
            break
    return cleaned


def capture_safe_page_context(target: Any) -> dict[str, Any]:
    """Host, URL, and visible control labels. No passwords, secrets, or body dump."""
    page = getattr(target, "page", None) or target
    host, url = safe_page_reference(str(getattr(page, "url", "") or ""))
    title = ""
    labels: list[str] = []
    title_fn = getattr(page, "title", None)
    if callable(title_fn):
        try:
            title = sanitize_visible_label(str(title_fn() or "")) or ""
        except Exception:
            title = ""
    elif isinstance(title_fn, str):
        title = sanitize_visible_label(title_fn) or ""
    evaluate = getattr(page, "evaluate", None)
    if callable(evaluate):
        try:
            raw = evaluate(_SAFE_CONTEXT_JS)
        except Exception:
            raw = None
        if isinstance(raw, dict):
            if not title:
                title = sanitize_visible_label(str(raw.get("title") or "")) or ""
            labels = sanitize_visible_labels(raw.get("labels"))
    return {"host": host, "url": url, "title": title, "labels": labels}


def locator_is_refused(selector: str) -> bool:
    text = str(selector or "").strip()
    if not text or len(text) > _MAX_LOCATOR_LEN or any(ch in text for ch in "\n\r\x00"):
        return True
    folded = text.casefold()
    if any(marker in folded for marker in _POSITIONAL):
        return True
    return locator_is_positional_guess(text)


def build_rescue_prompt(*, label: str, context: dict[str, Any], reason: str) -> str:
    labels = sanitize_visible_labels(context.get("labels"))
    host = sanitize_visible_label(str(context.get("host") or "")) or "(unknown host)"
    url = sanitize_visible_label(str(context.get("url") or "")) or "(url withheld)"
    title = sanitize_visible_label(str(context.get("title") or "")) or "(none)"
    wanted = sanitize_visible_label(label) or "the intended control"
    why = redact_text(str(reason or ""))[:500]
    listed = "\n".join(f"- {item}" for item in labels) or "- (none)"
    return (
        "A Progressive For Agents Only UI step is fail-closed. "
        f"Name one unique locator for the control {wanted!r}. "
        "Do not guess. Do not use .first, .nth(), .last, nth=, or :nth-child. "
        "Do not invent a second control. Documents and notes are out of scope.\n\n"
        f"Host: {host}\n"
        f"URL: {url}\n"
        f"Title: {title}\n"
        f"Intended control: {wanted}\n"
        f"Hold: {why}\n"
        "Visible controls (labels only):\n"
        f"{listed}\n\n"
        "Reply with JSON only, no markdown:\n"
        '{"decision":"unique","locator":"<one CSS selector or role:Exact Name>"}\n'
        "or\n"
        '{"decision":"unsure","reason":"<why you cannot name exactly one>"}\n'
        "role form is link:Name, button:Name, or textbox:Name. "
        "If zero or more than one control could match, decision must be unsure."
    )


def _parse_payload(raw: str) -> dict[str, Any] | None:
    text = redact_text(str(raw or "")).strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    return payload


def is_ui_timeout(exc: BaseException) -> bool:
    return type(exc).__name__ == "TimeoutError"


def is_rescuable_control_failure(exc: BaseException) -> bool:
    text = str(exc).split("; gemini:", 1)[0]
    if text == MEMO_OPEN_HOLD:
        return True
    return _CONTROL_HOLD.match(text) is not None


def asked_label(failure: BaseException, fallback: str) -> str:
    text = str(failure).split("; gemini:", 1)[0]
    if text == MEMO_OPEN_HOLD:
        return "Memo"
    match = _CONTROL_HOLD.match(text)
    if match:
        return match.group("label") or fallback
    return fallback


def _failure_text(failure: BaseException, label: str) -> str:
    if is_rescuable_control_failure(failure):
        return str(failure).split("; gemini:", 1)[0]
    return f"Progressive control {label!r} is missing or ambiguous"


def _hold(failure: BaseException, label: str, code: str) -> IntakeHold:
    base = _failure_text(failure, label)
    return IntakeHold(f"{base}; {code}")


def _password_present(target: Any) -> bool:
    page = getattr(target, "page", None) or target
    for candidate in (page, target):
        locate = getattr(candidate, "locator", None)
        if not callable(locate):
            continue
        try:
            found = locate(_PASSWORD_SELECTOR)
            if int(found.count()) > 0:
                return True
        except Exception:
            return True
    return False


def _locator_from_selector(target: Any, selector: str) -> Any | None:
    if locator_is_refused(selector):
        return None
    role = _ROLE_LOCATOR.fullmatch(selector.strip())
    if role:
        get_by_role = getattr(target, "get_by_role", None)
        if not callable(get_by_role):
            return None
        try:
            return get_by_role(role.group(1), name=role.group(2).strip(), exact=True)
        except Exception:
            return None
    locate = getattr(target, "locator", None)
    if not callable(locate):
        return None
    try:
        return locate(selector.strip())
    except Exception:
        return None


def _secret_control(locator: Any) -> bool:
    get_attribute = getattr(locator, "get_attribute", None)
    if not callable(get_attribute):
        return False
    try:
        control_type = str(get_attribute("type") or "")
    except Exception:
        return True
    return control_type.casefold() in {"password", "hidden"}


def resolve_unique_visible(target: Any, selector: str) -> Any | None:
    """Return the locator only when it matches one visible, non-secret control."""
    if locator_is_refused(selector):
        return None
    located = _locator_from_selector(target, selector)
    if located is None:
        return None
    try:
        count = int(located.count())
    except Exception:
        return None
    if count != 1:
        return None
    if _secret_control(located):
        return None
    visible = getattr(located, "is_visible", None)
    if not callable(visible):
        return None
    try:
        if not bool(visible()):
            return None
    except Exception:
        return None
    return located


def _active_client() -> Any | None:
    try:
        return build_default_client()
    except Exception:
        return None


def rescued_locator(target: Any, label: str, failure: BaseException) -> Any:
    """One Gemini locator, or an IntakeHold. Production re-raises ``failure``."""
    if not rescue_enabled():
        raise failure
    budget = _BUDGET.get()
    if budget is None or budget.used:
        raise failure
    budget.used = True
    if _password_present(target):
        raise failure
    client = _active_client()
    if client is None:
        raise _hold(failure, label, GEMINI_NOT_CONFIGURED) from failure
    context = capture_safe_page_context(target)
    prompt = build_rescue_prompt(
        label=asked_label(failure, label),
        context=context,
        reason=_failure_text(failure, label),
    )
    try:
        raw = client.generate_unique_locator(prompt)
    except Exception:
        raise _hold(failure, label, GEMINI_REQUEST_FAILED) from None
    payload = _parse_payload(str(raw or ""))
    if not payload:
        raise _hold(failure, label, GEMINI_UNSURE)
    decision = str(payload.get("decision") or "").strip().casefold()
    if decision in {"unsure", "hitl", "unknown", "ambiguous", ""}:
        raise _hold(failure, label, GEMINI_UNSURE)
    if decision != "unique":
        raise _hold(failure, label, GEMINI_AMBIGUOUS)
    extras = payload.get("locators") or payload.get("alternates") or payload.get("selectors")
    if extras:
        raise _hold(failure, label, GEMINI_AMBIGUOUS)
    selector = str(payload.get("locator") or "").strip()
    if locator_is_refused(selector):
        raise _hold(failure, label, GEMINI_AMBIGUOUS)
    located = resolve_unique_visible(target, selector)
    if located is None:
        raise _hold(failure, label, GEMINI_AMBIGUOUS)
    return located


def run_named_control_step(
    target: Any,
    label: str,
    primary: Callable[[], Any],
    retry: Callable[[Any], Any],
) -> Any:
    """Run ``primary``. On one rescuable failure, retry once with Gemini's locator.

    A control that ``primary`` already resolved does not call Gemini.
    """
    try:
        return primary()
    except IntakeHold as exc:
        if not is_rescuable_control_failure(exc):
            raise
        failure: BaseException = exc
    except Exception as exc:
        if not is_ui_timeout(exc):
            raise
        failure = exc
    locator = rescued_locator(target, label, failure)
    try:
        return retry(locator)
    except IntakeHold:
        raise
    except Exception as exc:
        raise IntakeHold(
            f"{_failure_text(failure, label)}; {GEMINI_RETRY_FAILED}"
        ) from exc
