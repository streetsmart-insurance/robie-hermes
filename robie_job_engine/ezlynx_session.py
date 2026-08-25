from __future__ import annotations

import argparse
import json
import os
from enum import Enum
from typing import Protocol

from .secret_manager import SecretAccessor, load_ezlynx_credentials


LOGIN_URL = "https://app.ezlynx.com/auth/account/login"
APP_URL = "https://app.ezlynx.com/"


class SessionState(str, Enum):
    SIGNED_IN = "SIGNED_IN"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    INTERACTIVE_AUTH_REQUIRED = "INTERACTIVE_AUTH_REQUIRED"
    UNVERIFIED = "UNVERIFIED"


class EzlynxSessionPort(Protocol):
    def state(self) -> SessionState: ...
    def login(self, username: str, password: str) -> SessionState: ...


class InteractiveAuthenticationRequired(RuntimeError):
    pass


class SessionVerificationFailed(RuntimeError):
    pass


def authenticated_app_evidence(
    url: str,
    *,
    internal_web_links: int,
    login_controls: int,
) -> bool:
    """Fail closed unless fresh page state proves the authenticated web shell."""
    normalized = url.casefold().split("?", 1)[0]
    return (
        normalized.startswith("https://app.ezlynx.com/web/")
        and internal_web_links > 0
        and login_controls == 0
    )


class PlaywrightEzlynxSession:
    """Use the persistent Hermes Chrome profile through its local CDP endpoint."""

    def __init__(self, cdp_url: str):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("playwright is required for EZLynx session management") from exc
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)
        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else self._browser.new_context()
        pages = [page for context in self._browser.contexts for page in context.pages]
        ezlynx_pages = [page for page in pages if "ezlynx.com" in page.url.casefold()]
        self._page = (ezlynx_pages or pages or [self._context.new_page()])[0]

    def close(self) -> None:
        # Disconnect from CDP without closing the server-owned Chrome process.
        self._playwright.stop()

    def _ensure_page(self) -> None:
        if "ezlynx.com" not in self._page.url.casefold():
            self._page.goto(APP_URL, wait_until="domcontentloaded")

    def state(self) -> SessionState:
        self._ensure_page()
        url = self._page.url.casefold()
        body = self._page.locator("body").inner_text(timeout=10_000).casefold()
        if "captcha" in body or "verification code" in body or "multi-factor" in body:
            return SessionState.INTERACTIVE_AUTH_REQUIRED
        if "/auth/account/login" in url or "/auth/account/logout" in url:
            return SessionState.LOGIN_REQUIRED
        if authenticated_app_evidence(
            url,
            internal_web_links=self._page.locator('a[href*="/web/"]').count(),
            login_controls=self._page.locator("#txtUserName,#txtPassword,#btnLogin").count(),
        ):
            return SessionState.SIGNED_IN
        return SessionState.UNVERIFIED

    def login(self, username: str, password: str) -> SessionState:
        self._page.goto(LOGIN_URL, wait_until="domcontentloaded")
        try:
            self._page.locator("#txtUserName").fill(username)
            self._page.locator("#txtPassword").fill(password)
            self._page.locator("#btnLogin").click()
            self._page.wait_for_load_state("domcontentloaded", timeout=20_000)
        except Exception as exc:
            raise RuntimeError("EZLynx login interaction failed") from exc
        return self.state()


def ensure_ezlynx_session(
    browser: EzlynxSessionPort,
    *,
    accessor: SecretAccessor | None = None,
) -> SessionState:
    state = browser.state()
    if state is SessionState.SIGNED_IN:
        return state
    if state is SessionState.INTERACTIVE_AUTH_REQUIRED:
        raise InteractiveAuthenticationRequired(
            "EZLynx requires an interactive verification step"
        )
    if state is SessionState.UNVERIFIED:
        raise SessionVerificationFailed(
            "fresh EZLynx page state did not prove an authenticated application shell"
        )
    credentials = load_ezlynx_credentials(accessor)
    state = browser.login(credentials.username, credentials.password)
    if state is SessionState.UNVERIFIED:
        raise SessionVerificationFailed(
            "credential submission completed but fresh destination state was not verified"
        )
    if state is not SessionState.SIGNED_IN:
        raise InteractiveAuthenticationRequired(
            "EZLynx requires interactive authentication after credential submission"
        )
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Ensure the Hermes EZLynx session is authenticated")
    parser.add_argument(
        "--cdp-url",
        default=os.environ.get("ROBIE_BROWSER_CDP_URL", "http://127.0.0.1:9222"),
    )
    args = parser.parse_args()
    browser = PlaywrightEzlynxSession(args.cdp_url)
    try:
        state = ensure_ezlynx_session(browser)
        print(json.dumps({"ezlynx_session": state.value}, sort_keys=True))
    except InteractiveAuthenticationRequired:
        print(json.dumps({"ezlynx_session": SessionState.INTERACTIVE_AUTH_REQUIRED.value}, sort_keys=True))
        raise SystemExit(2)
    except SessionVerificationFailed as exc:
        print(json.dumps({"ezlynx_session": SessionState.UNVERIFIED.value, "error": str(exc)}, sort_keys=True))
        raise SystemExit(3)
    finally:
        browser.close()


if __name__ == "__main__":
    main()
