"""Pie Insurance audit-paper puller (Phase 1, read-only).

Runtime: BOX (default). Reason: no reachability lesson is recorded for
Pie in repo AGENTS.md; the box's proxied browser is the default for
carrier portals. UNVERIFIED — confirm with a read-only smoke test.

Portal URL and selectors below are UNVERIFIED (DESIGNED, not TEST
VERIFIED).
"""

from __future__ import annotations

from pathlib import Path

from .base import (
    AdapterSpec,
    BrowserPort,
    DownloadResult,
    LoginStalledError,
    PolicyRef,
    SessionExpiredError,
    TransientBrowserError,
    login_or_stall,
)
from ..phase1_credentials import load_portal_credentials

ADAPTER = AdapterSpec(
    carrier_id="pie",
    carrier_name="Pie Insurance",
    portal_url="https://www.pieinsurance.com/agent-login",  # UNVERIFIED
    runtime="box",
    runtime_reason=(
        "Default carrier-portal runtime (box browser via residential proxy "
        "9.142.10.166:5822). No reachability lesson recorded for Pie; "
        "UNVERIFIED until a read-only smoke test."
    ),
    username_env="PHASE1_PIE_USERNAME_SECRET",
    password_env="PHASE1_PIE_PASSWORD_SECRET",
    doc_kind="audit_papers",
    # UNVERIFIED: confirm the actual authenticated-state selector in a
    # read-only smoke test (it must be absent on the login page itself).
    logged_in_indicator="a[href*='logout']",
    notes=(
        "UNVERIFIED: portal URL, login selectors, and audit-document "
        "navigation are DESIGNED; confirm in a read-only smoke test "
        "before the pilot.",
    ),
)


def _safe_name(policy_number: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in policy_number)


def login(browser: BrowserPort, accessor=None) -> None:
    """Login steps only. Raises LoginStalledError when the authenticated
    state is not reached — possible MFA, changed login page, or bad
    credentials. Never retried: the runner records the policy blocked
    instead of retrying a stalled login.
    """
    creds = load_portal_credentials(
        ADAPTER.username_env, ADAPTER.password_env, accessor
    )

    def _steps() -> None:
        browser.goto(ADAPTER.portal_url)
        # UNVERIFIED selectors — confirm against the live portal first.
        browser.fill("#username", creds.username)
        browser.fill("#password", creds.password)
        browser.click("button[type=submit]")
        browser.wait_for_selector("#policy-search")

    login_or_stall(ADAPTER, browser, _steps)


def download(
    policy: PolicyRef,
    dest_dir: Path,
    browser: BrowserPort,
    accessor=None,
) -> DownloadResult:
    dest = Path(dest_dir) / f"{_safe_name(policy.policy_number)}-audit-papers.pdf"
    try:
        login(browser, accessor)
        browser.fill("#policy-search", policy.policy_number)
        browser.click("#policy-search-submit")
        browser.wait_for_selector("#audit-documents")
        browser.click("#audit-documents")
        browser.download("#download-audit-papers", dest)
    except (TransientBrowserError, SessionExpiredError, LoginStalledError):
        raise  # runner retries transient / re-logins once on expiry / blocks on stall
    except Exception as exc:  # noqa: BLE001 - record, never raise past the runner
        return DownloadResult(ok=False, detail=f"pie pull failed: {type(exc).__name__}")
    return DownloadResult(
        ok=True,
        file_path=dest,
        detail=f"audit papers downloaded for {policy.policy_number}",
    )
