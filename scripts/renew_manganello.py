import asyncio
import os
from playwright.async_api import async_playwright

async def run():
    print('Starting Michelle Manganello renewal keying via Service -> Renew...')
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': 1600, 'height': 1000})
            print('Navigating to Manganello policies page...')
            await page.goto('https://app.ezlynx.com/web/account/143979332/policies', wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(4)
            
            # Find action button for TNF4012536 card
            card = page.locator("div, mat-card").filter(has_text="TNF4012536").first
            btn = card.locator("button[aria-label*='action' i], button[id*='action' i], mat-icon:has-text('more_vert')").first
            print('Clicking action button...')
            await btn.click()
            await asyncio.sleep(1)
            
            # Hover over 'Service'
            service_item = page.locator('[role="menuitem"]:has-text("Service"), button:has-text("Service"), .mat-mdc-menu-item:has-text("Service")').first
            print('Hovering over Service menu item...')
            await service_item.hover()
            await asyncio.sleep(1)
            
            # Click 'Renew'
            renew_item = page.locator('[role="menuitem"]:has-text("Renew"), button:has-text("Renew"), .mat-mdc-menu-item:has-text("Renew")').first
            print('Clicking Renew menu item...')
            await renew_item.click()
            
            # Wait for Renew action form to load
            print('Waiting for Renew Policy form to load...')
            await page.wait_for_selector('#RenewPolicyBtn', timeout=30000)
            print('Renew form loaded! Current URL:', page.url)
            
            # Fill renewal premium
            print('Filling renewal premium $1,982.00...')
            if await page.locator('#Premium').count() > 0:
                await page.locator('#Premium').fill('1982.00')
            if await page.locator('#FullTermPremium').count() > 0:
                await page.locator('#FullTermPremium').fill('1982.00')
            if await page.locator('#AnnualPremium').count() > 0:
                await page.locator('#AnnualPremium').fill('1982.00')
            if await page.locator('#Description').count() > 0:
                await page.locator('#Description').fill('Renewal of TNF4012536')
                
            os.makedirs('/opt/renewal-automation-system/data/screenshots', exist_ok=True)
            before_path = '/opt/renewal-automation-system/data/screenshots/manganello_renew_form_filled.png'
            await page.screenshot(path=before_path)
            print(f'Saved before screenshot: {before_path}')
            
            # Verify #RenewPolicyBtn is present and click it
            renew_btn = page.locator('#RenewPolicyBtn').first
            btn_text = (await renew_btn.inner_text()).strip()
            print(f'Ready to click button: "{btn_text}"')
            if 'edit' in btn_text.lower() or 'bind' in btn_text.lower():
                raise RuntimeError(f'Refusing forbidden button: {btn_text}')
                
            await renew_btn.click()
            print('Clicked Renew Policy button! Waiting for completion...')
            await asyncio.sleep(6)
            
            after_path = '/opt/renewal-automation-system/data/screenshots/manganello_renew_submitted.png'
            await page.screenshot(path=after_path)
            print(f'Saved after screenshot: {after_path}')
            print('URL after submit:', page.url)

            # Return to policies tab to capture updated policies view
            policies_url = 'https://app.ezlynx.com/web/account/143979332/policies'
            print(f'Navigating to {policies_url} to capture updated policies view...')
            await page.goto(policies_url, wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(4)
            policies_proof_path = '/opt/renewal-automation-system/data/screenshots/manganello_policies_after_renew.png'
            await page.screenshot(path=policies_proof_path)
            print(f'Saved policies after renew screenshot: {policies_proof_path}')

        finally:
            await page.close()

if __name__ == '__main__':
    asyncio.run(run())
