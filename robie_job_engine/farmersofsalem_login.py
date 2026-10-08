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


LOGIN_URL = "https://www.farmersofsalem.com/agent_login.aspx"
AGENT_HOME_URL = "https://www.farmersofsalem.com/agent/agent_home.aspx"
PORTAL_HOST = "farmersofsalem.com"
FINYS_HOST = "fos.finys.com"
# Live agent_login.aspx (read-only, 2026-10-08). The name attributes use $
# where the ids use _. The agent-search widget (SearchAgentByNameUserControl1)
# is also a visible text input and is not the login.
USER_ID = "ctl00_ContentPlaceHolder1_txtName"
PASS_ID = "ctl00_ContentPlaceHolder1_txtPass"
SUBMIT_ID = "ctl00_ContentPlaceHolder1_btnLogin"
GCP_PROJECT = "streetsmart-hermes-poc"
USER_SECRET = "farmers_of_salem_username"
PASS_SECRET = "farmers_of_salem_password"
OLDER_USER_SECRET = "farmers_of_salem_robie_username"
OLDER_PASS_SECRET = "farmers_of_salem_robie_password"
SETTLE_MS = 4000
_PASSWORD_SUBMITTED = False
_USER_SELECTORS = (
    f"#{USER_ID}",
    "input[name='ctl00$ContentPlaceHolder1$txtName']",
)
_PASS_SELECTORS = (
    f"#{PASS_ID}",
    "input[name='ctl00$ContentPlaceHolder1$txtPass']",
)
_SUBMIT_SELECTORS = (
    f"#{SUBMIT_ID}",
    "input[name='ctl00$ContentPlaceHolder1$btnLogin']",
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


def _title(page: Any) -> str:
    title = getattr(page, "title", None)
    if not callable(title):
        return ""
    try:
        return str(title() or "")
    except Exception:
        return ""


def _is_visible(node: Any) -> bool:
    visible = getattr(node, "is_visible", None)
    if not callable(visible):
        return True
    try:
        return bool(visible())
    except TypeError:
        try:
            return bool(visible(timeout=500))
        except Exception:
            return True
    except Exception:
        return False


def _visible_matches(page: Any, selector: str) -> list[Any]:
    locator = page.locator(selector)
    count = _count(locator)
    if count <= 0:
        return []
    matches = []
    for index in range(count):
        node = locator.nth(index) if hasattr(locator, "nth") else locator
        if _is_visible(node):
            matches.append(node)
        if len(matches) > 1:
            break
    return matches


def _on_agent_path(url: str) -> bool:
    """True for /agent/... on the Farmers of Salem host. Not /agent_login.aspx."""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if PORTAL_HOST not in host:
        return False
    return (parts.path or "").lower().startswith("/agent/")


def _login_form_visible(page: Any) -> bool:
    """The ContentPlaceHolder1 password box is on screen. Search fields do not count."""
    return any(_visible_matches(page, selector) for selector in _PASS_SELECTORS)


def _signed_in_markers(page: Any) -> bool:
    text = f"{_body(page)} {_title(page)}".lower()
    if any(marker in text for marker in ("agent home", "fos portal", "logout", "log out", "sign out")):
        return True
    for name in ("FOS PORTAL", "Logout", "Log Out", "Sign Out"):
        try:
            locator = page.get_by_role("link", name=name)
        except Exception:
            continue
        if _visible_matches_locator(locator):
            return True
    return False


def _visible_matches_locator(locator: Any) -> bool:
    count = _count(locator)
    if count <= 0:
        return False
    for index in range(min(count, 4)):
        node = locator.nth(index) if hasattr(locator, "nth") else locator
        if _is_visible(node):
            return True
    return False


def is_signed_in(page: Any) -> bool:
    """True on Finys, or any /agent/ page that is not showing the login form.

    Live home is https://www.farmersofsalem.com/agent/agent_home.aspx
    ('Farmers Of Salem :: Agent Home'). That page is not sent to
    agent_login.aspx. /agent_login.aspx is not an /agent/ path.
    """
    url = str(getattr(page, "url", "") or "")
    host = _host(url)
    path = urlsplit(url).path.lower()
    if host == FINYS_HOST and "login" not in path:
        return True
    if PORTAL_HOST not in host:
        return False
    if _on_agent_path(url) and not _login_form_visible(page):
        return True
    if "agent home" in _title(page).lower() and not _login_form_visible(page):
        return True
    if _signed_in_markers(page) and not _login_form_visible(page):
        return True
    return False


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
    """The one visible match. Hidden WebForms duplicates are ignored."""
    for selector in selectors:
        matches = _visible_matches(page, selector)
        if len(matches) > 1:
            raise IntakeHold("Farmers of Salem sign-in form is ambiguous")
        if len(matches) == 1:
            return matches[0]
    return None


def _type(locator: Any, value: str) -> None:
    try:
        locator.fill(value)
    except Exception as exc:
        raise IntakeHold(f"Farmers of Salem sign-in entry failed: {type(exc).__name__}") from None


def _click_submit(page: Any) -> bool:
    """Click ContentPlaceHolder1's btnLogin. The agent-search submit is not it.

    The live control is an input type=submit. The page has no button elements.
    """
    for selector in _SUBMIT_SELECTORS:
        matches = _visible_matches(page, selector)
        if len(matches) > 1:
            raise IntakeHold("Farmers of Salem sign-in form is ambiguous")
        if len(matches) == 1:
            matches[0].click()
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
        password_box = _first(page, _PASS_SELECTORS)
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
