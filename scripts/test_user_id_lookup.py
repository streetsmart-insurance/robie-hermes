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
        await email_inp.fill("robie@streetsmart.insurance")
        
        continue_btn = page.locator('input[value="Continue"], button:has-text("Continue"), a:has-text("Continue")')
        if await continue_btn.count() == 0:
            # maybe it is an image or input
            continue_btn = page.locator('input[type="submit"], input[type="button"], a')
            
        print("Submitting email...")
        # Check form submit or continue button
        await page.evaluate('''() => {
            const form = document.forms[0];
            form.submit();
        }''')
        await page.wait_for_load_state("networkidle")
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/merchants_recovery_result.png")
        
        text = await page.evaluate('() => document.body.innerText')
        print("=== RESULT TEXT ===")
        print(text)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
