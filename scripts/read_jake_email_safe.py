import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        # find gmail page
        gmail_page = None
        for page in ctx.pages:
            if "mail.google.com" in page.url:
                gmail_page = page
                break
                
        if not gmail_page:
            print("No gmail page found!")
            return
            
        print("Using gmail page:", gmail_page.url)
        
        # Navigate directly to the thread for Jake's email if possible, or search
        # Let's search for "from:Jake Merchants"
        await gmail_page.goto("https://mail.google.com/mail/u/0/#search/from%3Ajake+merchantsgroup", wait_until="domcontentloaded")
        await asyncio.sleep(4)
        await gmail_page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/jake_search_screen.png")
        
        # Click on the first row in the search
        await gmail_page.evaluate('''() => {
            const rows = document.querySelectorAll('tr[role="row"], .zA');
            if (rows.length > 0) {
                rows[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
            }
        }''')
        await asyncio.sleep(3)
        await gmail_page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/jake_email_view.png")
        
        text = await gmail_page.evaluate('''() => {
            const el = document.querySelector('div[role="main"]');
            return el ? el.innerText : document.body.innerText;
        }''')
        print("=== JAKE EMAIL VIEW TEXT ===")
        print(text)

if __name__ == "__main__":
    asyncio.run(run())
