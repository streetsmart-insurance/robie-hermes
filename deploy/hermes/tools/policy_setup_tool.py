#!/usr/bin/env python3
"""Hermes tool: ezlynx_policy_setup.

Email and Chat jobs that ask to create a homeowners policy on applicant
220250093 must call this tool — not wander with playwright_exec. The handler
invokes the Job Engine's EzlynxPolicySetupPage.setup_policy_by_lob, which
runs search-first gold create + Save & Continue Edit + FormEntry coverages by
literal label. No execute_code, no networkidle, no bind.
"""
from __future__ import annotations

import asyncio
import os

from tools.registry import registry, tool_error, tool_result

POLICY_SETUP_SCHEMA = {
    "name": "ezlynx_policy_setup",
    "description": (
        "Create a homeowners policy on EZLynx applicant 220250093 and fill its "
        "FormEntry Coverages tab. USE THIS TOOL — not playwright_exec — whenever "
        "the job asks to create, set up, or complete a homeowners policy. "
        "The engine runs search-first (no duplicate), creates with the gold "
        "carrier payload only when absent, mints the FormEntry via Save & "
        "Continue Edit, and fills coverages by literal label (Dwelling, Other "
        "Structures, Personal Property, Loss of Use, Blanket, Personal "
        "Liability EA OCC, Medical Payments EA PER). Applicant 220250093 only. "
        "Never binds. Returns the engine's evidence report."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "policy_number": {
                "type": "string",
                "description": "Policy number to create (e.g. TEST-HO-20260911-E01).",
            },
            "effective_date": {
                "type": "string",
                "description": "MM/DD/YYYY or ISO.",
            },
            "expiration_date": {
                "type": "string",
                "description": "MM/DD/YYYY or ISO.",
            },
            "dwelling": {"type": "string", "description": "Coverage A limit."},
            "other_structures": {"type": "string", "description": "Coverage B limit."},
            "personal_property": {"type": "string", "description": "Coverage C limit."},
            "loss_of_use": {"type": "string", "description": "Coverage D limit."},
            "personal_liability": {
                "type": "string",
                "description": "Personal Liability EA OCC limit.",
            },
            "medical_payments": {
                "type": "string",
                "description": "Medical Payments EA PER limit.",
            },
        },
        "required": ["policy_number", "effective_date", "expiration_date"],
    },
}


def _select_job_page(browser):
    """Select the current job tab. Never contexts[0]/pages[0] (first-ezlynx-wins).

    Uses the same recorder-hint + scored selection as playwright_exec, so the
    tool attaches to the job-owned page even when another EZLynx tab (e.g.
    Submission Center) is in front. Raises PLAYWRIGHT_BLOCKED instead of
    silently driving the wrong tab.
    """
    from robie_job_engine.recording_tab import (
        read_page_hint,
        resolve_hint_file,
        select_playwright_page,
    )

    contexts = browser.contexts
    if not contexts:
        raise RuntimeError("PLAYWRIGHT_BLOCKED: Chrome has no browser context")
    pages = [page for ctx in contexts for page in ctx.pages]
    hinted = read_page_hint(resolve_hint_file()) or {}
    page = select_playwright_page(pages, hint_url=hinted.get("url") or None)
    if page is None:
        raise RuntimeError(
            "PLAYWRIGHT_BLOCKED: could not select the current job tab; "
            "refusing pages[0] / first-ezlynx-wins"
        )
    return page


def _run_policy_setup(args: dict) -> dict:
    from playwright.async_api import async_playwright

    from robie_job_engine.ezlynx_policy_setup import (
        EzlynxPolicySetupPage,
        HomeownersCoverageItem,
        PolicyShellInput,
    )
    from robie_job_engine.recording_tab import publish_live_playwright_hint

    async def _main():
        cdp_url = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.connect_over_cdp(cdp_url, timeout=15000)
            # Attach to the current job tab the same way playwright_exec does:
            # recorder hint + scored selection. Never contexts[0]/pages[0]
            # (first-ezlynx-wins drove the Submission Center tab in job c1ffb79a).
            page = _select_job_page(browser)
            publish_live_playwright_hint(
                [tab for ctx in browser.contexts for tab in ctx.pages], page=page
            )
            setup = EzlynxPolicySetupPage(page)
            shell = PolicyShellInput(
                applicant_id="220250093",
                lob="HOME",
                policy_number=str(args.get("policy_number") or "").strip(),
                effective_date=str(args.get("effective_date") or ""),
                expiration_date=str(args.get("expiration_date") or ""),
                homeowners_coverage=HomeownersCoverageItem(
                    dwelling_a=str(args.get("dwelling") or ""),
                    other_structures_b=str(args.get("other_structures") or ""),
                    personal_property_c=str(args.get("personal_property") or ""),
                    loss_of_use_d=str(args.get("loss_of_use") or ""),
                    liability_e=str(args.get("personal_liability") or "500000"),
                    med_pay_f=str(args.get("medical_payments") or "5000"),
                ),
            )
            result = await setup.setup_policy_by_lob(shell)
            return result.to_dict()
        finally:
            await pw.stop()

    return asyncio.run(_main())


def _mark_policy_setup_tool_called() -> None:
    """Record that the tool ran, releasing the playwright_exec order guard."""
    try:
        from robie_job_engine.policy_setup_dispatch import POLICY_SETUP_REQUIRED_KIND
        from robie_job_engine.store import JobStore

        job_id = (
            os.environ.get("ROBIE_JOB_ID") or os.environ.get("JOB_ID") or ""
        ).strip()
        db_path = os.environ.get("ROBIE_JOB_DB") or ""
        if not job_id or not db_path:
            return
        store = JobStore(db_path)
        marker = store.get_checkpoint(job_id, POLICY_SETUP_REQUIRED_KIND) or {}
        marker["tool_called"] = True
        store.checkpoint(job_id, POLICY_SETUP_REQUIRED_KIND, marker)
    except Exception:
        pass


def ezlynx_policy_setup_handler(args: dict, **kwargs):
    policy_number = str((args or {}).get("policy_number") or "").strip()
    if not policy_number:
        return tool_error("policy_number is required")
    try:
        report = _run_policy_setup(args or {})
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return tool_error(f"{type(exc).__name__}: {exc}")
    # Only mark tool_called when the handler created or found the policy.
    # A failed create must not release the POLICY_SETUP_ORDER guard.
    if isinstance(report, dict) and report.get("success"):
        _mark_policy_setup_tool_called()
    return tool_result(report)


def _available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    try:
        from robie_job_engine import ezlynx_policy_setup  # noqa: F401
        return True
    except ImportError:
        return False


registry.register(
    name="ezlynx_policy_setup",
    toolset="ezlynx",
    schema=POLICY_SETUP_SCHEMA,
    handler=ezlynx_policy_setup_handler,
    check_fn=_available,
    emoji="🏠",
)
