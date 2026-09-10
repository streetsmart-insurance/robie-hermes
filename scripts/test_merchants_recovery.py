import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        url = "https://secure.merchantsgroup.com/cgi-bin/lansaweb?procfun+migusrproc+usrreco+mig+eng"
        print("Navigating to recovery:", url)
        await page.goto(url, wait_until="networkidle")
        print("URL:", page.url)
        print("Title:", await page.title())
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/merchants_recovery.png")
        
        text = await page.evaluate('() => document.body.innerText')
        print("=== RECOVERY PAGE TEXT ===")
        print(text)
        
        inputs = await page.evaluate('''() => {
            return Array.from(document.querySelectorAll('input, select')).map(i => ({
                id: i.id,
                name: i.name,
                type: i.type,
                placeholder: i.placeholder,
                value: i.value
            }));
        }''')
        print("Inputs:", inputs)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
