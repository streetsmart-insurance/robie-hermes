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
        
        await page.goto("https://app.ezlynx.com/applicantportal/policy/31887060/summary/index", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        
        # Check Viewing Summary As Of dropdown to see what drivers existed on earlier dates!
        # Dropdown: Viewing Summary As Of
        dates = await page.evaluate('''() => {
            const select = document.querySelector('select');
            return select ? Array.from(select.options).map(o => ({text: o.text, value: o.value})) : [];
        }''')
        print("Summary As Of Options:", dates)

        # Let's select each date and check drivers
        for opt in dates:
            val = opt['value']
            text = opt['text']
            print(f"Checking date option: {text} ({val})")
            await page.select_option("select", value=val)
            await asyncio.sleep(1.5)
            drivers_text = await page.evaluate('''() => {
                const body = document.body.innerText;
                const match = body.match(/DRIVERS[\\s\\S]*?(?=ADDITIONAL INTERESTS|GARAGE LOCATIONS|$)/);
                return match ? match[0] : 'No drivers section';
            }''')
            print(f"  Drivers for {text}:\n{drivers_text.strip()}\n")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(test())
