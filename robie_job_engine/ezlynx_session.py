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


class EzlynxSessionPort(Protocol):
    def state(self) -> SessionState: ...
    def login(self, username: str, password: str) -> SessionState: ...


class InteractiveAuthenticationRequired(RuntimeError):
    pass


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
        return SessionState.SIGNED_IN

    def login(self, username: str, password: str) -> SessionState:
        self._page.goto(LOGIN_URL, wait_until="domcontentloaded")
        try:
            self._page.get_by_label("Username", exact=True).fill(username)
            self._page.get_by_label("Password", exact=True).fill(password)
            self._page.get_by_role("button", name="Log in", exact=True).click()
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
    credentials = load_ezlynx_credentials(accessor)
    state = browser.login(credentials.username, credentials.password)
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
    finally:
        browser.close()


if __name__ == "__main__":
    main()
