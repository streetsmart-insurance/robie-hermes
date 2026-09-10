import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = None
        for p_item in ctx.pages:
            if "AddSession" in p_item.url or "accounts.google.com/v3/signin" in p_item.url:
                page = p_item
                break
                
        if not page:
            print("No AddSession page found!")
            return
            
        print("Using page:", page.url)
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(2)
        
        # Take screenshot of signin page
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/robie_signin_step1.png")
        
        email_inp = page.locator('input[type="email"]')
        if await email_inp.is_visible():
            print("Entering email Robie@streetsmart.insurance...")
            await email_inp.fill("Robie@streetsmart.insurance")
            await page.keyboard.press("Enter")
            await asyncio.sleep(4)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/robie_signin_step2.png")
            
            pw_inp = page.locator('input[type="password"]')
            if await pw_inp.is_visible():
                print("Entering password...")
                await pw_inp.fill("Dy5LmMgC7P5&7yU%%")
                await page.keyboard.press("Enter")
                await asyncio.sleep(6)
                await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/robie_signin_step3.png")
                print("Final URL:", page.url)
                print("Final Title:", await page.title())

if __name__ == "__main__":
    asyncio.run(run())
