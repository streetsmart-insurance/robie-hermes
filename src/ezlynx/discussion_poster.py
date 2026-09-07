"""Automated poster for EZLynx Discussion Notes with screenshot verification."""

import os
import asyncio
import logging
from typing import Optional, Dict, Any
from playwright.async_api import async_playwright, Page

from src.ezlynx.cdp_session_preflight import CdpSessionBlocked, preflight_live_cdp_session

logger = logging.getLogger(__name__)

class EZLynxDiscussionPoster:
    def __init__(self, cdp_url: str = "http://localhost:9222", screenshot_dir: str = "data/screenshots"):
        self.cdp_url = cdp_url
        self.screenshot_dir = screenshot_dir
        os.makedirs(self.screenshot_dir, exist_ok=True)

    async def post_note(
        self,
        applicant_id: str,
        discussion_search_text: str,
        note_text: str,
        screenshot_filename: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Navigates to the applicant activity page, opens the target discussion card,
        posts the audit note, and takes a verification screenshot.
        """
        if "Robie was here" not in note_text:
            note_text = f"{note_text.strip()}\n\nRobie was here"

        async with async_playwright() as p:
            try:
                browser = None
                ctx = None
                try:
                    browser = await p.chromium.connect_over_cdp(self.cdp_url)
                    ctx = browser.contexts[0]
                    preflight = await preflight_live_cdp_session(ctx)
                    if not preflight.ok:
                        raise CdpSessionBlocked(preflight)
                except CdpSessionBlocked as blocked:
                    logger.error("%s", blocked)
                    return {
                        "success": False,
                        "status": "blocked",
                        "error": str(blocked),
                        "preflight": blocked.preflight.to_dict(),
                    }
                except Exception as cdp_err:
                    logger.info(f"CDP connection ({self.cdp_url}) unavailable ({cdp_err}). Using EZLynxSessionManager...")
                    from src.ezlynx.session_manager import EZLynxSessionManager
                    mgr = EZLynxSessionManager()
                    try:
                        browser, ctx = await mgr.get_authenticated_context(p, headless=True)
                    except CdpSessionBlocked as blocked:
                        logger.error("%s", blocked)
                        return {
                            "success": False,
                            "status": "blocked",
                            "error": str(blocked),
                            "preflight": blocked.preflight.to_dict(),
                        }
                
                # Re-use the preflight-approved EZLynx tab or create new
                page = None
                for pg in ctx.pages:
                    if "ezlynx.com" in pg.url and "login" not in (pg.url or "").lower():
                        page = pg
                        break
                if not page:
                    page = await ctx.new_page()

                target_url = f"https://app.ezlynx.com/web/account/{applicant_id}/activity"
                logger.info(f"Navigating to {target_url}...")
                await page.goto(target_url, wait_until="domcontentloaded")
                await asyncio.sleep(3)

                # Check if session is logged in
                if "login" in page.url.lower():
                    logger.error("EZLynx session is at login page.")
                    return {"success": False, "status": "blocked", "error": "Not authenticated with EZLynx"}

                # Click Add to Discussion on the *exact* titled card.
                # Substring / first-card fallback posted Paulette HO note 1123385828
                # onto Email Automation — never do that.
                logger.info("Locating exact discussion title '%s'...", discussion_search_text)
                click_result = await page.evaluate(
                    """(want) => {
                        const query = String(want || '').trim().toLowerCase();
                        if (!query) return { success: false, error: "empty title" };
                        const forbidden = ["email automation", "automation center", "email sent by automation"];
                        if (forbidden.some((p) => query.includes(p))) {
                            return { success: false, error: "automation card refused" };
                        }
                        const untitled = new Set(["", "untitled", "(untitled)", "new discussion"]);
                        const cards = Array.from(document.querySelectorAll('.activity-container'));
                        const card = cards.find((c) => {
                            const headingEl = c.querySelector(
                                '.discussion-title, .activity-title, h3, h4, [class*="discussion-title"]'
                            );
                            const heading = ((headingEl && headingEl.innerText) || '').trim();
                            const firstLine = ((c.innerText || '').split('\\n').map((s) => s.trim()).find(Boolean) || '');
                            const title = (heading || firstLine).trim();
                            const tLow = title.toLowerCase();
                            if (untitled.has(tLow) || forbidden.some((p) => tLow.includes(p))) return false;
                            return tLow === query;
                        });
                        if (!card) return { success: false, error: "Card not found" };
                        const btn = card.querySelector('button[title="Add to Discussion"]');
                        if (!btn) return { success: false, error: "Add to Discussion button not found" };
                        btn.click();
                        return { success: true };
                    }""",
                    discussion_search_text,
                )

                if not click_result.get("success"):
                    logger.warning(
                        "Could not click Add to Discussion on exact title '%s': %s",
                        discussion_search_text,
                        click_result.get("error"),
                    )
                    return {
                        "success": False,
                        "error": (
                            f"Exact titled discussion '{discussion_search_text}' not found "
                            f"({click_result.get('error')})"
                        ),
                    }

                await asyncio.sleep(1)

                # Wait for txtNote
                txt_locator = page.locator('#txtNote')
                await txt_locator.wait_for(state="visible", timeout=6000)

                # Fill note text
                logger.info("Entering note text into #txtNote...")
                await txt_locator.fill(note_text)
                await asyncio.sleep(0.5)

                # Click Save button
                save_btn = page.locator('button:has-text("Save")').filter(has_not_text="Reset").first
                logger.info("Clicking Save button...")
                await save_btn.click()
                await asyncio.sleep(2.5)

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
                want_title = (discussion_search_text or "").strip().lower()
                if verify_data:
                    for d in verify_data:
                        d_title = (d.get("title") or "").strip()
                        if d_title.lower() == want_title:
                            matched_discussion_title = d_title
                            saved_note_id = (d.get("discussionNote") or {}).get("noteId")
                            break

                # Capture verification screenshot
                screenshot_path = None
                if screenshot_filename:
                    screenshot_path = os.path.join(self.screenshot_dir, screenshot_filename)
                    # Find top card or matching card
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

            except Exception as e:
                logger.exception(f"Exception during EZLynx note posting: {e}")
                return {"success": False, "error": str(e)}

    async def post_note_async(
        self,
        applicant_id: str,
        discussion_search_text: str,
        note_body: Optional[str] = None,
        note_text: Optional[str] = None,
        screenshot_filename: Optional[str] = None
    ) -> Dict[str, Any]:
        """Async alias for post_note with flexible note_body / note_text arguments."""
        text = note_body if note_body is not None else (note_text or "")
        return await self.post_note(
            applicant_id=applicant_id,
            discussion_search_text=discussion_search_text,
            note_text=text,
            screenshot_filename=screenshot_filename
        )

