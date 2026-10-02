from __future__ import annotations

import base64
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CDP_URL = "http://127.0.0.1:9222"
TOKEN_PATH = Path("/opt/streetsmart-hermes/.hermes/robie_google_token.json")
EXPECTED_MAILBOX = "robie@streetsmart.insurance"
OTP_PATTERNS = (
    re.compile(r"(?:verification|security|authentication|one[- ]time)\s+code\D{0,40}(\d{6})", re.I),
    re.compile(r"\bcode\D{0,20}(\d{6})\b", re.I),
)
AUTHENTICATED_APP_PREFIX = "https://app.ezlynx.com/web/"
SUBMISSION_URL = "https://app.ezlynx.com/web/submission-center/overview/submissions"
LOGIN_CONTROL_SELECTOR = "#txtUserName, #txtPassword, #btnLogin"
INTERNAL_WEB_LINK_SELECTOR = 'a[href^="/web/"], a[href*="app.ezlynx.com/web/"]'
# A real shell control. The tab URL, by itself, is not authentication.
AUTHENTICATED_ELEMENT_SELECTOR = (
    "#quickSearchInput, "
    "a[href*='/web/account/'], "
    'a[href^="/web/"], a[href*="app.ezlynx.com/web/"]'
)
SESSION_LIMIT_CONTINUE = (
    "button:has-text('Continue'), "
    "input[type='submit'][value='Continue'], "
    "a:has-text('Continue')"
)
SESSION_LIMIT_REFUSED = 29
SESSION_LIMIT_NOT_CLEARED = 31


def secret(name: str) -> str:
    """Always read the newest ENABLED version (never a stale env pin).

    ``ROBIE_EZLYNX_*_SECRET`` may still be present for operators and for
    ``load_ezlynx_credentials``, but SSRobie login on Test historically pinned
    ``ezlynx-password`` to ``versions/1`` while newer ENABLED versions worked.
    Matching Production discipline: list ENABLED by create_time and access that
    version. If an env pin points elsewhere, log a one-line warning (no value).
    """
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/streetsmart-hermes-poc/secrets/{name}"
    reference_env = {
        "ezlynx-username": "ROBIE_EZLYNX_USERNAME_SECRET",
        "ezlynx-password": "ROBIE_EZLYNX_PASSWORD_SECRET",
    }
    reference = os.environ.get(reference_env.get(name, ""), "").strip()
    if reference:
        expected = rf"{re.escape(parent)}/versions/[1-9]\d*"
        if not re.fullmatch(expected, reference):
            raise RuntimeError(
                f"Pinned Secret Manager reference is invalid for required secret {name}"
            )

    enabled = list(
        client.list_secret_versions(
            request={"parent": parent, "filter": "state:ENABLED"}
        )
    )
    if not enabled:
        raise RuntimeError(f"No enabled version exists for required secret {name}")
    newest = max(enabled, key=lambda version: version.create_time)
    newest_name = str(newest.name)
    if reference:
        pinned_suffix = reference.rsplit("/", 1)[-1]
        newest_suffix = newest_name.rsplit("/", 1)[-1]
        if pinned_suffix != newest_suffix:
            print(
                f"login-secret: ignoring stale pin for {name} "
                f"(env versions/{pinned_suffix}; newest ENABLED versions/{newest_suffix})",
                flush=True,
            )
    response = client.access_secret_version(request={"name": newest.name})
    return response.payload.data.decode("utf-8").strip()


class MailboxIdentityError(RuntimeError):
    pass


def build_legacy_mailbox_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def build_keyless_mailbox_service(service_account_email: str):
    from robie_job_engine.ringcentral_email_sync import (
        build_keyless_report_mailbox_service,
    )

    return build_keyless_report_mailbox_service(
        service_account_email,
        EXPECTED_MAILBOX,
    )


def gmail_service():
    service_account = os.environ.get(
        "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", ""
    ).strip()
    if service_account:
        service = build_keyless_mailbox_service(service_account)
    else:
        service = build_legacy_mailbox_service()
    profile = service.users().getProfile(userId="me").execute()
    mailbox = str(profile.get("emailAddress") or "").strip().casefold()
    if mailbox != EXPECTED_MAILBOX:
        raise MailboxIdentityError("Robie mailbox identity did not match")
    return service


def decoded_body(payload: dict) -> str:
    chunks: list[str] = []
    data = payload.get("body", {}).get("data")
    if data:
        chunks.append(base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace"))
    for part in payload.get("parts", []):
        chunks.append(decoded_body(part))
    return "\n".join(chunks)


def newest_ezlynx_code(
    not_before: datetime, exclude_message_id: str | None = None
) -> tuple[str, str] | None:
    service = gmail_service()
    result = service.users().messages().list(
        userId="me", q="newer_than:15m", maxResults=50
    ).execute()
    messages = result.get("messages", [])
    floor = max(
        not_before - timedelta(seconds=30),
        datetime.now(timezone.utc) - timedelta(minutes=14),
    )
    fetched = []
    for meta in messages:
        message = service.users().messages().get(
            userId="me", id=meta["id"], format="full"
        ).execute()
        received_at = datetime.fromtimestamp(int(message.get("internalDate", "0")) / 1000, timezone.utc)
        fetched.append((received_at, message))
    for received_at, message in sorted(fetched, key=lambda pair: pair[0], reverse=True):
        if received_at < floor:
            continue
        if message.get("id") == exclude_message_id:
            continue
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in message.get("payload", {}).get("headers", [])
        }
        haystack = "\n".join(
            (headers.get("from", ""), headers.get("subject", ""), decoded_body(message.get("payload", {})))
        )
        if "ezlynx" not in haystack.lower():
            continue
        for pattern in OTP_PATTERNS:
            match = pattern.search(haystack)
            if match:
                return str(message["id"]), match.group(1)
    return None


def visible_page(browser):
    pages = [page for context in browser.contexts for page in context.pages]
    if pages:
        return pages[-1]
    contexts = list(getattr(browser, "contexts", None) or [])
    if contexts:
        opener = getattr(contexts[0], "new_page", None)
        if callable(opener):
            page = opener()
            if page is not None:
                return page
    raise RuntimeError(
        "INCONCLUSIVE: Persistent EZLynx browser has no page and no context; "
        "session is not fine"
    )


def page_text(page) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=5_000) or "")
    except Exception:
        return ""


def session_limit_detail(text: str) -> str | None:
    """The two-session prompt, including which session Continue would end."""
    raw = " ".join(str(text or "").split())
    folded = raw.casefold()
    if "limited to 2 active sessions" not in folded:
        return None
    if "log out the session" not in folded and "continue will log out" not in folded:
        return None
    start = folded.find("limited to 2 active sessions")
    return raw[start:start + 300].strip()


def session_limit_on(page) -> str | None:
    return session_limit_detail(page_text(page))


def authenticated(page) -> bool:
    """True only after a real app element is present. The URL is not enough."""
    if session_limit_on(page):
        return False
    url = str(getattr(page, "url", "") or "").lower()
    if not url.startswith(AUTHENTICATED_APP_PREFIX):
        return False
    try:
        login_controls = page.locator(LOGIN_CONTROL_SELECTOR).count()
        internal_links = page.locator(INTERNAL_WEB_LINK_SELECTOR).count()
        # A shell control after reload. The URL and a stale link count are
        # not enough; the two-session prompt is refused above.
        shell = page.locator(AUTHENTICATED_ELEMENT_SELECTOR).count()
    except Exception:
        return False
    return login_controls == 0 and internal_links > 0 and shell > 0


def confirm_authenticated(page) -> bool:
    """Reload, then require an app element. A stale /web/ URL is not proof."""
    if session_limit_on(page):
        return False
    try:
        page.reload(wait_until="domcontentloaded")
    except Exception:
        return False
    return authenticated(page)


def navigate_to_submission_route(page) -> None:
    page.goto(SUBMISSION_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2_000)


def session_is_logged_in_on_app_page(page) -> bool:
    """Open the app, reload, and judge login from a page element."""
    navigate_to_submission_route(page)
    return confirm_authenticated(page)


def press_session_limit_continue(page) -> None:
    """Click the Continue control on the two-session prompt. Nothing else."""
    locator = page.locator(SESSION_LIMIT_CONTINUE)
    if locator.count() == 0:
        role = getattr(page, "get_by_role", None)
        if not callable(role):
            raise RuntimeError("SESSION_LIMIT_CONTINUE_NOT_FOUND")
        role("button", name="Continue").click()
        return
    first = getattr(locator, "first", locator)
    first.click()


def handle_two_session_prompt(page, *, gate) -> int:
    """Press Continue only when this environment holds the driver lease.

    The log names the session that was ended. A lease refusal does not
    click Continue and does not return the username-login code.
    """
    detail = session_limit_on(page) or "the session that is currently active"
    try:
        gate()
    except Exception as exc:
        from robie_job_engine.ezlynx_driver_gate import EzlynxDriverGateRefused

        if not isinstance(exc, EzlynxDriverGateRefused):
            raise
        print(f"SESSION_LIMIT_REFUSED: did not press Continue ({exc})", flush=True)
        print(f"other session left active: {detail}", flush=True)
        return SESSION_LIMIT_REFUSED
    press_session_limit_continue(page)
    print(
        f"SESSION_LIMIT_CONTINUED: ended the other active session: {detail}",
        flush=True,
    )
    try:
        page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass
    if confirm_authenticated(page):
        print("AUTHENTICATED", flush=True)
        return 0
    print("SESSION_LIMIT_NOT_CLEARED", flush=True)
    return SESSION_LIMIT_NOT_CLEARED


def resolve_login_barrier(page, *, gate) -> int | None:
    """Handle a two-session prompt or a proved shell. None continues login."""
    if session_limit_on(page):
        return handle_two_session_prompt(page, gate=gate)
    if confirm_authenticated(page):
        print("AUTHENTICATED", flush=True)
        return 0
    if session_limit_on(page):
        return handle_two_session_prompt(page, gate=gate)
    return None


def ensure_login_form(page) -> None:
    """Reload a blank login page and wait until the username field is visible."""
    field = page.locator("#txtUserName")
    try:
        field.wait_for(state="visible", timeout=8_000)
        return
    except Exception:
        page.reload(wait_until="domcontentloaded")
    field.wait_for(state="visible", timeout=15_000)


def main() -> int:
    from playwright.sync_api import sync_playwright
    from robie_job_engine.ezlynx_driver_gate import (
        EzlynxDriverGateRefused,
        require_driver_in,
    )

    try:
        require_driver_in()
    except EzlynxDriverGateRefused as exc:
        print(str(exc))
        return 28

    try:
        # Verify the OAuth identity before retrieving credentials or requesting
        # an MFA message. Carlo's mailbox must never be used as a fallback.
        gmail_service()
    except MailboxIdentityError:
        print("MAILBOX_IDENTITY_MISMATCH")
        return 25
    except Exception:
        print("ROBIE_MAILBOX_AUTH_REQUIRED")
        return 26
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(CDP_URL)
        try:
            page = visible_page(browser)
        except RuntimeError as exc:
            if str(exc).startswith("INCONCLUSIVE"):
                print("INCONCLUSIVE")
                return 27
            raise
        page.set_default_timeout(20_000)

        barrier = resolve_login_barrier(page, gate=require_driver_in)
        if barrier is not None:
            return barrier

        url = page.url.lower()
        recognized_auth_route = any(
            route in url
            for route in (
                "/auth/account/login",
                "/auth/twofactorverification/typeselection",
                "/auth/twofactorverification/verificationcode",
            )
        )
        if not recognized_auth_route:
            navigate_to_submission_route(page)
            barrier = resolve_login_barrier(page, gate=require_driver_in)
            if barrier is not None:
                return barrier
            url = page.url.lower()

        if "/auth/account/login" in url:
            ensure_login_form(page)
            page.locator("#txtUserName").fill(secret("ezlynx-username"))
            page.locator("#txtPassword").fill(secret("ezlynx-password"))
            page.locator("#btnLogin").click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(2_000)
            barrier = resolve_login_barrier(page, gate=require_driver_in)
            if barrier is not None:
                return barrier
            url = page.url.lower()

        previous = newest_ezlynx_code(datetime.now(timezone.utc) - timedelta(minutes=14))
        previous_message_id = previous[0] if previous else None
        if "/auth/twofactorverification/typeselection" in url:
            requested_at = datetime.now(timezone.utc)
            page.locator("#VerificationType_EMAIL-input").check()
            page.locator("#two-factor-next").click()
            page.wait_for_load_state("domcontentloaded")

        elif "/auth/twofactorverification/verificationcode" in url:
            requested_at = datetime.now(timezone.utc)
            page.locator("#btnResend").click()
            page.wait_for_timeout(1_000)

        else:
            print("AUTH_STATE_REQUIRES_USERNAME_LOGIN")
            return 24

        deadline = time.monotonic() + 120
        candidate = None
        while time.monotonic() < deadline and candidate is None:
            candidate = newest_ezlynx_code(requested_at, previous_message_id)
            if candidate is None:
                time.sleep(5)
        if candidate is None:
            print("MFA_CODE_NOT_FOUND")
            return 20
        _, code = candidate

        code_inputs = page.locator(
            "input[inputmode=numeric], input[autocomplete=one-time-code], "
            "input[name*=code i], input[id*=code i], input[type=tel], input[type=text]"
        )
        if code_inputs.count() == 0:
            print("MFA_INPUT_NOT_FOUND")
            return 21
        code_inputs.first.fill(code)
        trust = page.locator("#trust-this-computer-input")
        if trust.count() and not trust.is_checked():
            trust.check()
        submit = page.locator("button[type=submit], input[type=submit]")
        if submit.count() == 0:
            print("MFA_SUBMIT_NOT_FOUND")
            return 22
        submit.first.click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2_000)
        barrier = resolve_login_barrier(page, gate=require_driver_in)
        if barrier is not None:
            return barrier
        print("MFA_NOT_ACCEPTED")
        return 23


if __name__ == "__main__":
    raise SystemExit(main())
