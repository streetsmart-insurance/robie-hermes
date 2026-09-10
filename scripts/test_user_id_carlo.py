import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        url = "https://secure.merchantsgroup.com/cgi-bin/lansaweb?procfun+migusrproc+usrreco+mig+eng"
        print("Navigating to recovery:", url)
        await page.goto(url, wait_until="networkidle")
        
        email_inp = page.locator('input[name="AM1ASUSEML"]')
        await email_inp.fill("carlo@streetsmart.insurance")
        
        print("Invoking SubmitInfo() for carlo...")
        async with page.expect_navigation():
            await page.evaluate('SubmitInfo()')
            
        text = await page.evaluate('() => document.body.innerText')
        print("=== RESULT FOR CARLO ===")
        print(text)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
