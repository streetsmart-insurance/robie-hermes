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

        # Open discussion note
        add_note_btn = page.locator("#add-note-header, button:has-text('note_add'), a:has-text('Add Note')")
        if await add_note_btn.count() > 0:
            await add_note_btn.first.click()
            await page.wait_for_timeout(1000)

            title_input = page.locator("#txtDiscussionTitle, input[name='txtDiscussionTitle']")
            if await title_input.count() > 0:
                await title_input.first.fill("Commercial Auto - Schedules Complete")

            note_input = page.locator("#txtNote, textarea[name='txtNote'], #DiscussionNotes")
            if await note_input.count() > 0:
                await note_input.first.fill("Completed Commercial Auto policy schedules (Vehicle + Driver) for Policy #CA-ROBIE-LIVE-02.\n\nROBIE was here")

            save_note = page.locator("#btnSaveNote, button:has-text('Save')")
            if await save_note.count() > 0:
                await save_note.first.click()
                print("Discussion note logged successfully.")
                await page.wait_for_timeout(2000)


if __name__ == "__main__":
    asyncio.run(log_final_note())
