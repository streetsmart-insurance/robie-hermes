import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({"width": 1600, "height": 1000})
            await page.goto("https://natgenagency.com/MainMenu.aspx", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            
            # Find the Find Customer box on the left
            print("Finding customer input...")
            # The input right under 'Find Customer' / 'Last Name or Policy #'
            # In HTML, let's find inputs in the left container
            inputs = await page.locator("input[type='text']:visible").all()
            for i, inp in enumerate(inputs):
                id_val = await inp.get_attribute("id") or ""
                name_val = await inp.get_attribute("name") or ""
                print(f"Input {i}: id={id_val}, name={name_val}")
                
            # Fill first visible input with Smordoni
            await inputs[0].fill("Smordoni")
            # Find search button
            search_btns = await page.locator("input[type='submit']:visible, input[type='button']:visible, a:visible, button:visible").filter(has_text="Search").all()
            for j, btn in enumerate(search_btns):
                bid = await btn.get_attribute("id") or ""
                btxt = await btn.inner_text()
                print(f"Search btn {j}: id={bid}, text={btxt}")
                
            print("Clicking first search button...")
            await search_btns[0].click()
            await asyncio.sleep(6)
            
            print("After search URL:", page.url)
            await page.screenshot(path="/opt/renewal-automation-system/data/screenshots/natgen_smordoni_name_results.png")
            text = await page.evaluate("() => document.body.innerText")
            print("Page text snippet:\n", text[:600])
        finally:
            await page.close()

if __name__ == "__main__":
    asyncio.run(run())
