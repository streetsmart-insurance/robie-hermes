import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = None
        for p_item in ctx.pages:
            if "challenge" in p_item.url:
                page = p_item
                break
                
        if not page:
            print("No challenge page found!")
            return
            
        print("Found challenge page:", page.url)
        # Look for option with text (•••) •••-••09
        sms_opt = page.locator('text=••09').first
        if await sms_opt.is_visible():
            print("Clicking SMS option...")
            await sms_opt.click()
            await asyncio.sleep(3)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/robie_sms_sent.png")
            text = await page.evaluate('() => document.body.innerText')
            print("=== SMS SENT PAGE ===")
            print(text)

if __name__ == "__main__":
    asyncio.run(run())
