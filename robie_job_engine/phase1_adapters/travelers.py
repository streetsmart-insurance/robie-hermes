"""Travelers document puller (Phase 1, read-only).

Serves BOTH pilot Travelers rows:
- 4246 audit papers (UB4J350177)
- 4744 dec pages / mortgagee docs (6103713686331)

Runtime: BOX (default). Reason: no reachability lesson is recorded for
the Travelers agent portal in repo AGENTS.md; the box's proxied browser
is the default for carrier portals. Nicole confirmed the Travelers
login is provisioned and active (2026-09-15); portal reachability from
the box is UNVERIFIED until a read-only smoke test.

Portal URL and selectors below are UNVERIFIED (DESIGNED, not TEST
VERIFIED).
"""

from __future__ import annotations

from pathlib import Path

from .base import AdapterSpec, BrowserPort, DownloadResult, PolicyRef
from ..phase1_credentials import load_portal_credentials

ADAPTER = AdapterSpec(
    carrier_id="travelers",
    carrier_name="Travelers",
    portal_url="https://www.travelers.com/agent-login",  # UNVERIFIED
    runtime="box",
    runtime_reason=(
        "Default carrier-portal runtime (box browser via residential proxy "
        "9.142.10.166:5822). Travelers login provisioned and active per "
        "Nicole 2026-09-15; box reachability UNVERIFIED until a read-only "
        "smoke test."
    ),
    username_env="PHASE1_TRAVELERS_USERNAME_SECRET",
    password_env="PHASE1_TRAVELERS_PASSWORD_SECRET",
    doc_kind="audit_papers_or_dec_pages",
    notes=(
        "UNVERIFIED: portal URL, login selectors, and document navigation "
        "are DESIGNED; confirm in a read-only smoke test before the pilot.",
        "doc_kind comes from the pilot row (audit_papers for 4246, "
        "dec_pages_or_mortgagee_docs for 4744).",
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
    suffix = "audit-papers" if policy.doc_kind == "audit_papers" else "dec-pages"
    dest = Path(dest_dir) / f"{_safe_name(policy.policy_number)}-{suffix}.pdf"
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
        browser.download("#download-document", dest)
    except Exception as exc:  # noqa: BLE001 - record, never raise past the runner
        return DownloadResult(ok=False, detail=f"travelers pull failed: {type(exc).__name__}")
    return DownloadResult(
        ok=True,
        file_path=dest,
        detail=f"{suffix} downloaded for {policy.policy_number}",
    )
