import asyncio
from playwright.async_api import async_playwright

async def run():
    print('Testing MGA Portal (JJIns Agency Login)...')
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': 1600, 'height': 1000})
            print('Navigating to https://agent.mga-portal.com/home ...')
            await page.goto('https://agent.mga-portal.com/home', wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(4)
            
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/jjins_mga_portal.png')
            print('Current URL:', page.url)
            print('Title:', await page.title())
            
            # Print form inputs
            inputs = await page.locator('input').all()
            for inp in inputs:
                id_attr = await inp.get_attribute('id') or ''
                name_attr = await inp.get_attribute('name') or ''
                type_attr = await inp.get_attribute('type') or ''
                placeholder = await inp.get_attribute('placeholder') or ''
                print(f'Input: type={type_attr}, id={id_attr}, name={name_attr}, placeholder={placeholder}')
                
        finally:
            await page.close()

if __name__ == '__main__':
    asyncio.run(run())
