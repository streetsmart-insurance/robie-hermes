import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://secure.merchantsgroup.com", wait_until="networkidle")
        print("URL:", page.url)
        print("Title:", await page.title())
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/merchants_login_page.png")
        
        # Check login inputs
        inputs = await page.evaluate('''() => {
            return Array.from(document.querySelectorAll('input')).map(i => ({
                id: i.id,
                name: i.name,
                type: i.type,
                placeholder: i.placeholder
            }));
        }''')
        print("Inputs:", inputs)
        
        # Check links (e.g. Register, Forgot Password, etc.)
        links = await page.evaluate('''() => {
            return Array.from(document.querySelectorAll('a')).map(a => ({
                text: a.innerText.trim(),
                href: a.href
            }));
        }''')
        print("Links:", links[:10])
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
