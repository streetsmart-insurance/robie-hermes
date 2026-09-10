import asyncio
import logging
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)

async def check():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        
        page = None
        for p_item in ctx.pages:
            if "mail.google.com" in p_item.url:
                page = p_item
                break
                
        print("Gmail page:", page.url)
        
        # Click row containing 'your user has been added successfully'
        clicked = await page.evaluate('''() => {
            const elements = Array.from(document.querySelectorAll('*'));
            for (const el of elements) {
                if (el.innerText && el.innerText.includes('your user has been added successfully')) {
                    // find closest clickable row or container
                    const row = el.closest('tr') || el.closest('[role="row"]') || el;
                    row.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                    return true;
                }
            }
            return false;
        }''')
        print("Clicked via evaluate:", clicked)
        await asyncio.sleep(4)
        
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/merchants_email_opened.png")
        
        # Extract email body
        body_text = await page.evaluate('''() => {
            const b = document.querySelector('.a3s.aiL') || document.querySelector('div[role="main"]');
            return b ? b.innerText : document.body.innerText;
        }''')
        
        links = await page.evaluate('''() => {
            const body = document.querySelector('.a3s.aiL') || document.querySelector('div[role="main"]') || document.body;
            return Array.from(body.querySelectorAll('a')).map(a => ({ text: a.innerText.trim(), href: a.href }));
        }''')
        
        print("--- EMAIL BODY ---")
        print(body_text)
        print("--- LINKS ---")
        for l in links:
            print(l)

if __name__ == "__main__":
    asyncio.run(check())
