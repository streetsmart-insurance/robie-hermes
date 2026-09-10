import asyncio
import logging
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

async def post_discussion_note_via_cdp(
    applicant_id: str,
    discussion_title: str,
    note_text: str,
    cdp_url: str = "http://localhost:9222"
) -> bool:
    """Posts an audit note directly to an existing discussion in EZLynx via Chrome CDP."""
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            ctx = browser.contexts[0]
            
            # Find an existing EZLynx tab or open one
            page = None
            for pg in ctx.pages:
                if "ezlynx.com" in pg.url:
                    page = pg
                    break
            if not page:
                page = await ctx.new_page()

            target_url = f"https://app.ezlynx.com/web/account/{applicant_id}/activity"
            print(f"Navigating to {target_url}...")
            await page.goto(target_url, wait_until="networkidle")
            await asyncio.sleep(2)

            # Check if redirected to login
            if "login" in page.url.lower():
                print("❌ EZLynx session is at login page. Please sign in.")
                return False

            # Find discussion container
            print(f"Locating discussion container for '{discussion_title}'...")
            containers = await page.locator(".activity-container").all()
            target_container = None
            for c in containers:
                text = await c.inner_text()
                if discussion_title.lower() in text.lower():
                    target_container = c
                    break

            if not target_container:
                print(f"⚠️ Discussion '{discussion_title}' not found on first screen, searching page...")
                target_container = page.locator(".activity-container").filter(has_text=discussion_title).first

            await target_container.hover()
            await asyncio.sleep(0.5)

            # Click 'Add to Discussion'
            add_btn = target_container.locator('button[title="Add to Discussion"]').first
            if not await add_btn.is_visible():
                add_btn = target_container.locator('button:has-text("Add to Discussion"), button:has-text("Reply")').first

            print("Clicking Add to Discussion button...")
            await add_btn.click()
            await asyncio.sleep(1)

            # Fill note textarea
            note_area = page.locator('#txtNote, textarea[name="noteText"], textarea.mat-mdc-input-element').first
            await note_area.wait_for(state="visible", timeout=5000)
            
            # Ensure "Robie was here" is at the bottom
            final_note = note_text.strip()
            if "Robie was here" not in final_note:
                final_note = f"{final_note}\n\nRobie was here"

            print(f"Entering note text ({len(final_note)} chars)...")
            await note_area.fill(final_note)
            await asyncio.sleep(0.5)

            # Click Save button
            save_btn = page.locator('button:has-text("Save")').first
            print("Clicking Save button...")
            await save_btn.click()
            await asyncio.sleep(2)

            print("✅ Discussion note posted successfully!")
            return True

        except Exception as e:
            print(f"❌ Error posting discussion note: {e}")
            return False

if __name__ == "__main__":
    import sys
    test_note = (
        "Emailed quotes@trinityunderwriters.net (CC: maria@streetsmart.insurance, jake@streetsmart.insurance) at Trinity Underwriters.\n"
        "Requested upcoming renewal offer and loss runs for Policy #A23B8960-78760-SSRM NTL (Exp: 2026-10-05).\n"
        "Subject: [RENEWAL-REQ-31] Renewal & Loss Runs Request: Edwin Lema - Pol #A23B8960-78760-SSRM NTL (Exp: 10/05/2026)\n"
        "Tracking Ref: [RENEWAL-REQ-31]\n"
        "Pending renewal offer - awaiting documents back from underwriter."
    )
    asyncio.run(post_discussion_note_via_cdp("145217363", "Commercial Auto Manual Renewal", test_note))
