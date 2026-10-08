"""Utica First (UFirst Now) automated login with optional email MFA.

BUILT 2026-10-07 (Ralph). Handles the full Okta login flow:
1. Navigate to Utica First agent portal
2. Enter credentials from GCP secrets
3. If Okta prompts for email verification, automatically retrieve the
   code from Gmail API and enter it
4. Verify landing on UFirst Now portal

The login leaves the tab on the UFirst Now portal home page, ready for
the document pull modules to use via select_utica_page().

Gmail code retrieval uses the delegated service account (same as
gmail_accountability.py) to read the "One-time verification code" email
from carlo@streetsmart.insurance.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from .intake_core import IntakeHold

UTICA_LOGIN_URL = "https://www.uticafirst.com/"
UTICA_HOST = "ufirstnow.uticafirst.com"
OKTA_HOST = "login.uticafirst.com"

# GCP secret names for Utica credentials
UTICA_USER_SECRET = "uticafirst_username"
UTICA_PASS_SECRET = "uticafirst_password"
GCP_PROJECT = "streetsmart-hermes-poc"

# Gmail search for verification codes
GMAIL_CODE_SUBJECT = "One-time verification code"
GMAIL_CODE_FROM = "DoNotReply@uticafirst.com"
_CODE_RE = re.compile(r"\b(\d{6})\b")


def _get_secret(name: str) -> str:
    """Read a secret from GCP Secret Manager."""
    from .gcp_secret_reader import get_secret
    return get_secret(name, project=GCP_PROJECT).strip()


def _get_gmail_service() -> Any:
    """Build delegated Gmail service for carlo@streetsmart.insurance."""
    from .gmail_accountability import build_keyless_delegated_service
    
    service_account = os.environ.get(
        "ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT", ""
    ).strip()
    if not service_account:
        # Gmail API not configured on this host. The caller should handle
        # this by raising IntakeHold with a clear message.
        raise IntakeHold(
            "Utica email verification required but Gmail API is not configured "
            "on this host (ACCOUNTABILITY_GMAIL_DELEGATED_SERVICE_ACCOUNT not set). "
            "Log in manually once to establish a trusted session."
        )
    
    return build_keyless_delegated_service(
        service_account,
        "carlo@streetsmart.insurance",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )


def get_verification_code(max_wait: int = 120) -> str:
    """Poll Gmail for the latest Utica verification code.
    
    Returns the 6-digit code from the most recent email.
    Raises IntakeHold if no code found within max_wait seconds.
    """
    import base64
    
    service = _get_gmail_service()
    deadline = time.time() + max_wait
    
    while time.time() < deadline:
        try:
            # Search for recent verification emails
            results = service.users().messages().list(
                userId="me",
                q=f"from:{GMAIL_CODE_FROM} subject:\"{GMAIL_CODE_SUBJECT}\" newer_than:10m",
                maxResults=1,
            ).execute()
            
            messages = results.get("messages", [])
            if messages:
                msg = service.users().messages().get(
                    userId="me",
                    id=messages[0]["id"],
                    format="full",
                ).execute()
                
                # Extract body
                body = ""
                payload = msg.get("payload", {})
                parts = payload.get("parts", [])
                if parts:
                    for part in parts:
                        if part.get("mimeType") == "text/plain":
                            data = part.get("body", {}).get("data", "")
                            if data:
                                body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                                break
                else:
                    data = payload.get("body", {}).get("data", "")
                    if data:
                        body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                
                # Find 6-digit code
                match = _CODE_RE.search(body)
                if match:
                    return match.group(1)
        except Exception:
            pass
        
        time.sleep(5)
    
    raise IntakeHold("No Utica verification code found in Gmail within timeout")


def _is_logged_in(page: Any) -> bool:
    """Check if the page shows the logged-in UFirst Now portal."""
    try:
        url = str(getattr(page, "url", "") or "").lower()
        if UTICA_HOST not in url:
            return False
        body = page.locator("body").inner_text()
        text = body.lower()
        return "welcome" in text or "carlo ferrara" in text
    except Exception:
        return False


def _is_verification_page(page: Any) -> bool:
    """Check if we're on the Okta email verification page."""
    try:
        body = page.locator("body").inner_text()
        text = body.lower()
        return "verify with your email" in text or "verification code" in text
    except Exception:
        return False


def login_utica(page: Any) -> None:
    """Perform full Utica First login on the given Playwright page.
    
    Handles both the direct SSO path (no MFA) and the email verification
    path (with automatic code retrieval from Gmail).
    
    Raises IntakeHold on failure.
    """
    username = _get_secret(UTICA_USER_SECRET)
    password = _get_secret(UTICA_PASS_SECRET)
    
    if not username or not password:
        raise IntakeHold("Utica credentials not available from secrets")
    
    # Step 1: Navigate to Utica First
    page.goto(UTICA_LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    
    # Step 2: Find and click AGENTS & BROKERS → UFirst Now
    # Try direct navigation to the agent portal first
    try:
        # Look for agent login link
        agent_link = page.get_by_text("AGENTS", exact=False).first
        if agent_link.count() > 0:
            agent_link.click()
            page.wait_for_timeout(3000)
    except Exception:
        pass
    
    # Try to find UFirst Now continue button/link
    try:
        ufirst_link = page.get_by_text("UFirst Now", exact=False).first
        if ufirst_link.count() > 0:
            ufirst_link.click()
            page.wait_for_timeout(5000)
    except Exception:
        pass
    
    # Step 3: Handle Okta login page
    # Wait for the Okta page to load
    page.wait_for_timeout(5000)
    
    url = str(getattr(page, "url", "") or "").lower()
    if OKTA_HOST not in url:
        # Try direct Okta URL
        page.goto(f"https://{OKTA_HOST}/", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
    
    # Fill username if needed
    try:
        user_input = page.locator("input[name='identifier'], input[type='email'], input[name='username']").first
        if user_input.count() > 0 and user_input.is_visible():
            user_input.fill(username)
            page.wait_for_timeout(1000)
            # Click Next if present
            next_btn = page.locator("input[type='submit'], button[type='submit']").first
            if next_btn.count() > 0:
                next_btn.click()
                page.wait_for_timeout(3000)
    except Exception:
        pass
    
    # Fill password
    try:
        pass_input = page.locator("input[type='password'], input[name='credentials.passcode']").first
        if pass_input.count() > 0 and pass_input.is_visible():
            pass_input.fill(password)
            page.wait_for_timeout(1000)
            submit_btn = page.locator("input[type='submit'], button[type='submit']").first
            if submit_btn.count() > 0:
                submit_btn.click()
                page.wait_for_timeout(8000)
    except Exception as e:
        raise IntakeHold(f"Utica password entry failed: {e}")
    
    # Step 4: Check if we're logged in (no MFA path)
    if _is_logged_in(page):
        return
    
    # Step 5: Handle email verification (MFA path)
    if _is_verification_page(page):
        # Click "Send email" to trigger fresh code
        try:
            # Tab to find the send button (learned 2026-10-07: Okta widget needs Tab nav)
            for _ in range(10):
                page.keyboard.press("Tab")
                page.wait_for_timeout(300)
                focused = page.evaluate(
                    "document.activeElement ? document.activeElement.textContent.trim() : ''"
                )
                if "send" in focused.lower() and "email" in focused.lower():
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(5000)
                    break
        except Exception:
            pass
        
        # Get the code from Gmail
        code = get_verification_code(max_wait=120)
        
        # Click "Enter a verification code instead" via Tab
        try:
            for _ in range(10):
                page.keyboard.press("Tab")
                page.wait_for_timeout(300)
                focused = page.evaluate(
                    "document.activeElement ? document.activeElement.textContent.trim() : ''"
                )
                if "verification code" in focused.lower():
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(3000)
                    break
        except Exception:
            pass
        
        # Fill the code
        try:
            code_input = page.locator("input[name='credentials.passcode']").first
            if code_input.count() > 0:
                code_input.fill(code)
                page.wait_for_timeout(1000)
                submit = page.locator("input[type='submit']").first
                if submit.count() > 0:
                    submit.click()
                    page.wait_for_timeout(10000)
        except Exception as e:
            raise IntakeHold(f"Utica code entry failed: {e}")
    
    # Step 6: Verify login
    if not _is_logged_in(page):
        # Wait a bit more for SSO redirect
        page.wait_for_timeout(10000)
        if not _is_logged_in(page):
            raise IntakeHold("Utica login failed - not on UFirst Now portal after auth")
