import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = None
        for p_item in ctx.pages:
            if "mail.google.com" in p_item.url:
                page = p_item
                break
                
        # Find the row containing 6:43 PM
        row = page.locator('tr:has-text("6:43")').first
        if await row.count() == 0:
            row = page.locator('div[role="row"]:has-text("6:43")').first
            
        print("Clicking row with 6:43 PM...")
        await row.click(force=True)
        await asyncio.sleep(4)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/jake_row_opened.png")
        
        # Read the message pane
        text = await page.evaluate('''() => {
            const pane = document.querySelector('div[role="main"]') || document.body;
            return pane.innerText;
        }''')
        print("=== JAKE EMAIL CONTENT ===")
        print(text)

if __name__ == "__main__":
    asyncio.run(run())
