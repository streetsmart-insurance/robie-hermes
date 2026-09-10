import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://files.merchantsgroup.com/agents-colleagues-vendors.asp", wait_until="networkidle")
        
        # Check function OpenPersonalLogin
        fn_code = await page.evaluate('''() => {
            return {
                OpenPersonalLogin: window.OpenPersonalLogin ? window.OpenPersonalLogin.toString() : 'not found',
                HandleEventRecovery: window.HandleEventRecovery ? window.HandleEventRecovery.toString() : 'not found',
                LogIn: window.LogIn ? window.LogIn.toString() : 'not found'
            };
        }''')
        print("Functions:", fn_code)
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
