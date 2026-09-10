import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        nat_page = None
        for page in ctx.pages:
            if 'natgenagency.com' in page.url:
                nat_page = page
                break
        if nat_page:
            vin = "1FBSS31L05HB40186"
            print(f"Filling VIN: {vin}")
            vin_input = nat_page.locator("#ctl00_MainContent_ucInsuredAuto_txtVIN")
            await vin_input.fill(vin)
            await asyncio.sleep(1)
            
            # Click verify VIN button
            verify_btn = nat_page.locator("#ctl00_MainContent_ucInsuredAuto_btnVerifyVIN")
            print("Clicking btnVerifyVIN...")
            await verify_btn.click()
            await asyncio.sleep(5)
            
            # Inspect form values
            inputs = await nat_page.evaluate("""() => {
                return Array.from(document.querySelectorAll('input, select, textarea'))
                    .filter(el => el.offsetParent !== null)
                    .map(el => ({
                        id: el.id,
                        name: el.name,
                        tagName: el.tagName,
                        value: el.value,
                        type: el.type
                    }));
            }""")
            print("--- Current Input Values after VIN Decode ---")
            for inp in inputs:
                print(inp)
                
            # Also check text
            text = await nat_page.evaluate('() => document.body.innerText')
            lines = [l.strip() for l in text.split('\n') if l.strip()]
            for l in lines[30:120]:
                print('  ', l)
                
            await nat_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/natgen_vin_decoded.png')

asyncio.run(main())
