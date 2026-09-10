import asyncio
import os
import re
from playwright.async_api import async_playwright

async def run():
    print('Starting A&B Finish Carpenters renewal keying via Service -> Renew...')
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': 1600, 'height': 1000})
            print('Navigating to policies page...')
            await page.goto('https://app.ezlynx.com/web/account/144897143/policies', wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(4)
            
            # Click #policy-actions on row for 106793
            row = page.locator('mat-row:has-text("106793")')
            btn = row.locator('#policy-actions')
            print('Clicking #policy-actions button...')
            await btn.click()
            await asyncio.sleep(1)
            
            # Hover over 'Service'
            service_item = page.locator('[role="menuitem"]:has-text("Service"), button:has-text("Service")').first
            print('Hovering over Service menu item...')
            await service_item.hover()
            await asyncio.sleep(1)
            
            # Click 'Renew'
            renew_item = page.locator('[role="menuitem"]:has-text("Renew"), button:has-text("Renew")').first
            print('Clicking Renew menu item...')
            await renew_item.click()
            
            # Wait for Renew action form to load
            print('Waiting for Renew Policy form to load...')
            await page.wait_for_selector('#RenewPolicyBtn', timeout=30000)
            print('Renew form loaded! Current URL:', page.url)
            
            # Fill new renewal values
            print('Filling new renewal values (Deposit: ,304.00)...')
            await page.locator('#Premium').fill('1304.00')
            
            if await page.locator('#FullTermPremium').count() > 0:
                await page.locator('#FullTermPremium').fill('1304.00')
            if await page.locator('#AnnualPremium').count() > 0:
                await page.locator('#AnnualPremium').fill('1304.00')
            if await page.locator('#Description').count() > 0:
                await page.locator('#Description').fill('Renewal of 106793-4-25')
                
            # Check Writing Company
            writing_selectors = ['select#WritingCompanyId', 'select[name*="WritingCompany" i]', '#WritingCompany']
            for wsel in writing_selectors:
                loc = page.locator(wsel).first
                if await loc.count() > 0:
                    tag = await loc.evaluate('el => el.tagName.toLowerCase()')
                    if tag == 'select':
                        opts = await loc.evaluate('el => Array.from(el.options).map(o => ({text: o.text, val: o.value}))')
                        print('Writing company options:', opts)
                        for o in opts:
                            if 'MANUFACTURERS' in o['text'].upper() or 'CASUALTY' in o['text'].upper() or 'NJM' in o['text'].upper():
                                await loc.select_option(value=o['val'])
                                print(f'Selected writing company: {o["text"]}')
                                break

            # Ensure Producer Carlo Ferrara
            producer_selectors = ['select#ProducerId', 'select#ProducerCode', 'select[name*="Producer" i]', '#Producer']
            for psel in producer_selectors:
                loc = page.locator(psel).first
                if await loc.count() > 0:
                    tag = await loc.evaluate('el => el.tagName.toLowerCase()')
                    if tag == 'select':
                        opts = await loc.evaluate('el => Array.from(el.options).map(o => ({text: o.text, val: o.value}))')
                        print('Producer options:', opts)
                        for o in opts:
                            if 'CARLO' in o['text'].upper():
                                await loc.select_option(value=o['val'])
                                print(f'Selected producer: {o["text"]}')
                                break

            os.makedirs('data/screenshots', exist_ok=True)
            before_path = 'data/screenshots/ab_renew_form_filled.png'
            await page.screenshot(path=before_path)
            print(f'Saved before screenshot: {before_path}')
            
            # Verify #RenewPolicyBtn is present and click it
            btn = page.locator('#RenewPolicyBtn').first
            btn_text = (await btn.inner_text()).strip()
            print(f'Ready to click button: "{btn_text}"')
            if 'edit' in btn_text.lower() or 'bind' in btn_text.lower():
                raise RuntimeError(f'Refusing forbidden button: {btn_text}')
                
            await btn.click()
            print('Clicked Renew Policy button! Waiting for completion...')
            await asyncio.sleep(6)
            
            after_path = 'data/screenshots/ab_renew_submitted.png'
            await page.screenshot(path=after_path)
            print(f'Saved after screenshot: {after_path}')
            print('URL after submit:', page.url)

            # Navigate to policy summary to capture pending RWL proof
            summary_url = 'https://app.ezlynx.com/applicantportal/policy/72580795/summary/index'
            print(f'Navigating to {summary_url} to verify pending RWL...')
            await page.goto(summary_url, wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(4)
            proof_path = 'data/screenshots/ab_pending_shell_confirmed.png'
            await page.screenshot(path=proof_path)
            print(f'Saved pending shell proof screenshot: {proof_path}')
            
            body_text = await page.evaluate('() => document.body.innerText')
            has_pending = 'pending' in body_text.lower()
            has_1304 = '1304' in body_text or '1,304' in body_text
            print(f'Verification on Summary: has_pending={has_pending}, has_1304={has_1304}')

            # Also navigate to policies tab to see both terms
            policies_url = 'https://app.ezlynx.com/web/account/144897143/policies'
            print(f'Navigating to {policies_url} to capture updated policies view...')
            await page.goto(policies_url, wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(4)
            policies_proof_path = 'data/screenshots/ab_policies_after_renew.png'
            await page.screenshot(path=policies_proof_path)
            print(f'Saved policies after renew screenshot: {policies_proof_path}')

        finally:
            await page.close()

asyncio.run(run())
