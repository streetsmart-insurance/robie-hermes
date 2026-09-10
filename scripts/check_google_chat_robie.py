import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = None
        for p_item in ctx.pages:
            if "chat.google.com" in p_item.url:
                page = p_item
                break
        if not page:
            return
            
        robie_conv = page.locator('span:text-is("Robie")').first
        if await robie_conv.is_visible():
            await robie_conv.click()
            await asyncio.sleep(2)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/google_chat_robie.png")
            text = await page.evaluate("() => document.body.innerText")
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            print("Chat lines with Robie:")
            for l in lines[-30:]:
                print(l)

if __name__ == "__main__":
    asyncio.run(run())
