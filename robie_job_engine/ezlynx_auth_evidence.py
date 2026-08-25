"""Dependency-free authenticated EZLynx application evidence checks."""

from __future__ import annotations

from typing import Any


AUTHENTICATED_APP_PREFIX = "https://app.ezlynx.com/web/"
LOGIN_CONTROL_SELECTOR = "#txtUserName, #txtPassword, #btnLogin"
INTERNAL_WEB_LINK_SELECTOR = 'a[href^="/web/"], a[href*="app.ezlynx.com/web/"]'


def authenticated_app_evidence(page: Any) -> bool:
    """Require an internal web route, live navigation, and no login controls."""
    if not str(page.url).lower().startswith(AUTHENTICATED_APP_PREFIX):
        return False
    try:
        login_controls = page.locator(LOGIN_CONTROL_SELECTOR).count()
        internal_links = page.locator(INTERNAL_WEB_LINK_SELECTOR).count()
    except Exception:
        return False
    return login_controls == 0 and internal_links > 0
