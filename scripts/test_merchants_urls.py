import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        url1 = "https://login.merchantsgroup.com/"
        print("Visiting URL 1:", url1)
        try:
            await page.goto(url1, wait_until="networkidle", timeout=15000)
            print("URL 1 redirected to:", page.url)
            print("URL 1 title:", await page.title())
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/merchants_login_1.png")
        except Exception as e:
            print("URL 1 error:", e)
            
        url2 = "https://secure.merchantsgroup.com/cgi-bin/lansaweb?procfun+migcomproc+mlogon+mig+eng+funcparms+z1aplchld(A0010):P"
        print("\nVisiting URL 2:", url2)
        try:
            await page.goto(url2, wait_until="networkidle", timeout=15000)
            print("URL 2 redirected to:", page.url)
            print("URL 2 title:", await page.title())
            await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/merchants_login_2.png")
            
            inputs = await page.evaluate('''() => {
                return Array.from(document.querySelectorAll('input')).map(i => ({
                    id: i.id,
                    name: i.name,
                    type: i.type,
                    placeholder: i.placeholder,
                    value: i.value
                }));
            }''')
            print("URL 2 inputs:", inputs)
            
            text = await page.evaluate('() => document.body.innerText')
            print("URL 2 text snippet:", text[:300].replace('\n', ' '))
        except Exception as e:
            print("URL 2 error:", e)
            
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
