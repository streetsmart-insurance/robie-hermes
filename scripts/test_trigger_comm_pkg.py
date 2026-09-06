import asyncio
from playwright.async_api import async_playwright

sid = "50e461126ea5412fa87289ab06cd3469"

async def check():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        await page.goto(f"https://www.loom.com/share/{sid}")
        await asyncio.sleep(4)
        
        # Click more actions
        more_btn = await page.wait_for_selector("[data-testid='toggleActions'], button:has-text('More actions')")
        await more_btn.click()
        await asyncio.sleep(1.5)
        
        # Click Download video in menu
        dl_item = await page.wait_for_selector("button:has-text('Download video')")
        await dl_item.click()
        await asyncio.sleep(2)
        
        # Click the submit download button inside the modal dialog
        clicked = await page.evaluate('''() => {
            const btns = Array.from(document.querySelectorAll("button"));
            // Find button that contains "Download video" and is not the menu item
            const modalBtns = btns.filter(b => b.innerText.trim() === "Download video" || b.innerText.trim() === "Downloading...");
            if (modalBtns.length > 0) {
                const target = modalBtns[modalBtns.length - 1];
                target.click();
                return { success: true, text: target.innerText };
            }
            return { success: false, found: btns.map(b => b.innerText.trim()).filter(Boolean) };
        }''')
        print("Click result:", clicked)
        await asyncio.sleep(3)
        
        # Check text after click
        status = await page.evaluate('''() => {
            const btns = Array.from(document.querySelectorAll("button"));
            return btns.map(b => b.innerText.trim()).filter(t => t.includes("Download") || t.includes("720p"));
        }''')
        print("Status after click:", status)
        
        await page.close()

if __name__ == "__main__":
    asyncio.run(check())
