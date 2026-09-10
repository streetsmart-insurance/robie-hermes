import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        tab = ctx.pages[0]
        url = 'https://app.ezlynx.com/web/account/21586424/activity'
        print(f"Navigating to {url}...")
        await tab.goto(url)
        await tab.wait_for_selector('text=Personal Auto Policy Change Request', timeout=15000)
        print("Found Personal Auto Policy Change Request!")
        
        # Locate the card
        # Find element with text 'Personal Auto Policy Change Request - CHANGE ME'
        card_el = tab.locator('text=Personal Auto Policy Change Request - CHANGE ME').first
        await card_el.scroll_into_view_if_needed()
        await asyncio.sleep(1)
        
        # Get its parent container
        parent_info = await tab.evaluate('''() => {
            const titleEl = Array.from(document.querySelectorAll('*')).find(e => e.innerText && e.innerText.trim() === 'Personal Auto Policy Change Request - CHANGE ME');
            if (!titleEl) return null;
            // Climb up to the card container
            let container = titleEl;
            while (container && !container.classList.contains('mat-expansion-panel') && !container.classList.contains('activity-item') && !container.classList.contains('timeline-entry') && container.tagName !== 'MAT-CARD' && container.parentElement && !container.parentElement.classList.contains('activity-stream')) {
                container = container.parentElement;
                if (container && container.classList.contains('mat-card')) break;
                if (container && container.classList.contains('mat-expansion-panel')) break;
            }
            if (!container) container = titleEl.parentElement;
            
            return {
                containerTag: container.tagName,
                containerClass: container.className,
                html: container.outerHTML
            };
        }''')
        
        if parent_info:
            print("Container tag:", parent_info['containerTag'])
            print("Container class:", parent_info['containerClass'])
            # Save html snippet
            with open('/opt/renewal-automation-system/data/card_snippet.html', 'w') as f:
                f.write(parent_info['html'])
            print("Saved card_snippet.html")
            
        await tab.screenshot(path='/opt/renewal-automation-system/data/screenshots/activity_card_located.png')

asyncio.run(main())
