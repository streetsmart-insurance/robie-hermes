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


async def add_discussion_note() -> None:
    applicant_id = require_allowed_ezlynx_write_applicant(APPLICANT_ID)
    print("[SIMULATION] Adding Discussion Note to EZLynx account...")
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        page = browser.contexts[0].pages[0]

        scope_block = ezlynx_control_scope_block_reason(
            page.url, requested_applicant_id=applicant_id
        )
        if scope_block:
            raise RuntimeError(scope_block)

        # Click Discussions / Notes button or tab
        # In EZLynx, the top bar has #add-note-header or we can navigate to /discussions
        add_note_btn = page.locator("#add-note-header, [data-testid='add-note-header']")
        if await add_note_btn.count() > 0:
            await add_note_btn.click()
            await page.wait_for_timeout(1000)

            # Title input
            title_input = page.locator("#txtDiscussionTitle, [name='discussionTitle']")
            if await title_input.count() > 0:
                await title_input.fill("New Policy - Commercial Auto")

            # Note body
            body_input = page.locator("#txtDiscussionBody, textarea.k-editor-textarea, [name='discussionBody'], .note-editor textarea, [contenteditable='true']")
            if await body_input.count() > 0:
                await body_input.first.fill("Completed Commercial Auto policy setup for Policy #CA-ROBIE-CLOUD-01 with Progressive Commercial.\n\nROBIE was here")

            # Save note
            save_note_btn = page.locator("#btnSaveNote, [data-testid='save-note-btn'], button:has-text('Save Note'), button:has-text('Save')")
            if await save_note_btn.count() > 0:
                await save_note_btn.first.click()
                await page.wait_for_timeout(3000)
                print("Discussion note saved successfully!")

        await page.screenshot(path="/tmp/robie_live_test/step17_discussion_note_added.png")
        print("Step 17 screenshot saved.")


if __name__ == "__main__":
    asyncio.run(add_discussion_note())
