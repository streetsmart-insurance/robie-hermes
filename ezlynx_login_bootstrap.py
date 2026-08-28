from __future__ import annotations

import base64
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.cloud import secretmanager
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from playwright.sync_api import sync_playwright


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


def secret(name: str) -> str:
    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/streetsmart-hermes-poc/secrets/{name}"
    enabled = list(
        client.list_secret_versions(
            request={"parent": parent, "filter": "state:ENABLED"}
        )
    )
    if not enabled:
        raise RuntimeError(f"No enabled version exists for required secret {name}")
    newest = max(enabled, key=lambda version: version.create_time)
    response = client.access_secret_version(request={"name": newest.name})
    return response.payload.data.decode("utf-8").strip()


class MailboxIdentityError(RuntimeError):
    pass


def gmail_service():
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
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


def authenticated(page) -> bool:
    url = page.url.lower()
    if not url.startswith(AUTHENTICATED_APP_PREFIX):
        return False
    try:
        login_controls = page.locator(LOGIN_CONTROL_SELECTOR).count()
        internal_links = page.locator(INTERNAL_WEB_LINK_SELECTOR).count()
    except Exception:
        return False
    return login_controls == 0 and internal_links > 0


def navigate_to_submission_route(page) -> None:
    page.goto(SUBMISSION_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2_000)


def main() -> int:
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

        if authenticated(page):
            print("AUTHENTICATED")
            return 0

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
            if authenticated(page):
                print("AUTHENTICATED")
                return 0
            url = page.url.lower()

        if "/auth/account/login" in url:
            page.locator("#txtUserName").fill(secret("ezlynx-username"))
            page.locator("#txtPassword").fill(secret("ezlynx-password"))
            page.locator("#btnLogin").click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(2_000)
            if authenticated(page):
                print("AUTHENTICATED")
                return 0
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
        if authenticated(page):
            print("AUTHENTICATED")
            return 0
        print("MFA_NOT_ACCEPTED")
        return 23


if __name__ == "__main__":
    raise SystemExit(main())
