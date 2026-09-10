import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = None
        for p_item in ctx.pages:
            if "accounts.google.com" in p_item.url:
                page = p_item
                break
                
        if not page:
            print("No accounts page found, creating/using tab...")
            return
            
        print("Using tab:", page.url)
        await page.goto("https://accounts.google.com/AddSession?service=mail", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        
        email_inp = page.locator('#identifierId, input[type="email"], input[name="identifier"]').first
        if await email_inp.is_visible():
            await email_inp.fill("robie@streetsmart.insurance")
            next_btn = page.locator('#identifierNext, button:has-text("Next")').first
            await next_btn.click()
            await asyncio.sleep(3)
            
            pw_inp = page.locator('input[type="password"], input[name="Passwd"], input[name="password"]').first
            if await pw_inp.is_visible():
                await pw_inp.fill("Dy5LmMgC7P5&7yU%%")
                pw_next = page.locator('#passwordNext, button:has-text("Next")').first
                await pw_next.click()
                await asyncio.sleep(5)
                
        print("Current URL:", page.url)
        print("Current Title:", await page.title())
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/current_2fa_prompt.png")
        
        # Check "Try another way" if visible
        try_another = page.locator('button:has-text("Try another way")').first
        if await try_another.is_visible():
            await try_another.click()
            await asyncio.sleep(3)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/current_2fa_all_options.png")
            
        text = await page.evaluate("() => document.body.innerText")
        print("=== 2FA SCREEN TEXT ===")
        print(text)

if __name__ == "__main__":
    asyncio.run(run())
