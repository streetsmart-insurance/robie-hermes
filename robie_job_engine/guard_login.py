"""Guard (Berkshire Hathaway GUARD Agency Service Center) automated login.

BUILT 2026-10-08. Test host only (hermes-test-01); Production and unknown
hosts hold before any credential is read.

Credentials come from Secret Manager (project streetsmart-hermes-poc):
``berkshire_guard_username`` / ``berkshire_guard_password``. Live 2026-10-08
the user code is case-sensitive: the ``guard_username`` secret holds
"cferrara2" and is rejected, while the berkshire pair ("Cferrara2") signs in.

Flow (proven live 2026-10-08 on the Test carrier Chrome, CDP 9223):
1. Open https://gigezrate.guard.com/portal (redirects to /auth when signed out).
2. Dismiss the cookie banner if shown ("REJECT ALL").
3. Type the user code into ``input[name='Username']`` and the password into
   ``input[name='Password']``; click LOGIN.
4. Confirm the tab is back on /portal with "Logout" visible.

Nothing here logs, prints, or stores the credentials. A wrong-credential
response holds immediately (never retried), so the account is not locked.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from .intake_core import IntakeHold

GUARD_HOST = "gigezrate.guard.com"
GUARD_PORTAL_URL = f"https://{GUARD_HOST}/portal"
GCP_PROJECT = "streetsmart-hermes-poc"
GUARD_USER_SECRET = "berkshire_guard_username"
GUARD_PASS_SECRET = "berkshire_guard_password"
LOGIN_SETTLE_MS = 14000

INVALID_CREDENTIALS = (
    "Guard rejected the user code/password (berkshire_guard_* secrets). "
    "Not retried, to avoid locking the account."
)


def _get_secret(name: str) -> str:
    from .gcp_secret_reader import get_secret

    return get_secret(name, project=GCP_PROJECT).strip()


def _host_and_path(page: Any) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(str(getattr(page, "url", "") or ""))
    return (parts.hostname or "").lower(), (parts.path or "").lower()


def _body(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text() or "")
    except Exception:
        return ""


def is_logged_in(page: Any) -> bool:
    """True when the tab is a signed-in Agency Service Center page."""
    host, path = _host_and_path(page)
    if host != GUARD_HOST or path.startswith("/auth"):
        return False
    return "logout" in _body(page).lower()


def _type(locator: Any, value: str) -> None:
    typer = getattr(locator, "press_sequentially", None)
    locator.click()
    if callable(typer):
        typer(value, delay=30)
    else:
        locator.type(value)


def login_guard(page: Any) -> None:
    """Sign ``page`` in to the Agency Service Center. Raises IntakeHold on failure."""
    username = _get_secret(GUARD_USER_SECRET)
    password = _get_secret(GUARD_PASS_SECRET)
    if not username or not password:
        raise IntakeHold("Guard credentials not available from secrets")
    page.goto(GUARD_PORTAL_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    if is_logged_in(page):
        return
    try:
        banner = page.get_by_role("button", name="REJECT ALL")
        if banner.count() > 0:
            banner.first.click()
            page.wait_for_timeout(1000)
    except Exception:
        pass
    user_box = page.locator("input[name='Username']")
    pass_box = page.locator("input[name='Password']")
    try:
        if user_box.count() != 1 or pass_box.count() != 1:
            raise IntakeHold("Guard login form is missing or ambiguous")
        _type(user_box.first, username)
        _type(pass_box.first, password)
        page.get_by_role("button", name="LOGIN").first.click()
    except IntakeHold:
        raise
    except Exception as exc:
        # Never echo the exception text: it can carry the typed value.
        raise IntakeHold(f"Guard login entry failed: {type(exc).__name__}")
    page.wait_for_timeout(LOGIN_SETTLE_MS)
    if "invalid" in _body(page).lower() and _host_and_path(page)[1].startswith("/auth"):
        raise IntakeHold(INVALID_CREDENTIALS)
    if not is_logged_in(page):
        raise IntakeHold("Guard login failed - not on the Agency Service Center after sign-in")
