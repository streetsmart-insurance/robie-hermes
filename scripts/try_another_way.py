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
        btn = page.locator('button:has-text("Try another way")').first
        if await btn.is_visible():
            await btn.click()
            await asyncio.sleep(3)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/robie_try_another_way.png")
            text = await page.evaluate('() => document.body.innerText')
            print("=== TRY ANOTHER WAY OPTIONS ===")
            print(text)

if __name__ == "__main__":
    asyncio.run(run())
