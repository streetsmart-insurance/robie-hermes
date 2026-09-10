import asyncio
import os
from playwright.async_api import async_playwright

async def run():
    print('Starting fast proof capture with domcontentloaded...')
    os.makedirs('data/screenshots', exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': 1600, 'height': 1000})
            
            # 1. Check history with 74053846
            h_url = 'https://app.ezlynx.com/applicantportal/policy/74053846/history/index'
            print(f'Navigating to {h_url}...')
            await page.goto(h_url, wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(4)
            await page.screenshot(path='data/screenshots/ab_policy_history_74053846.png')
            print('Saved ab_policy_history_74053846.png')
            
            # Also check summary
            s_url = 'https://app.ezlynx.com/applicantportal/policy/74053846/summary/index'
            print(f'Navigating to {s_url}...')
            await page.goto(s_url, wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(4)
            await page.screenshot(path='data/screenshots/ab_policy_summary_74053846.png')
            print('Saved ab_policy_summary_74053846.png')

            # 2. Activity / Discussion Note
            a_url = 'https://app.ezlynx.com/web/account/144897143/activity'
            print(f'Navigating to {a_url}...')
            await page.goto(a_url, wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(4)
            card = page.locator('.activity-container:has-text("Renewal Manual Workers comp")').first
            if await card.count() > 0:
                print('Found discussion card, scrolling to it...')
                await card.scroll_into_view_if_needed()
                await asyncio.sleep(1)
            await page.screenshot(path='data/screenshots/ab_discussion_note_proof.png')
            print('Saved ab_discussion_note_proof.png')

            # 3. Documents
            d_url = 'https://app.ezlynx.com/web/account/144897143/documents'
            print(f'Navigating to {d_url}...')
            await page.goto(d_url, wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(4)
            folder = page.locator('text="Renewal Offers/Declarations"').first
            if await folder.count() > 0:
                print('Opening Renewal Offers folder...')
                await folder.click()
                await asyncio.sleep(2)
            await page.screenshot(path='data/screenshots/ab_docs_folder_proof.png')
            print('Saved ab_docs_folder_proof.png')

        finally:
            await page.close()

asyncio.run(run())
