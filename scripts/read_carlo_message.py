import asyncio
from playwright.async_api import async_playwright

async def check():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        # Target the page with the email open
        page = None
        for p_item in ctx.pages:
            if "your user has been added" in p_item.url or "mail.google.com" in p_item.url:
                page = p_item
                break
                
        # Click on Carlo Ferrara's header to expand it
        carlo_header = page.locator('span:has-text("Carlo Ferrara")').last
        if await carlo_header.count() > 0:
            print("Found Carlo header, clicking...")
            await carlo_header.click()
            await asyncio.sleep(2)
            
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/carlo_message_expanded.png")
        
        # Dump all text inside the right pane
        right_pane_text = await page.evaluate('''() => {
            const pane = document.querySelector('div[role="main"]') || document.body;
            return pane.innerText;
        }''')
        print("=== EXPANDED THREAD TEXT ===")
        print(right_pane_text)

if __name__ == "__main__":
    asyncio.run(check())
