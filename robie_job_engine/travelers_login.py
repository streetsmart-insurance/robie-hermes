"""Sign in to the Travelers for-agents portal on the carrier Chrome (CDP 9223).

Secrets (project streetsmart-hermes-poc): ``travelers_username`` and
``travelers_password``. One password submission per process. A rejection
holds and is not retried, so a wrong password cannot lock the account.
Values are never logged.
"""

from __future__ import annotations

import re
import socket
from typing import Any, Callable
from urllib.parse import urlsplit

from .intake_core import IntakeHold


PORTAL_URL = "https://foragents.travelers.com/Business"
TRAVELERS_HOST = "foragents.travelers.com"
GCP_PROJECT = "streetsmart-hermes-poc"
USER_SECRET = "travelers_username"
PASS_SECRET = "travelers_password"
SETTLE_MS = 4000
_PASSWORD_SUBMITTED = False
_USER_SELECTORS = (
    "input[name='username']",
    "input#username",
    "input[name='USER']",
    "input[type='email']",
    "input[type='text']",
)
_REJECT_TERMS = ("invalid", "incorrect", "does not match", "locked", "disabled", "unsuccessful", "rejected", "try again")


def _require_test_host() -> None:
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if any(label == "hermes-poc-01" or label.startswith("hermes-poc") for label in labels):
        raise IntakeHold("Travelers sign-in refuses a Production host")
    if "hermes-test-01" not in labels:
        raise IntakeHold("Travelers sign-in runs only on hermes-test-01")


def _get_secret(name: str) -> str:
    from .gcp_secret_reader import get_secret

    return get_secret(name, project=GCP_PROJECT).strip()


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
    parts = urlsplit(str(getattr(page, "url", "") or ""))
    host = (parts.hostname or "").lower()
    if host != TRAVELERS_HOST:
        return False
    if "login" in parts.path.lower() or "auth" in parts.path.lower():
        return False
    try:
        if _count(page.locator("input[type='password']")) > 0:
            return False
    except Exception:
        return False
    return True


def _rejection_message(body: str) -> str:
    lines = []
    for line in str(body or "").splitlines():
        text = " ".join(line.split())
        if text and any(term in text.lower() for term in _REJECT_TERMS):
            lines.append(text[:160])
    said = " ".join(lines[:2])
    base = "Travelers rejected the username or password. Not retried, so the account is not locked."
    return f"{base} Travelers said: {said}" if said else base


def _rejected(page: Any) -> bool:
    if is_signed_in(page):
        return False
    return any(term in _body(page).lower() for term in _REJECT_TERMS)


def _first(page: Any, selectors: tuple[str, ...]) -> Any | None:
    for selector in selectors:
        locator = page.locator(selector)
        count = _count(locator)
        if count == 1:
            return locator.first if hasattr(locator, "first") else locator
        if count > 1:
            raise IntakeHold("Travelers sign-in form is ambiguous")
    return None


def _type(locator: Any, value: str) -> None:
    try:
        locator.fill(value)
    except Exception as exc:
        raise IntakeHold(f"Travelers sign-in entry failed: {type(exc).__name__}") from None


def _click_submit(page: Any) -> bool:
    for name in ("Sign In", "Log In", "Login", "Submit"):
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
        (locator.first if hasattr(locator, "first") else locator).click()
        return True
    return False


def _settle(page: Any) -> None:
    waiter = getattr(page, "wait_for_timeout", None)
    if callable(waiter):
        waiter(SETTLE_MS)


def login_travelers(
    page: Any,
    *,
    credentials: Callable[[], tuple[str, str]] | None = None,
) -> None:
    """Sign ``page`` in. One password submission. Raises IntakeHold on failure."""
    global _PASSWORD_SUBMITTED
    _require_test_host()
    if is_signed_in(page):
        return
    if _PASSWORD_SUBMITTED:
        raise IntakeHold(
            "Travelers sign-in already attempted this run. Not retried, so the account is not locked."
        )
    page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60_000)
    _settle(page)
    if is_signed_in(page):
        return
    if credentials is None:
        user, password = _get_secret(USER_SECRET), _get_secret(PASS_SECRET)
    else:
        user, password = credentials()
    if not user or not password:
        raise IntakeHold("Travelers credentials are not available")
    user_box = _first(page, _USER_SELECTORS)
    password_box = _first(page, ("input[type='password']",))
    if user_box is None or password_box is None:
        raise IntakeHold("Travelers sign-in form did not show a username and password")
    _PASSWORD_SUBMITTED = True
    _type(user_box, user)
    _type(password_box, password)
    if not _click_submit(page):
        raise IntakeHold("Travelers sign-in form did not show a submit button")
    _settle(page)
    if _rejected(page):
        raise IntakeHold(_rejection_message(_body(page)))
    if not is_signed_in(page):
        raise IntakeHold("Travelers sign-in did not leave a signed-in session. Not retried.")


def ensure_travelers_tab(cdp_url: str = "http://127.0.0.1:9223") -> str:
    """Connect over CDP, own one Travelers tab, return its URL."""
    from playwright.sync_api import sync_playwright

    from .travelers_pending_cancellation import ensure_travelers_page

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        page = ensure_travelers_page(browser)
        return str(getattr(page, "url", "") or PORTAL_URL)
