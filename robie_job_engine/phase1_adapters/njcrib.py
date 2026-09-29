"""NJCRIB document puller (Phase 1, read-only).

Serves BOTH pilot rows that go through the NJCRIB portal:
- 4246 audit papers (NJCRIB - Liberty Assigned Risk)
- 4247 renewal offer/bill (NJCRIB - Hartford Assigned Risk)

IMPORTANT: this is the NJCRIB assigned-risk portal, NOT the Hartford EBC
portal. The Hartford EBC exception (box-unreachable, sandbox-only) does
NOT apply here — no Hartford EBC navigation happens in this adapter.

Runtime: BOX (default). Reason: no reachability lesson is recorded for
njcrib in repo AGENTS.md; the box's proxied browser is the default for
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
    carrier_id="njcrib",
    carrier_name="NJCRIB (NJ Compensation Rating & Inspection Bureau)",
    portal_url="https://www.njcrib.com",  # UNVERIFIED
    runtime="box",
    runtime_reason=(
        "Default carrier-portal runtime (box browser via residential proxy "
        "9.142.10.166:5822). No reachability lesson recorded for NJCRIB; "
        "UNVERIFIED until a read-only smoke test. This is the NJCRIB "
        "portal, not Hartford EBC — the Hartford box-block does not apply."
    ),
    username_env="PHASE1_NJCRIB_USERNAME_SECRET",
    password_env="PHASE1_NJCRIB_PASSWORD_SECRET",
    doc_kind="audit_papers_or_renewal",
    # UNVERIFIED: confirm the actual authenticated-state selector in a
    # read-only smoke test (it must be absent on the login page itself).
    logged_in_indicator="a[href*='logout']",
    notes=(
        "UNVERIFIED: portal URL, login selectors, and document navigation "
        "are DESIGNED; confirm in a read-only smoke test before the pilot.",
        "doc_kind comes from the pilot row (audit_papers for 4246, "
        "renewal_offer_or_bill for 4247).",
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
    suffix = "audit-papers" if policy.doc_kind == "audit_papers" else "renewal"
    dest = Path(dest_dir) / f"{_safe_name(policy.policy_number)}-{suffix}.pdf"
    try:
        login(browser, accessor)
        browser.fill("#policy-search", policy.policy_number)
        browser.click("#policy-search-submit")
        browser.wait_for_selector("#policy-documents")
        browser.click("#policy-documents")
        browser.download("#download-document", dest)
    except (TransientBrowserError, SessionExpiredError, LoginStalledError):
        raise  # runner retries transient / re-logins once on expiry / blocks on stall
    except Exception as exc:  # noqa: BLE001 - record, never raise past the runner
        return DownloadResult(ok=False, detail=f"njcrib pull failed: {type(exc).__name__}")
    return DownloadResult(
        ok=True,
        file_path=dest,
        detail=f"{suffix} downloaded for {policy.policy_number}",
    )
