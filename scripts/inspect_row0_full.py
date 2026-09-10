import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = [x for x in ctx.pages if "mail.google.com" in x.url][0]
        
        # Click row 0
        rows = page.locator('tr[role="row"], .zA')
        print("Clicking row 0...")
        await rows.nth(0).click()
        await asyncio.sleep(3)
        
        # Expand all collapsed messages in the thread if any
        collapsed = page.locator('div[aria-expanded="false"]')
        count = await collapsed.count()
        print(f"Collapsed elements: {count}")
        for i in range(count):
            try:
                await collapsed.nth(i).click()
                await asyncio.sleep(0.5)
            except:
                pass
                
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/row0_expanded.png")
        
        # Extract full thread text
        thread_text = await page.evaluate('''() => {
            const main = document.querySelector('div[role="main"]') || document.body;
            return main.innerText;
        }''')
        print("=== THREAD FULL TEXT ===")
        print(thread_text)
        
        # Extract all links and their hrefs
        links = await page.evaluate('''() => {
            const main = document.querySelector('div[role="main"]') || document.body;
            return Array.from(main.querySelectorAll('a')).map(a => ({
                text: a.innerText.trim(),
                href: a.href
            }));
        }''')
        print("=== ALL LINKS IN THREAD ===")
        for l in links:
            print(l)

if __name__ == "__main__":
    asyncio.run(run())
