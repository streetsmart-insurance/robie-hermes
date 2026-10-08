"""Sign in to GEICO Gateway on the carrier Chrome (CDP 9223).

Never touches EZLynx. Never files. Never emails. Credentials stay in Secret
Manager; this module never prints them.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from .intake_core import IntakeHold


GATEWAY_HOME = "https://gateway2.geico.com/"
CLIENT_ALERTS = "https://gateway2.geico.com/client-alerts"
GATEWAY_HOST = "gateway2.geico.com"
B2C_HOST_FRAGMENT = "b2clogin"
PRODUCER_SECRET = "geico-gateway-producer"
EXTEND_USER_SECRET = "geico-extend-username"
EXTEND_PASS_SECRET = "geico-extend-password"
GCP_PROJECT = "streetsmart-hermes-poc"


def _get_secret(name: str) -> str:
    from .gcp_secret_reader import get_secret

    return get_secret(name, project=GCP_PROJECT).strip()


def geico_credentials() -> tuple[str, str]:
    """Username and password for Gateway. Never logs either half."""
    raw = _get_secret(PRODUCER_SECRET)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            user = str(data.get("username") or data.get("user") or data.get("signInName") or "").strip()
            password = str(data.get("password") or data.get("pass") or "").strip()
            if user and password:
                return user, password
            if user:
                return user, _get_secret(EXTEND_PASS_SECRET)
    except Exception:
        pass
    if ":" in raw and len(raw.split(":", 1)[0]) < 40:
        user, password = raw.split(":", 1)
        return user.strip(), password.strip()
    if re.fullmatch(r"I00\d{4}", raw.strip()):
        return raw.strip(), _get_secret(EXTEND_PASS_SECRET)
    return _get_secret(EXTEND_USER_SECRET), _get_secret(EXTEND_PASS_SECRET)


def _host(url: str) -> str:
    return (urlsplit(url or "").hostname or "").lower()


def _body(page: Any, n: int = 400) -> str:
    try:
        return page.evaluate(
            "() => { const t = document.body && document.body.innerText; return t ? t.slice(0, %d) : ''; }"
            % n
        )
    except Exception:
        return ""


_EXPIRED_RE = re.compile(r"session expired|session has ended", re.IGNORECASE)


def is_session_expired(page: Any) -> bool:
    return bool(_EXPIRED_RE.search(_body(page, 400)))


def is_signed_in(page: Any) -> bool:
    host = _host(getattr(page, "url", "") or "")
    if B2C_HOST_FRAGMENT in host:
        return False
    if host != GATEWAY_HOST:
        return False
    text = _body(page, 400).lower()
    if "sign in" in text or "password" in text or _EXPIRED_RE.search(text):
        return False
    try:
        if page.locator("input[type='password']").count():
            return False
    except Exception:
        return False
    return True


def _close_geico_tabs(context: Any) -> None:
    for page in list(context.pages):
        host = _host(getattr(page, "url", "") or "")
        if host == GATEWAY_HOST or B2C_HOST_FRAGMENT in host or host.endswith("geico.com"):
            try:
                page.close()
            except Exception:
                pass


def _fill_first(page: Any, selectors: tuple[str, ...], value: str) -> None:
    for sel in selectors:
        loc = page.locator(sel)
        if loc.count() and loc.first.is_visible():
            loc.first.fill(value)
            return
    page.locator("input").first.fill(value)


def _click_first(page: Any, selectors: tuple[str, ...], *, timeout: int = 7000) -> bool:
    for sel in selectors:
        loc = page.locator(sel)
        try:
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


def login_geico(
    context: Any,
    *,
    sleep: Callable[[float], None] = time.sleep,
    credentials: Callable[[], tuple[str, str]] | None = None,
) -> Any:
    """Leave exactly one signed-in GEICO Gateway tab on ``context``.

    Closes other GEICO / B2C tabs first so the pull finds exactly one.
    """
    _close_geico_tabs(context)
    page = context.new_page()
    page.goto(GATEWAY_HOME, wait_until="domcontentloaded", timeout=60_000)
    sleep(6)
    if is_signed_in(page):
        page.goto(CLIENT_ALERTS, wait_until="domcontentloaded", timeout=60_000)
        sleep(6)
        return page
    if is_session_expired(page):
        # "Session expired ... Click here to log back in": follow the link to
        # the sign-in form instead of typing into the expired page.
        try:
            page.get_by_text(re.compile(r"click here", re.I)).first.click(timeout=8000)
        except Exception as exc:
            raise IntakeHold("GEICO Gateway session expired and the log-back-in link was not found") from exc
        sleep(8)
        if is_signed_in(page):
            page.goto(CLIENT_ALERTS, wait_until="domcontentloaded", timeout=60_000)
            sleep(6)
            return page
    user, password = (credentials or geico_credentials)()
    if not user or not password:
        raise IntakeHold("GEICO Gateway credentials are not available from secrets")
    _fill_first(
        page,
        ("input[name='signInName']", "input#signInName", "input[type='email']", "input[type='text']"),
        user,
    )
    sleep(0.4)
    _click_first(page, ("button#next", "button[type='submit']"), timeout=5000)
    sleep(2)
    pwd = page.locator("input[type='password']").first
    if not pwd.count():
        raise IntakeHold("GEICO Gateway sign-in form did not show a password field")
    pwd.fill(password)
    sleep(0.4)
    if not _click_first(page, ("button#next", "button[type='submit']", "button:has-text('Sign in')")):
        pwd.press("Enter")
    sleep(12)
    page.goto(CLIENT_ALERTS, wait_until="domcontentloaded", timeout=60_000)
    sleep(6)
    if not is_signed_in(page):
        raise IntakeHold("GEICO Gateway sign-in did not leave a signed-in session")
    return page


def ensure_geico_tab(cdp_url: str = "http://127.0.0.1:9223") -> str:
    """Connect over CDP, sign in if needed, return the Gateway URL."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise IntakeHold("Carrier Chrome has no browser context")
        page = login_geico(browser.contexts[0])
        return str(page.url or GATEWAY_HOME)
