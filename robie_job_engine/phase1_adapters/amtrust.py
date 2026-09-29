"""AmTrust audit-paper puller (Phase 1, read-only).

Runtime: BOX (hermes-poc-01 via residential proxy 9.142.10.166:5822).
Reason: AmTrust's F5 bot defense 403-blocks the sandbox browser (proven
2026-09-15, repo AGENTS.md). The box's proxied browser is the only
working path. Never retry AmTrust from the sandbox.

Portal URL and selectors below are UNVERIFIED (DESIGNED, not TEST
VERIFIED): confirm with a read-only smoke test before the pilot runs.
"""

from __future__ import annotations

from pathlib import Path

from .base import AdapterSpec, BrowserPort, DownloadResult, PolicyRef
from ..phase1_credentials import load_portal_credentials

ADAPTER = AdapterSpec(
    carrier_id="amtrust",
    carrier_name="AmTrust Financial Services, Inc",
    portal_url="https://www.amtrustfinancial.com/agent-login",  # UNVERIFIED
    runtime="box",
    runtime_reason=(
        "AmTrust F5 bot defense 403-blocks the sandbox browser (proven "
        "2026-09-15, repo AGENTS.md). Box browser via residential proxy "
        "9.142.10.166:5822 is the only working path."
    ),
    username_env="PHASE1_AMTRUST_USERNAME_SECRET",
    password_env="PHASE1_AMTRUST_PASSWORD_SECRET",
    doc_kind="audit_papers",
    notes=(
        "UNVERIFIED: portal URL, login selectors, and audit-document "
        "navigation are DESIGNED from the agent-portal layout; confirm in "
        "a read-only smoke test before the pilot.",
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
    dest = Path(dest_dir) / f"{_safe_name(policy.policy_number)}-audit-papers.pdf"
    try:
        browser.goto(ADAPTER.portal_url)
        # UNVERIFIED selectors — confirm against the live portal first.
        browser.fill("#username", creds.username)
        browser.fill("#password", creds.password)
        browser.click("button[type=submit]")
        browser.wait_for_selector("#policy-search")
        browser.fill("#policy-search", policy.policy_number)
        browser.click("#policy-search-submit")
        browser.wait_for_selector("#audit-documents")
        browser.click("#audit-documents")
        browser.download("#download-audit-papers", dest)
    except Exception as exc:  # noqa: BLE001 - record, never raise past the runner
        return DownloadResult(ok=False, detail=f"amtrust pull failed: {type(exc).__name__}")
    return DownloadResult(
        ok=True,
        file_path=dest,
        detail=f"audit papers downloaded for {policy.policy_number}",
    )
