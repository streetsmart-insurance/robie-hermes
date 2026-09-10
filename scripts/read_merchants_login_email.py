import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)

async def check():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        
        # Find gmail page
        page = None
        for p_item in ctx.pages:
            if "mail.google.com" in p_item.url:
                page = p_item
                break
                
        if not page:
            page = await ctx.new_page()
            await page.goto("https://mail.google.com/mail/u/0/#inbox", wait_until="domcontentloaded")
            await asyncio.sleep(3)
        else:
            await page.bring_to_front()
            
        print("Gmail page found:", page.url)
        
        # Click on the email with text "MerchantsGroup.com: your user has been added successfully"
        target_span = page.locator('span:has-text("MerchantsGroup.com: your user has been added successfully")').first
        if await target_span.is_visible():
            print("Found target span, clicking...")
            await target_span.click()
            await asyncio.sleep(4)
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/merchants_email_opened.png")
            
            # Extract email text and links
            email_data = await page.evaluate('''() => {
                const body = document.querySelector('.a3s.aiL') || document.body;
                const links = Array.from(body.querySelectorAll('a')).map(a => ({ text: a.innerText, href: a.href }));
                return {
                    text: body.innerText,
                    links: links
                };
            }''')
            
            print("--- EMAIL TEXT ---")
            print(email_data['text'])
            print("--- LINKS ---")
            for l in email_data['links']:
                print(l)
        else:
            print("Target span not visible, trying search...")

if __name__ == "__main__":
    asyncio.run(check())
