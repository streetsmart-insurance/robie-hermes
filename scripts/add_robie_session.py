import asyncio
from playwright.async_api import async_playwright

async def s():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        await page.goto("https://accounts.google.com/AddSession?service=mail&continue=https://mail.google.com/mail/u/", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        print("AddSession URL:", page.url)
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/add_session_1.png")
        
        email_inp = page.locator('input[type="email"]')
        if await email_inp.is_visible():
            print("Entering email...")
            await email_inp.fill("Robie@streetsmart.insurance")
            await page.keyboard.press("Enter")
            await asyncio.sleep(4)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/add_session_2.png")
            
            pw_inp = page.locator('input[type="password"]')
            if await pw_inp.is_visible():
                print("Entering password...")
                await pw_inp.fill("Dy5LmMgC7P5&7yU%%")
                await page.keyboard.press("Enter")
                await asyncio.sleep(5)
                await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/add_session_3.png")
                print("Final URL:", page.url)

if __name__ == "__main__":
    asyncio.run(s())
