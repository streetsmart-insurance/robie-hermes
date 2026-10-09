"""Sign in to NatGen Agency on the carrier Chrome (CDP 9223).

Never touches EZLynx. Never files. Never emails clients. Credentials stay in
Secret Manager; the email OTP is read from the MFA mailbox the same way Utica
does. This module never prints secrets or codes.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from .intake_core import IntakeHold
from .utica_login import (
    DELEGATED_SA_ENV,
    GMAIL_READONLY_SCOPE,
    MFA_UNCONFIGURED,
    extract_code,
    message_text,
    mfa_mailbox,
)


NATGEN_HOME = "https://natgenagency.com/MainMenu.aspx"
NATGEN_LANDING = "https://natgenagency.com/"
NATGEN_REPORTS = "https://natgenagency.com/Reports/AgencyActivityReports.aspx?r=5"
NATGEN_HOST = "natgenagency.com"
USER_SECRET = "natgen_robie_username"
PASS_SECRET = "natgen_robie_password"
GCP_PROJECT = "streetsmart-hermes-poc"
OTP_FROM = "natgen.verification@ngic.com"
OTP_SUBJECT = "One Time Verification Code"
_CODE_CLOCK_SKEW_SECONDS = 60


def _get_secret(name: str) -> str:
    from .gcp_secret_reader import get_secret

    return get_secret(name, project=GCP_PROJECT).strip()


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


def _session_taken_by_another_window(page: Any) -> bool:
    """Home page with Enable Login and a disabled User ID box.

    Another window owns the session. Do not click Enable Login.
    """
    text = _body(page, 800).lower()
    taken = "another window" in text or "enable login" in text
    disabled = False
    try:
        box = page.locator("#txtUserID")
        if int(box.count()) == 1:
            checker = getattr(box, "is_disabled", None)
            if callable(checker):
                try:
                    disabled = bool(checker())
                except TypeError:
                    disabled = bool(checker(timeout=1000))
            if not disabled:
                attr = box.get_attribute("disabled")
                disabled = attr is not None
    except Exception:
        disabled = False
    if taken and disabled:
        return True
    if taken and "enable login" in text:
        return True
    return False


def is_signed_in(page: Any) -> bool:
    host = _host(getattr(page, "url", "") or "")
    if host != NATGEN_HOST:
        return False
    path = (urlsplit(getattr(page, "url", "") or "").path or "").lower()
    if path.endswith("/login") or "login" in path:
        return False
    text = _body(page, 200).lower()
    if "password" in text and "hello," not in text and "mainmenu" not in path:
        return False
    return "mainmenu" in path or "hello," in text or "agencyactivity" in path


def _close_natgen_tabs(context: Any) -> None:
    for page in list(context.pages):
        if _host(getattr(page, "url", "") or "") == NATGEN_HOST:
            try:
                page.close()
            except Exception:
                pass


def _gmail_service() -> Any:
    service_account = os.environ.get(DELEGATED_SA_ENV, "").strip()
    if not service_account:
        raise IntakeHold(MFA_UNCONFIGURED)
    from .gmail_accountability import build_keyless_delegated_service

    return build_keyless_delegated_service(
        service_account,
        mfa_mailbox(),
        scopes=[GMAIL_READONLY_SCOPE],
    )


def get_natgen_code(
    max_wait: int = 120,
    *,
    not_before: float | None = None,
    service: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> str:
    """Poll Gmail for the NatGen one-time verification code."""
    service = service if service is not None else _gmail_service()
    floor_ms = int(((not_before if not_before is not None else clock()) - _CODE_CLOCK_SKEW_SECONDS) * 1000)
    deadline = clock() + max_wait
    query = f'from:{OTP_FROM} subject:"{OTP_SUBJECT}" newer_than:1d'
    while True:
        try:
            listing = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
            candidates = []
            for meta in listing.get("messages", []) or []:
                msg = service.users().messages().get(userId="me", id=meta["id"], format="full").execute()
                received_ms = int(msg.get("internalDate") or 0)
                if received_ms >= floor_ms:
                    candidates.append((received_ms, msg))
            for _, msg in sorted(candidates, key=lambda pair: pair[0], reverse=True):
                code = extract_code(message_text(msg.get("payload") or {}))
                if code:
                    return code
        except IntakeHold:
            raise
        except Exception:
            pass
        if clock() >= deadline:
            break
        sleep(5)
    raise IntakeHold("No NatGen verification code found in Gmail within timeout")


def login_natgen(
    context: Any,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    credentials: Callable[[], tuple[str, str]] | None = None,
    code_reader: Callable[..., str] | None = None,
) -> Any:
    """Leave exactly one signed-in NatGen tab on ``context``."""
    _close_natgen_tabs(context)
    page = context.new_page()
    page.goto(NATGEN_LANDING, wait_until="domcontentloaded", timeout=60_000)
    sleep(4)
    if is_signed_in(page):
        page.goto(NATGEN_REPORTS, wait_until="domcontentloaded", timeout=60_000)
        sleep(5)
        return page
    if _session_taken_by_another_window(page):
        raise IntakeHold("NatGen session was taken by another window; Enable Login needed")
    if credentials is None:
        user, password = _get_secret(USER_SECRET), _get_secret(PASS_SECRET)
    else:
        user, password = credentials()
    if not user or not password:
        raise IntakeHold("NatGen credentials are not available from secrets")
    page.locator("#txtUserID").fill(user)
    page.locator("#btnLogin").click()
    sleep(5)
    page.locator("input[type='password']").first.fill(password)
    page.locator("button[type='submit'], input[type='submit']").first.click()
    sleep(6)
    text = _body(page, 400).lower()
    if "multi-factor" in text or "verification" in text:
        try:
            page.get_by_text(re.compile(r"Send code to|email", re.I)).first.click(timeout=8000)
        except Exception:
            try:
                page.locator("#loginWith2faEmail").click(timeout=5000)
            except Exception:
                pass
        sent_at = clock()
        sleep(3)
        reader = code_reader or get_natgen_code
        code = reader(not_before=sent_at)
        page.locator("input[type='text'], input[name*='Code'], input[id*='code']").first.fill(code)
        try:
            page.get_by_role("button", name=re.compile("Verify|Continue|Submit", re.I)).first.click(timeout=8000)
        except Exception:
            page.keyboard.press("Enter")
        sleep(10)
    page.goto(NATGEN_REPORTS, wait_until="domcontentloaded", timeout=60_000)
    sleep(6)
    if not is_signed_in(page):
        raise IntakeHold("NatGen sign-in did not leave a signed-in session")
    return page


def ensure_natgen_tab(cdp_url: str = "http://127.0.0.1:9223") -> str:
    """Connect over CDP, sign in if needed, return the reports URL."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise IntakeHold("Carrier Chrome has no browser context")
        page = login_natgen(browser.contexts[0])
        return str(page.url or NATGEN_REPORTS)
