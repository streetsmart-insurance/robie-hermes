"""Safeco dec-page / mortgagee-doc puller (Phase 1, read-only).

Runtime: BOX (default). Reason: no reachability lesson is recorded for
the Safeco agent portal in repo AGENTS.md; the box's proxied browser is
the default for carrier portals. UNVERIFIED — confirm with a read-only
smoke test.

Portal URL and selectors below are UNVERIFIED (DESIGNED, not TEST
VERIFIED).
"""

from __future__ import annotations

from pathlib import Path

from .base import (
    AdapterSpec,
    BrowserPort,
    DownloadResult,
    PolicyRef,
    SessionExpiredError,
    TransientBrowserError,
)
from ..phase1_credentials import load_portal_credentials

ADAPTER = AdapterSpec(
    carrier_id="safeco",
    carrier_name="Safeco Insurance",
    portal_url="https://www.safeconow.com",  # UNVERIFIED
    runtime="box",
    runtime_reason=(
        "Default carrier-portal runtime (box browser via residential proxy "
        "9.142.10.166:5822). No reachability lesson recorded for Safeco; "
        "UNVERIFIED until a read-only smoke test."
    ),
    username_env="PHASE1_SAFECO_USERNAME_SECRET",
    password_env="PHASE1_SAFECO_PASSWORD_SECRET",
    doc_kind="dec_pages_or_mortgagee_docs",
    notes=(
        "UNVERIFIED: portal URL, login selectors, and document navigation "
        "are DESIGNED; confirm in a read-only smoke test before the pilot.",
    ),
)


def _safe_name(policy_number: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in policy_number)


def download(
    policy: PolicyRef,
    dest_dir: Path,
    browser: BrowserPort,
    accessor=None,
) -> DownloadResult:
    creds = load_portal_credentials(
        ADAPTER.username_env, ADAPTER.password_env, accessor
    )
    dest = Path(dest_dir) / f"{_safe_name(policy.policy_number)}-dec-pages.pdf"
    try:
        browser.goto(ADAPTER.portal_url)
        # UNVERIFIED selectors — confirm against the live portal first.
        browser.fill("#username", creds.username)
        browser.fill("#password", creds.password)
        browser.click("button[type=submit]")
        browser.wait_for_selector("#policy-search")
        browser.fill("#policy-search", policy.policy_number)
        browser.click("#policy-search-submit")
        browser.wait_for_selector("#policy-documents")
        browser.click("#policy-documents")
        browser.download("#download-dec-pages", dest)
    except (TransientBrowserError, SessionExpiredError):
        raise  # runner retries transient / re-logins once on expiry
    except Exception as exc:  # noqa: BLE001 - record, never raise past the runner
        return DownloadResult(ok=False, detail=f"safeco pull failed: {type(exc).__name__}")
    return DownloadResult(
        ok=True,
        file_path=dest,
        detail=f"dec pages downloaded for {policy.policy_number}",
    )
