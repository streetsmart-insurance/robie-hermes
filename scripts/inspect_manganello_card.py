import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        try:
            await page.set_viewport_size({'width': 1600, 'height': 1000})
            await page.goto('https://app.ezlynx.com/web/account/143979332/policies', wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(4)
            
            # Find the card containing TNF4012536
            cards = await page.locator("div, mat-card").filter(has_text="TNF4012536").all()
            print(f"Matched cards/elements: {len(cards)}")
            
            # Find all buttons/menus within the card
            # In the screenshot, there is a menu button right next to the Forms clipboard icon
            menu_btns = await page.locator("button[aria-label*='action' i], button[id*='action' i], mat-icon:has-text('more_vert')").all()
            print(f"Action buttons: {len(menu_btns)}")
            for idx, mb in enumerate(menu_btns):
                vis = await mb.is_visible()
                print(f"  Button {idx}: visible={vis}")
                if vis:
                    await mb.click()
                    await asyncio.sleep(1)
                    menu_items = await page.locator("[role='menuitem'], .mat-mdc-menu-item").all_inner_texts()
                    print(f"  Menu items for button {idx}:", menu_items)
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(1)
                    
            # Let's inspect the chevron dropdown
            chevrons = await page.locator("mat-icon:has-text('keyboard_arrow_down'), mat-icon:has-text('expand_more')").all()
            print(f"Chevrons: {len(chevrons)}")
            for idx, ch in enumerate(chevrons):
                if await ch.is_visible():
                    print(f"Clicking chevron {idx}...")
                    await ch.click()
                    await asyncio.sleep(2)
                    
            await page.screenshot(path='/opt/renewal-automation-system/data/screenshots/manganello_card_opened.png')
            print("Saved manganello_card_opened.png")

        finally:
            await page.close()

if __name__ == '__main__':
    asyncio.run(run())
