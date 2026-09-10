import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': 1600, 'height': 1000})
            await page.goto('https://app.ezlynx.com/applicantportal/Policy/Actions/Renew/143979332/56639575', wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(3)
            
            # Inspect all inputs and selects
            inputs = await page.locator('input, select, textarea').all()
            print(f'Total input elements: {len(inputs)}')
            for inp in inputs:
                el_id = await inp.get_attribute('id')
                name = await inp.get_attribute('name')
                val = await inp.input_value() if await inp.evaluate('el => "value" in el') else ''
                vis = await inp.is_visible()
                req = await inp.get_attribute('required')
                cl = await inp.get_attribute('class')
                if vis:
                    print(f'  Input: id="{el_id}" name="{name}" val="{val}" req="{req}" class="{cl}"')
                    
            btns = await page.locator('button').all()
            for b in btns:
                txt = (await b.inner_text()).strip()
                b_id = await b.get_attribute('id')
                dis = await b.is_disabled()
                if txt:
                    print(f'  Button: id="{b_id}" text="{txt}" disabled={dis}')
        finally:
            await page.close()

asyncio.run(run())
