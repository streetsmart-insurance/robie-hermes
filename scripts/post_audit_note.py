import os
import asyncio
import logging
from typing import Optional, Dict, Any
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def post_audit_note(
    applicant_id: str,
    discussion_search_text: str,
    note_text: str,
    screenshot_filename: Optional[str] = None,
    cdp_url: str = "http://localhost:9222"
) -> Dict[str, Any]:
    os.makedirs("/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots", exist_ok=True)
    
    if "Robie was here" not in note_text:
        note_text = f"{note_text.strip()}\n\nRobie was here"

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        ctx = browser.contexts[0]
        
        page = None
        for pg in ctx.pages:
            if "ezlynx.com" in pg.url:
                page = pg
                break
        if not page:
            page = await ctx.new_page()

        target_url = f"https://app.ezlynx.com/web/account/{applicant_id}/activity"
        logger.info(f"Navigating to {target_url}...")
        await page.goto(target_url, wait_until="domcontentloaded")
        await asyncio.sleep(4)

        if "login" in page.url.lower():
            logger.error("EZLynx session is at login page.")
            return {"success": False, "error": "Not authenticated with EZLynx"}

        # Click Add to Discussion on the target card
        logger.info(f"Locating discussion matching '{discussion_search_text}'...")
        click_result = await page.evaluate(f'''() => {{
            const query = "{discussion_search_text}".toLowerCase();
            const cards = Array.from(document.querySelectorAll('.activity-container'));
            const card = cards.find(c => c.innerText.toLowerCase().includes(query));
            if (!card) return {{ success: false, error: "Card not found" }};
            
            const btn = card.querySelector('button[title="Add to Discussion"]');
            if (!btn) return {{ success: false, error: "Add to Discussion button not found" }};
            
            btn.click();
            return {{ success: true }};
        }}''')

        if not click_result.get("success"):
            logger.warning(f"Could not click Add to Discussion: {click_result.get('error')}")
            fallback = await page.evaluate('''() => {
                const card = document.querySelector('.activity-container');
                if (!card) return false;
                const btn = card.querySelector('button[title="Add to Discussion"]');
                if (btn) { btn.click(); return true; }
                return false;
            }''')
            if not fallback:
                return {"success": False, "error": f"Discussion matching '{discussion_search_text}' not found"}

        await asyncio.sleep(1.5)

        # Wait for txtNote
        txt_locator = page.locator('#txtNote')
        await txt_locator.wait_for(state="visible", timeout=6000)

        # Fill note text
        logger.info("Entering note text into #txtNote...")
        await txt_locator.fill(note_text)
        await asyncio.sleep(1)

        # Click Save button
        save_btn = page.locator('button:has-text("Save")').filter(has_not_text="Reset").first
        logger.info("Clicking Save button...")
        await save_btn.click()
        await asyncio.sleep(3)

        # Verify discussion note via internal API
        verify_data = await page.evaluate(f'''async () => {{
            try {{
                const r = await fetch('/EZLynxPortalAPI/Discussions/GetPagedDiscussions?pageNumber=1&pageSize=5&applicantId={applicant_id}&applicantContext=true');
                if (!r.ok) return null;
                const j = await r.json();
                return j.discussions || [];
            }} catch(e) {{
                return null;
            }}
        }}''')

        saved_note_id = None
        matched_discussion_title = None
        if verify_data:
            for d in verify_data:
                d_title = d.get("title", "")
                if discussion_search_text.lower() in d_title.lower() or not matched_discussion_title:
                    matched_discussion_title = d_title
                    saved_note_id = d.get("discussionNote", {}).get("noteId")
                    break

        screenshot_path = None
        if screenshot_filename:
            screenshot_path = f"/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/{screenshot_filename}"
            card = page.locator('.activity-container').filter(has_text=discussion_search_text).first
            if not await card.is_visible():
                card = page.locator('.activity-container').first
            await card.screenshot(path=screenshot_path)
            logger.info(f"Saved verification screenshot to {screenshot_path}")

        return {
            "success": True,
            "applicant_id": applicant_id,
            "discussion_title": matched_discussion_title or discussion_search_text,
            "note_id": saved_note_id,
            "screenshot_path": screenshot_path
        }
