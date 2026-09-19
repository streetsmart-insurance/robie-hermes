import asyncio
import os
import sys

from playwright.async_api import async_playwright

from robie_job_engine.ezlynx_write_scope import (
    ezlynx_control_scope_block_reason,
    require_allowed_ezlynx_write_applicant,
)

CDP_URL = os.environ.get("ROBIE_PLAYWRIGHT_CDP_URL", "http://127.0.0.1:9222")
APPLICANT_ID = "220250093"


async def log_final_note() -> None:
    applicant_id = require_allowed_ezlynx_write_applicant(APPLICANT_ID)
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0]

        await page.goto(f"https://app.ezlynx.com/web/account/{applicant_id}/policies", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        scope_block = ezlynx_control_scope_block_reason(
            page.url, requested_applicant_id=applicant_id
        )
        if scope_block:
            raise RuntimeError(scope_block)

        # EZLynx notes are API-only (Carlo 2026-09-19). Playwright must not file.
        from robie_job_engine.ezlynx_api_only_writes import refuse_playwright_note_or_doc

        refuse_playwright_note_or_doc("scripts/run_complete_schedules_setup discussion note")


if __name__ == "__main__":
    asyncio.run(log_final_note())
