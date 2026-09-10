import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://files.merchantsgroup.com/agents-colleagues-vendors.asp", wait_until="networkidle")
        print("URL:", page.url)
        print("Title:", await page.title())
        await page.screenshot(path="/Users/carloferrara/Documents/antigravity/happy-fermi/data/screenshots/merchants_agent_login.png")
        
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
        
        form_action = await page.evaluate('''() => {
            const form = document.querySelector('form');
            return form ? form.action : 'no form';
        }''')
        print("Form action:", form_action)
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
