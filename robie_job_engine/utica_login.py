"""Utica First (UFirst Now) automated login with email MFA.

BUILT 2026-10-07 (Ralph); recovered from hermes-test-01 and tightened in the
Test-patch reconcile (2026-10-08). Flow, as proven live on 2026-10-07:

1. Open uticafirst.com -> AGENTS -> UFirst Now (falls back to the Okta host).
2. Enter the username/password from Secret Manager (``uticafirst_username`` /
   ``uticafirst_password``, project streetsmart-hermes-poc).
3. If Okta asks "Verify with your email": send the email, read the 6-digit
   code from Gmail, choose "Enter a verification code instead", fill
   ``input[name='credentials.passcode']`` and submit.
4. Confirm the tab landed signed in on ufirstnow.uticafirst.com.

MFA REQUIREMENT (see docs/CARRIER_DOCUMENT_RETRIEVAL.md): the code must be
read server-side through the delegated Gmail service account
(``ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT``, gmail.readonly on the
MFA mailbox). Agent/connector Gmail access redacts one-time codes as
``[credential:<uuid>]`` in every format including ``format=raw``, so codes
can never be relayed through a chat agent. If the server-side read also
comes back redacted, this module holds immediately instead of polling.

Nothing here logs, prints, or stores the code or the credentials.
"""

from __future__ import annotations

import base64
import html as html_lib
import os
import re
import time
from typing import Any, Callable

from .intake_core import IntakeHold

UTICA_LOGIN_URL = "https://www.uticafirst.com/"
UTICA_HOST = "ufirstnow.uticafirst.com"
OKTA_HOST = "login.uticafirst.com"

# GCP secret names for Utica credentials
UTICA_USER_SECRET = "uticafirst_username"
UTICA_PASS_SECRET = "uticafirst_password"
GCP_PROJECT = "streetsmart-hermes-poc"

# Mailbox that receives the Okta "One-time verification code" email.
MFA_MAILBOX_ENV = "UTICA_MFA_MAILBOX"
DEFAULT_MFA_MAILBOX = "carlo@streetsmart.insurance"
DELEGATED_SA_ENV = "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

# Gmail search for verification codes
GMAIL_CODE_SUBJECT = "One-time verification code"
GMAIL_CODE_FROM = "DoNotReply@uticafirst.com"
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_REDACTED_RE = re.compile(r"\[credential:[^\]]*\]", re.IGNORECASE)
# Allow for clock skew between this host and Gmail's internalDate.
_CODE_CLOCK_SKEW_SECONDS = 60

MFA_UNCONFIGURED = (
    "Utica email verification required but Gmail API is not configured "
    f"on this host ({DELEGATED_SA_ENV} not set). Configure the delegated "
    "service account (docs/CARRIER_DOCUMENT_RETRIEVAL.md) or log in manually "
    "once to establish a trusted session."
)
MFA_REDACTED = (
    "Utica verification email was found but its code is redacted "
    "([credential:...]). Codes cannot be read through a redacting connector; "
    f"the server must read Gmail directly via {DELEGATED_SA_ENV}."
)


def _get_secret(name: str) -> str:
    """Read a secret from GCP Secret Manager."""
    from .gcp_secret_reader import get_secret
    return get_secret(name, project=GCP_PROJECT).strip()


def mfa_mailbox() -> str:
    return (os.environ.get(MFA_MAILBOX_ENV, "") or DEFAULT_MFA_MAILBOX).strip()


def _get_gmail_service() -> Any:
    """Build the delegated, read-only Gmail service for the MFA mailbox."""
    service_account = os.environ.get(DELEGATED_SA_ENV, "").strip()
    if not service_account:
        # Gmail API not configured on this host.
        raise IntakeHold(MFA_UNCONFIGURED)

    from .gmail_accountability import build_keyless_delegated_service

    return build_keyless_delegated_service(
        service_account,
        mfa_mailbox(),
        scopes=[GMAIL_READONLY_SCOPE],
    )


def _decode_part(data: str) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="ignore")
    except Exception:
        return ""


def message_text(payload: dict[str, Any]) -> str:
    """All text/plain and text/html bodies in a Gmail payload, HTML stripped."""
    chunks: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = str(part.get("mimeType") or "").lower()
        data = (part.get("body") or {}).get("data", "")
        if data and (mime.startswith("text/") or not mime):
            text = _decode_part(data)
            if mime == "text/html":
                text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
            chunks.append(text)
        for child in part.get("parts") or []:
            walk(child)

    walk(payload or {})
    return "\n".join(chunks)


def extract_code(text: str) -> str:
    """Return the 6-digit code, or raise IntakeHold if the code is redacted.

    Returns "" when the text simply has no code.
    """
    if _REDACTED_RE.search(text or ""):
        raise IntakeHold(MFA_REDACTED)
    match = _CODE_RE.search(text or "")
    return match.group(1) if match else ""


def get_verification_code(
    max_wait: int = 120,
    *,
    not_before: float | None = None,
    service: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> str:
    """Poll Gmail for the Utica verification code sent after ``not_before``.

    ``not_before`` is the epoch second the "Send me an email" click happened;
    older emails (earlier codes) are ignored. Raises IntakeHold when Gmail is
    not configured, when the code is redacted, or on timeout.
    """
    service = service if service is not None else _get_gmail_service()
    floor_ms = int(((not_before if not_before is not None else clock()) - _CODE_CLOCK_SKEW_SECONDS) * 1000)
    deadline = clock() + max_wait
    query = f'from:{GMAIL_CODE_FROM} subject:"{GMAIL_CODE_SUBJECT}" newer_than:1d'

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
            # Transient Gmail/API error: keep polling until the deadline.
            pass
        if clock() >= deadline:
            break
        sleep(5)

    raise IntakeHold("No Utica verification code found in Gmail within timeout")


def is_logged_in(page: Any) -> bool:
    """True when the page is the signed-in UFirst Now portal."""
    try:
        url = str(getattr(page, "url", "") or "")
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if (parts.hostname or "").lower() != UTICA_HOST or "login" in parts.path.lower():
            return False
        text = str(page.locator("body").inner_text() or "").lower()
        return "welcome" in text or "carlo ferrara" in text
    except Exception:
        return False


_is_logged_in = is_logged_in  # Ralph's original name


def _is_verification_page(page: Any) -> bool:
    """Check if we're on the Okta email verification page."""
    try:
        text = str(page.locator("body").inner_text() or "").lower()
        return "verify with your email" in text or "verification code" in text
    except Exception:
        return False


def _tab_to_and_press(page: Any, predicate: Callable[[str], bool], *, presses: int = 10) -> bool:
    """Okta's widget sometimes only responds to keyboard focus (learned 2026-10-07)."""
    for _ in range(presses):
        page.keyboard.press("Tab")
        page.wait_for_timeout(300)
        focused = str(page.evaluate(
            "document.activeElement ? document.activeElement.textContent.trim() : ''"
        ) or "")
        if predicate(focused.lower()):
            page.keyboard.press("Enter")
            return True
    return False


def _click_text(page: Any, pattern: re.Pattern[str]) -> bool:
    try:
        target = page.get_by_text(pattern).first
        if target.count() > 0 and target.is_visible():
            target.click()
            return True
    except Exception:
        pass
    return False


def _send_email_code(page: Any) -> None:
    if _click_text(page, re.compile(r"send me an email", re.IGNORECASE)):
        page.wait_for_timeout(5000)
        return
    try:
        if _tab_to_and_press(page, lambda text: "send" in text and "email" in text):
            page.wait_for_timeout(5000)
    except Exception:
        pass


def _choose_code_entry(page: Any) -> None:
    if _click_text(page, re.compile(r"enter a verification code instead", re.IGNORECASE)):
        page.wait_for_timeout(2000)
        return
    try:
        if _tab_to_and_press(page, lambda text: "verification code" in text):
            page.wait_for_timeout(3000)
    except Exception:
        pass


def login_utica(
    page: Any,
    *,
    code_reader: Callable[..., str] = get_verification_code,
    clock: Callable[[], float] = time.time,
) -> None:
    """Perform the full Utica First login on ``page``. Raises IntakeHold on failure."""
    username = _get_secret(UTICA_USER_SECRET)
    password = _get_secret(UTICA_PASS_SECRET)
    if not username or not password:
        raise IntakeHold("Utica credentials not available from secrets")

    # Step 1-2: uticafirst.com -> AGENTS -> UFirst Now
    page.goto(UTICA_LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    for label in ("AGENTS", "UFirst Now"):
        try:
            link = page.get_by_text(label, exact=False).first
            if link.count() > 0:
                link.click()
                page.wait_for_timeout(3000 if label == "AGENTS" else 5000)
        except Exception:
            pass

    # Step 3: Okta sign-in
    page.wait_for_timeout(5000)
    url = str(getattr(page, "url", "") or "").lower()
    if OKTA_HOST not in url and UTICA_HOST not in url:
        page.goto(f"https://{OKTA_HOST}/", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)

    try:
        user_input = page.locator("input[name='identifier'], input[type='email'], input[name='username']").first
        if user_input.count() > 0 and user_input.is_visible():
            user_input.fill(username)
            page.wait_for_timeout(1000)
            next_btn = page.locator("input[type='submit'], button[type='submit']").first
            if next_btn.count() > 0:
                next_btn.click()
                page.wait_for_timeout(3000)
    except Exception:
        pass

    try:
        pass_input = page.locator("input[type='password'], input[name='credentials.passcode']").first
        if pass_input.count() > 0 and pass_input.is_visible():
            pass_input.fill(password)
            page.wait_for_timeout(1000)
            submit_btn = page.locator("input[type='submit'], button[type='submit']").first
            if submit_btn.count() > 0:
                submit_btn.click()
                page.wait_for_timeout(8000)
    except Exception as exc:
        # Never echo the exception text: it can carry the filled value.
        raise IntakeHold(f"Utica password entry failed: {type(exc).__name__}")

    # Step 4: trusted session, no MFA
    if is_logged_in(page):
        return

    # Step 5: email MFA
    if _is_verification_page(page):
        sent_at = clock()
        _send_email_code(page)
        code = code_reader(max_wait=120, not_before=sent_at)
        _choose_code_entry(page)
        try:
            code_input = page.locator("input[name='credentials.passcode']").first
            code_input.wait_for(state="visible", timeout=15000)
            code_input.fill(code)
            page.wait_for_timeout(1000)
            submit = page.locator("input[type='submit'], button[type='submit']").first
            if submit.count() > 0:
                submit.click()
                page.wait_for_timeout(10000)
        except Exception as exc:
            raise IntakeHold(f"Utica code entry failed: {type(exc).__name__}")

    # Step 6: confirm
    if not is_logged_in(page):
        page.wait_for_timeout(10000)  # SSO redirect can lag
        if not is_logged_in(page):
            raise IntakeHold("Utica login failed - not on UFirst Now portal after auth")
