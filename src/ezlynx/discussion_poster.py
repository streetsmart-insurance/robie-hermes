"""Automated poster for EZLynx Discussion Notes with screenshot verification."""

import os
import asyncio
import logging
from typing import Optional, Dict, Any
from playwright.async_api import async_playwright, Page

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
                except Exception as cdp_err:
                    logger.info(f"CDP connection ({self.cdp_url}) unavailable ({cdp_err}). Using EZLynxSessionManager...")
                    from src.ezlynx.session_manager import EZLynxSessionManager
                    mgr = EZLynxSessionManager()
                    browser, ctx = await mgr.get_authenticated_context(p, headless=True)
                
                # Re-use existing EZLynx tab or create new
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
                await asyncio.sleep(3)

                # Check if session is logged in
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
                    # Fallback to general note_add or top card
                    fallback = await page.evaluate('''() => {
                        const card = document.querySelector('.activity-container');
                        if (!card) return false;
                        const btn = card.querySelector('button[title="Add to Discussion"]');
                        if (btn) { btn.click(); return true; }
                        return false;
                    }''')
                    if not fallback:
                        return {"success": False, "error": f"Discussion matching '{discussion_search_text}' not found"}

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
                if verify_data:
                    for d in verify_data:
                        d_title = d.get("title", "")
                        if discussion_search_text.lower() in d_title.lower() or not matched_discussion_title:
                            matched_discussion_title = d_title
                            saved_note_id = d.get("discussionNote", {}).get("noteId")
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
            finally:
                if page:
                    try:
                        if ctx and len(ctx.pages) > 1:
                            await page.close()
                        else:
                            await page.goto("about:blank", timeout=5000)
                    except Exception:
                        pass

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

    async def create_task_async(
        self,
        applicant_id: str,
        discussion_search_text: str,
        title: str,
        note_body: str,
        assigned_user: Optional[str] = None,
        due_date: Optional[str] = None,
        high_priority: bool = True,
        screenshot_filename: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Navigates to applicant activity page, opens target discussion card,
        expands Add Task drawer, configures priority, assignee, due date, fills
        note body, and clicks Save Note.
        """
        if "Robie was here" not in note_body:
            note_body = f"{note_body.strip()}\n\nRobie was here"

        async with async_playwright() as p:
            try:
                browser = None
                ctx = None
                try:
                    browser = await p.chromium.connect_over_cdp(self.cdp_url)
                    ctx = browser.contexts[0]
                except Exception as cdp_err:
                    logger.info(f"CDP connection ({self.cdp_url}) unavailable ({cdp_err}). Using EZLynxSessionManager...")
                    from src.ezlynx.session_manager import EZLynxSessionManager
                    mgr = EZLynxSessionManager()
                    browser, ctx = await mgr.get_authenticated_context(p, headless=True)

                page = None
                for pg in ctx.pages:
                    if "ezlynx.com" in pg.url:
                        page = pg
                        break
                if not page:
                    page = await ctx.new_page()

                target_url = f"https://app.ezlynx.com/web/account/{applicant_id}/activity"
                logger.info(f"Navigating to {target_url} for task creation...")
                await page.goto(target_url, wait_until="domcontentloaded")
                await asyncio.sleep(3)

                if "login" in page.url.lower():
                    logger.error("EZLynx session is at login page.")
                    return {"success": False, "error": "Not authenticated with EZLynx"}

                # Click Add to Discussion on the target card via JS evaluation
                logger.info(f"Locating discussion matching '{discussion_search_text}'...")
                click_result = await page.evaluate(f'''() => {{
                    const query = "{discussion_search_text}".toLowerCase();
                    const cards = Array.from(document.querySelectorAll('.activity-container'));
                    const card = cards.find(c => c.innerText.toLowerCase().includes(query)) || cards[0];
                    if (!card) return {{ success: false, error: "No activity card found" }};
                    
                    const btn = card.querySelector('button[title="Add to Discussion"]');
                    if (!btn) return {{ success: false, error: "Add to Discussion button not found" }};
                    
                    btn.click();
                    return {{ success: true }};
                }}''')

                if not click_result.get("success"):
                    # Fallback to header Add Note button
                    await page.evaluate('''() => {
                        const btn = document.querySelector('#add-note-header');
                        if (btn) btn.click();
                    }''')

                await asyncio.sleep(1.5)

                # Ensure task fields are visible
                if not await page.locator('#taskDueDate').is_visible():
                    logger.info("Clicking #btnAddtask to reveal task fields...")
                    add_task_btn = page.locator('#btnAddtask')
                    if await add_task_btn.count() > 0:
                        await add_task_btn.evaluate('b => b.click()')
                        try:
                            await page.wait_for_selector('#taskDueDate', timeout=8000)
                        except Exception:
                            logger.warning("Timed out waiting for #taskDueDate after clicking #btnAddtask")

                # Toggle High Priority if requested
                if high_priority:
                    prio = page.locator('#btnPriority, mat-icon:has-text("priority_high"), button:has-text("!")').first
                    if await prio.count() > 0:
                        await prio.click()
                        logger.info("Toggled high priority (!)")

                # Set Assignee if provided
                if assigned_user:
                    assignee_input = page.locator('mat-form-field:has-text("Assign this task") input, input[placeholder*="Assign" i]').first
                    if await assignee_input.count() > 0:
                        await assignee_input.click()
                        await assignee_input.fill('')
                        first_name = assigned_user.split()[0]
                        await assignee_input.type(first_name, delay=100)
                        await asyncio.sleep(1)
                        opt = page.locator(f'mat-option:has-text("{first_name}")').first
                        if await opt.count() > 0:
                            await opt.click()
                            logger.info(f"Assigned task to '{assigned_user}'")
                        else:
                            logger.warning(f"Option for '{assigned_user}' not found in dropdown")

                # Set Due Date if provided
                if due_date:
                    due_input = page.locator('#taskDueDate')
                    if await due_input.count() > 0:
                        await due_input.fill(due_date)
                        logger.info(f"Set task due date to {due_date}")

                # Enter note body
                txt_locator = page.locator('#txtNote')
                await txt_locator.wait_for(state="visible", timeout=6000)
                await txt_locator.fill(note_body)
                await asyncio.sleep(0.5)

                # Save Note & Task
                save_btn = page.locator('#btnSaveNote, button:has-text("Save")').filter(has_not_text="Reset").first
                await save_btn.click()
                logger.info("Clicked Save button for task creation...")
                await asyncio.sleep(4)

                screenshot_path = None
                if screenshot_filename:
                    screenshot_path = os.path.join(self.screenshot_dir, screenshot_filename)
                    await page.screenshot(path=screenshot_path)
                    logger.info(f"Saved task creation screenshot to {screenshot_path}")

                return {
                    "success": True,
                    "applicant_id": applicant_id,
                    "discussion_title": discussion_search_text,
                    "assigned_user": assigned_user,
                    "due_date": due_date,
                    "screenshot_path": screenshot_path
                }

            except Exception as e:
                logger.exception(f"Exception during EZLynx task creation: {e}")
                return {"success": False, "error": str(e)}
            finally:
                if page:
                    try:
                        if ctx and len(ctx.pages) > 1:
                            await page.close()
                        else:
                            await page.goto("about:blank", timeout=5000)
                    except Exception:
                        pass

    def create_task(
        self,
        applicant_id: str,
        discussion_search_text: str,
        title: str,
        note_body: str,
        assigned_user: Optional[str] = None,
        due_date: Optional[str] = None,
        high_priority: bool = True,
        screenshot_filename: Optional[str] = None
    ) -> Dict[str, Any]:
        """Synchronous wrapper for create_task_async."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import nest_asyncio
                nest_asyncio.apply()
                return loop.run_until_complete(
                    self.create_task_async(
                        applicant_id=applicant_id,
                        discussion_search_text=discussion_search_text,
                        title=title,
                        note_body=note_body,
                        assigned_user=assigned_user,
                        due_date=due_date,
                        high_priority=high_priority,
                        screenshot_filename=screenshot_filename
                    )
                )
            return loop.run_until_complete(
                self.create_task_async(
                    applicant_id=applicant_id,
                    discussion_search_text=discussion_search_text,
                    title=title,
                    note_body=note_body,
                    assigned_user=assigned_user,
                    due_date=due_date,
                    high_priority=high_priority,
                    screenshot_filename=screenshot_filename
                )
            )
        except RuntimeError:
            return asyncio.run(
                self.create_task_async(
                    applicant_id=applicant_id,
                    discussion_search_text=discussion_search_text,
                    title=title,
                    note_body=note_body,
                    assigned_user=assigned_user,
                    due_date=due_date,
                    high_priority=high_priority,
                    screenshot_filename=screenshot_filename
                )
            )

