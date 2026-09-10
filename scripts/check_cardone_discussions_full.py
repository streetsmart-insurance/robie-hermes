import asyncio
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
sys.path.append('/Users/carloferrara/.gemini/antigravity/scratch/renewal-automation-system')
from playwright.async_api import async_playwright
from src.ezlynx.session_manager import EZLynxSessionManager

async def test():
    mgr = EZLynxSessionManager(cdp_url=None)
    async with async_playwright() as p:
        browser, ctx = await mgr.get_authenticated_context(p, headless=True)
        page = await ctx.new_page()
        
        await page.goto("https://app.ezlynx.com/web/account/51287231/activity", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        
        # Get discussions
        disc_res = await page.evaluate('''async () => {
            const r = await fetch('/EZLynxPortalAPI/Discussions/GetPagedDiscussions?pageNumber=1&pageSize=20&applicantId=51287231&applicantContext=true');
            return r.ok ? await r.json() : null;
        }''')
        
        print("--- CARDONE DISCUSSIONS ---")
        if disc_res and "discussions" in disc_res:
            for d in disc_res["discussions"]:
                print(f"ID: {d.get('discussionId')}, Title: {d.get('title')}, Created: {d.get('created')}, LastMod: {d.get('lastModified')}")
                note = d.get('discussionNote', {})
                print(f"  Note: {note.get('note')}")
                print(f"  ActivityComment: {note.get('noteActivityComment')}")
                print(f"  Labels: {[l.get('labelName') for l in note.get('noteLabels', [])]}")
                print(f"  Tasks: {note.get('task')}")
                print("---")
                
        # Also get all notes on the policy itself
        # policyMasterId: 31887060
        pol_notes = await page.evaluate('''async () => {
            const r = await fetch('/EZLynxPortalAPI/Discussions/GetPagedDiscussions?pageNumber=1&pageSize=20&applicantId=51287231&policyMasterId=31887060');
            return r.ok ? await r.json() : null;
        }''')
        print("--- CARDONE POLICY DISCUSSIONS ---")
        if pol_notes and "discussions" in pol_notes:
            for d in pol_notes["discussions"]:
                print(f"ID: {d.get('discussionId')}, Title: {d.get('title')}")
                print(f"  Note: {d.get('discussionNote', {}).get('note')}")
                print("---")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test())
