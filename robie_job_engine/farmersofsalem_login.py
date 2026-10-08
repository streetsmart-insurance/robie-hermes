"""Sign in to Farmers of Salem on the carrier Chrome (CDP 9223).

The newer pair (2026-10-04) is ``farmers_of_salem_username`` and
``farmers_of_salem_password``. ``farmers_of_salem_robie_*`` is the older
pair and is not read.

One password submission per process. A rejection holds and is not retried.
Values are never logged.
"""

from __future__ import annotations

import re
import socket
from typing import Any, Callable
from urllib.parse import urlsplit

from .intake_core import IntakeHold


LOGIN_URL = "https://farmersofsalem.com/agent_login.aspx"
PORTAL_HOST = "farmersofsalem.com"
FINYS_HOST = "fos.finys.com"
GCP_PROJECT = "streetsmart-hermes-poc"
USER_SECRET = "farmers_of_salem_username"
PASS_SECRET = "farmers_of_salem_password"
OLDER_USER_SECRET = "farmers_of_salem_robie_username"
OLDER_PASS_SECRET = "farmers_of_salem_robie_password"
SETTLE_MS = 4000
_PASSWORD_SUBMITTED = False
_USER_SELECTORS = (
    "input[name*='UserName']",
    "input[name*='username']",
    "input[id*='UserName']",
    "input[type='email']",
    "input[type='text']",
)
_REJECT_TERMS = ("invalid", "incorrect", "does not match", "locked", "disabled", "unsuccessful", "rejected", "try again")


def _require_test_host() -> None:
    raw = f"{socket.gethostname()} {socket.getfqdn()}".lower()
    labels = [label for label in re.split(r"[\s.]+", raw) if label]
    if any(label == "hermes-poc-01" or label.startswith("hermes-poc") for label in labels):
        raise IntakeHold("Farmers of Salem sign-in refuses a Production host")
    if "hermes-test-01" not in labels:
        raise IntakeHold("Farmers of Salem sign-in runs only on hermes-test-01")


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


def _host(url: str) -> str:
    return (urlsplit(url or "").hostname or "").lower()


def is_signed_in(page: Any) -> bool:
    """True on the agent home or Finys, not on the login form."""
    url = str(getattr(page, "url", "") or "")
    host = _host(url)
    path = urlsplit(url).path.lower()
    if "login" in path or "signin" in path:
        return False
    if host == FINYS_HOST:
        return True
    if PORTAL_HOST not in host:
        return False
    try:
        if _count(page.locator("input[type='password']")) > 0:
            return False
    except Exception:
        return False
    text = _body(page).lower()
    return "agent home" in text or "fos portal" in text


def _rejection_message(body: str) -> str:
    lines = []
    for line in str(body or "").splitlines():
        text = " ".join(line.split())
        if text and any(term in text.lower() for term in _REJECT_TERMS):
            lines.append(text[:160])
    said = " ".join(lines[:2])
    base = "Farmers of Salem rejected the username or password. Not retried, so the account is not locked."
    return f"{base} Farmers of Salem said: {said}" if said else base


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
            raise IntakeHold("Farmers of Salem sign-in form is ambiguous")
    return None


def _type(locator: Any, value: str) -> None:
    try:
        locator.fill(value)
    except Exception as exc:
        raise IntakeHold(f"Farmers of Salem sign-in entry failed: {type(exc).__name__}") from None


def _click_submit(page: Any) -> bool:
    for name in ("Login", "Log In", "Sign In", "Submit"):
        try:
            button = page.get_by_role("button", name=re.compile(rf"^{name}$", re.IGNORECASE))
        except Exception:
            continue
        if _count(button) == 1:
            (button.first if hasattr(button, "first") else button).click()
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


def login_farmers(
    page: Any,
    *,
    credentials: Callable[[], tuple[str, str]] | None = None,
    open_portal: Callable[[Any], Any] | None = None,
) -> Any:
    """Sign ``page`` in and return the Finys tab. One password submission."""
    global _PASSWORD_SUBMITTED
    _require_test_host()
    if _host(str(getattr(page, "url", "") or "")) == FINYS_HOST and "login" not in str(getattr(page, "url", "") or "").lower():
        return page
    if _PASSWORD_SUBMITTED and not is_signed_in(page):
        raise IntakeHold(
            "Farmers of Salem sign-in already attempted this run. Not retried, so the account is not locked."
        )
    if not is_signed_in(page):
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        _settle(page)
    if not is_signed_in(page):
        if credentials is None:
            user, password = _get_secret(USER_SECRET), _get_secret(PASS_SECRET)
        else:
            user, password = credentials()
        if not user or not password:
            raise IntakeHold("Farmers of Salem credentials are not available")
        user_box = _first(page, _USER_SELECTORS)
        password_box = _first(page, ("input[type='password']",))
        if user_box is None or password_box is None:
            raise IntakeHold("Farmers of Salem sign-in form did not show a username and password")
        _PASSWORD_SUBMITTED = True
        _type(user_box, user)
        _type(password_box, password)
        if not _click_submit(page):
            raise IntakeHold("Farmers of Salem sign-in form did not show a submit button")
        _settle(page)
        if _rejected(page):
            raise IntakeHold(_rejection_message(_body(page)))
        if not is_signed_in(page):
            raise IntakeHold("Farmers of Salem sign-in did not leave a signed-in session. Not retried.")
    if _host(str(getattr(page, "url", "") or "")) == FINYS_HOST:
        return page
    opener = open_portal
    if opener is None:
        from .farmersofsalem_pending_cancellation import open_finys_from_portal

        opener = open_finys_from_portal
    return opener(page)


def ensure_farmers_tab(cdp_url: str = "http://127.0.0.1:9223") -> str:
    """Connect over CDP, sign in if needed, return the Finys URL."""
    from playwright.sync_api import sync_playwright

    from .farmersofsalem_pending_cancellation import ensure_finys_page

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        page = ensure_finys_page(browser)
        return str(getattr(page, "url", "") or LOGIN_URL)
