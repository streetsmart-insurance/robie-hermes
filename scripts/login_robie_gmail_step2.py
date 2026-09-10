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
        email_inp = page.locator('#identifierId, input[type="email"], input[name="identifier"]').first
        print("Filling email...")
        await email_inp.click()
        await email_inp.fill("Robie@streetsmart.insurance")
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/robie_typed_email.png")
        
        # Click Next
        next_btn = page.locator('#identifierNext, button:has-text("Next")').first
        await next_btn.click()
        print("Clicked Next, waiting for password...")
        await asyncio.sleep(4)
        
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/robie_pw_page.png")
        
        pw_inp = page.locator('input[type="password"], input[name="Passwd"], input[name="password"]').first
        if await pw_inp.is_visible():
            print("Entering password...")
            await pw_inp.click()
            await pw_inp.fill("Dy5LmMgC7P5&7yU%%")
            await asyncio.sleep(1)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/robie_typed_pw.png")
            
            pw_next = page.locator('#passwordNext, button:has-text("Next")').first
            await pw_next.click()
            print("Clicked Next for password, waiting...")
            await asyncio.sleep(6)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/robie_after_login.png")
            print("Final URL:", page.url)
            print("Final Title:", await page.title())

if __name__ == "__main__":
    asyncio.run(run())
