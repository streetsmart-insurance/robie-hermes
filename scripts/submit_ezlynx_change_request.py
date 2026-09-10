import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        
        target_page = None
        for page in ctx.pages:
            if 'ChangeRequest' in page.url:
                target_page = page
                break
                
        if not target_page:
            print("ERROR: ChangeRequest page not found in tabs!")
            return
            
        print("Found ChangeRequest page:", target_page.url)
        
        # Verify fields
        change_date = await target_page.input_value('#ChangeDate')
        print(f"Current ChangeDate: {change_date}")
        
        desc_text = "Add 2005 Ford Econoline E350 Super Duty Wagon VIN: 1FBSS31L05HB40186 - Liability Only - Effective 09/08/2026 - Submitted on Carrier Portal (Pending Download)"
        print(f"Filling Description: {desc_text}")
        await target_page.fill('#Description', desc_text)
        
        # Screenshot before submit
        await target_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/ezlynx_cr_filled.png')
        print("Saved ezlynx_cr_filled.png")
        
        # Click ChangeRequestPolicyBtn
        print("Clicking #ChangeRequestPolicyBtn...")
        await target_page.click('#ChangeRequestPolicyBtn')
        
        # Wait for navigation or postback
        await asyncio.sleep(6)
        
        print("Landed URL after submit:", target_page.url)
        print("Title:", await target_page.title())
        
        await target_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/ezlynx_cr_submitted.png')
        print("Saved ezlynx_cr_submitted.png")
        
        # Check text
        text = await target_page.evaluate('() => document.body.innerText')
        for l in text.split('\n')[:50]:
            if l.strip():
                print("  ", l.strip())

asyncio.run(main())
