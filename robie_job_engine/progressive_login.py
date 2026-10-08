"""Sign in to Progressive ForAgentsOnly on the carrier Chrome (CDP 9223).

The FAO pending-cancellation worker and the BOP worker both use
ForAgentsOnly (``www.foragentsonly.com``, login host
``foragentsonlylogin.progressive.com``). This module types that login.

Secrets (Secret Manager project streetsmart-hermes-poc), never logged:

* ``progressive-robie-login``
* ``progressive-robie-password``
* ``progressive-robie-security-questions`` (read only when the page asks)

``progressive_username`` / ``progressive_password`` are not used. Nothing in
this repo or its docs ties them to ForAgentsOnly. They share a create day
(2026-10-06) with the robie pair, which is not the same identity. Submitting
them would be a second login against a portal that locks accounts.

``foragentsonly_*`` is the name of this same portal. It is not used either.
The robie pair is the one that carries the security-question answers, and a
second username is not submitted.

One password submission per process. A rejection holds and is not retried.
"""

from __future__ import annotations

import json
import re
import socket
from typing import Any, Callable

from .intake_core import IntakeHold


LOGIN_URL = "https://www.foragentsonlylogin.progressive.com/Login/"
HOME_URL = "https://www.foragentsonly.com/"
LOGIN_HOST = "foragentsonlylogin.progressive.com"
GCP_PROJECT = "streetsmart-hermes-poc"
USER_SECRET = "progressive-robie-login"
PASS_SECRET = "progressive-robie-password"
QUESTIONS_SECRET = "progressive-robie-security-questions"
# Not read. Documented so a later change does not "fix" the login onto them.
UNUSED_PROGRESSIVE_USERNAME = "progressive_username"
UNUSED_PROGRESSIVE_PASSWORD = "progressive_password"
SETTLE_MS = 4000

_PASSWORD_SUBMITTED = False
_USER_SELECTORS = (
    "input[name='userId']",
    "input#userId",
    "input[name='username']",
    "input[name='UserId']",
    "input[type='email']",
    "input[type='text']",
)
_REJECT_TERMS = ("invalid", "incorrect", "does not match", "locked", "disabled", "unsuccessful", "rejected", "try again")
_QUESTION_TERMS = ("security question", "challenge question", "please answer", "secret question")


def _require_test_host() -> None:
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if any(label == "hermes-poc-01" or label.startswith("hermes-poc") for label in labels):
        raise IntakeHold("Progressive sign-in refuses a Production host")
    if "hermes-test-01" not in labels:
        raise IntakeHold("Progressive sign-in runs only on hermes-test-01")


def _get_secret(name: str) -> str:
    from .gcp_secret_reader import get_secret

    return get_secret(name, project=GCP_PROJECT).strip()


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url or "").hostname or "").lower()


def _body(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text() or "")
    except Exception:
        return ""


def _count(locator: Any) -> int:
    try:
        return int(locator.count())
    except Exception:
        return -1


def is_signed_in(page: Any) -> bool:
    """True on a ForAgentsOnly app page that is not the login form."""
    url = str(getattr(page, "url", "") or "")
    host = _host(url)
    if host == LOGIN_HOST or host.endswith("." + LOGIN_HOST):
        return False
    on_fao = host == "foragentsonly.com" or host.endswith(".foragentsonly.com")
    if not on_fao:
        return False
    path = url.lower()
    if "/login" in path or path.rstrip("/").endswith("/logon"):
        return False
    try:
        if _count(page.locator("input[type='password']")) > 0:
            return False
    except Exception:
        return False
    return True


def parse_security_questions(raw: str) -> tuple[tuple[str, str], ...]:
    """Question/answer pairs. Values are never included in errors."""
    try:
        data = json.loads(raw or "")
    except Exception as exc:
        raise IntakeHold("Progressive security questions secret is missing or ambiguous") from exc
    if isinstance(data, dict) and isinstance(data.get("questions"), list):
        data = data["questions"]
    pairs: list[tuple[str, str]] = []
    if isinstance(data, dict):
        items = [(str(key), str(value)) for key, value in data.items()]
    elif isinstance(data, list):
        items = []
        for item in data:
            if isinstance(item, dict):
                items.append((
                    str(item.get("question") or item.get("q") or ""),
                    str(item.get("answer") or item.get("a") or ""),
                ))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                items.append((str(item[0]), str(item[1])))
    else:
        items = []
    for question, answer in items:
        question, answer = question.strip(), answer.strip()
        if len(question) >= 4 and answer:
            pairs.append((question, answer))
    if not pairs:
        raise IntakeHold("Progressive security questions secret is missing or ambiguous")
    return tuple(pairs)


def matching_answer(pairs: tuple[tuple[str, str], ...], page_text: str) -> str:
    """The one answer whose question text appears on the page."""
    body = re.sub(r"\s+", " ", page_text or "").casefold()
    hits = [answer for question, answer in pairs if question.casefold() in body]
    if len(hits) != 1:
        raise IntakeHold(
            "Progressive asked a security question this login cannot answer. Not retried."
        )
    return hits[0]


def _rejection_message(body: str) -> str:
    lines = []
    for line in str(body or "").splitlines():
        text = " ".join(line.split())
        if text and any(term in text.lower() for term in _REJECT_TERMS):
            lines.append(text[:160])
    said = " ".join(lines[:2])
    base = "Progressive rejected the user id or password. Not retried, so the account is not locked."
    return f"{base} Progressive said: {said}" if said else base


def _rejected(page: Any) -> bool:
    if is_signed_in(page):
        return False
    return any(term in _body(page).lower() for term in _REJECT_TERMS)


def _looks_like_question(body: str) -> bool:
    text = body.lower()
    return any(term in text for term in _QUESTION_TERMS)


def _first(page: Any, selectors: tuple[str, ...]) -> Any | None:
    for selector in selectors:
        locator = page.locator(selector)
        count = _count(locator)
        if count == 1:
            return locator.first if hasattr(locator, "first") else locator
        if count > 1:
            raise IntakeHold("Progressive sign-in form is ambiguous")
    return None


def _type(locator: Any, value: str) -> None:
    try:
        locator.fill(value)
    except Exception as exc:
        raise IntakeHold(f"Progressive sign-in entry failed: {type(exc).__name__}") from None


def _click_submit(page: Any) -> bool:
    for name in ("Log In", "Sign In", "Login", "Continue", "Submit"):
        try:
            button = page.get_by_role("button", name=re.compile(rf"^{name}$", re.IGNORECASE))
        except Exception:
            continue
        if _count(button) == 1:
            target = button.first if hasattr(button, "first") else button
            target.click()
            return True
    locator = page.locator("button[type='submit'], input[type='submit']")
    if _count(locator) == 1:
        target = locator.first if hasattr(locator, "first") else locator
        target.click()
        return True
    return False


def _settle(page: Any) -> None:
    waiter = getattr(page, "wait_for_timeout", None)
    if callable(waiter):
        waiter(SETTLE_MS)


def login_progressive(
    page: Any,
    *,
    credentials: Callable[[], tuple[str, str]] | None = None,
    questions: tuple[tuple[str, str], ...] | None = None,
) -> None:
    """Sign ``page`` in. One password submission. Raises IntakeHold on failure."""
    global _PASSWORD_SUBMITTED
    _require_test_host()
    if is_signed_in(page):
        return
    if _PASSWORD_SUBMITTED:
        raise IntakeHold(
            "Progressive sign-in already attempted this run. Not retried, so the account is not locked."
        )
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
    _settle(page)
    if is_signed_in(page):
        return
    if credentials is None:
        user, password = _get_secret(USER_SECRET), _get_secret(PASS_SECRET)
    else:
        user, password = credentials()
    if not user or not password:
        raise IntakeHold("Progressive credentials are not available")
    user_box = _first(page, _USER_SELECTORS)
    if user_box is None:
        raise IntakeHold("Progressive sign-in form did not show a user id field")
    _type(user_box, user)
    password_box = _first(page, ("input[type='password']",))
    if password_box is None:
        if not _click_submit(page):
            raise IntakeHold("Progressive sign-in form did not show a password field")
        _settle(page)
        password_box = _first(page, ("input[type='password']",))
    if password_box is None:
        raise IntakeHold("Progressive sign-in form did not show a password field")
    _PASSWORD_SUBMITTED = True
    _type(password_box, password)
    if not _click_submit(page):
        raise IntakeHold("Progressive sign-in form did not show a submit button")
    _settle(page)
    if _rejected(page):
        raise IntakeHold(_rejection_message(_body(page)))
    if is_signed_in(page):
        return
    prompt = _body(page)
    if _looks_like_question(prompt):
        pairs = questions if questions is not None else parse_security_questions(_get_secret(QUESTIONS_SECRET))
        answer = matching_answer(pairs, prompt)
        box = _first(page, ("input[type='text']", "input[type='password']"))
        if box is None:
            raise IntakeHold("Progressive security question did not show an answer field. Not retried.")
        _type(box, answer)
        if not _click_submit(page):
            raise IntakeHold("Progressive security question did not show a submit button. Not retried.")
        _settle(page)
        if _rejected(page):
            raise IntakeHold(_rejection_message(_body(page)))
    if not is_signed_in(page):
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
            _settle(page)
        except IntakeHold:
            raise
        except Exception as exc:
            raise IntakeHold(f"Progressive sign-in did not leave a signed-in session ({type(exc).__name__})") from None
    if not is_signed_in(page):
        raise IntakeHold("Progressive sign-in did not leave a signed-in session. Not retried.")


def ensure_progressive_tab(cdp_url: str = "http://127.0.0.1:9223") -> str:
    """Connect over CDP, sign in if needed, return the ForAgentsOnly URL."""
    from playwright.sync_api import sync_playwright

    from .progressive_pending_cancellation import ensure_fao_page

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        page = ensure_fao_page(browser)
        return str(getattr(page, "url", "") or HOME_URL)
