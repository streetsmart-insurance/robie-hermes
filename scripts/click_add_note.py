import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        ez_page = ctx.pages[0]
        
        target_url = 'https://app.ezlynx.com/web/account/21586424/activity'
        print(f"Navigating to {target_url}...")
        await ez_page.goto(target_url)
        await asyncio.sleep(4)
        print("URL is now:", ez_page.url)
        
        # Click Add to Discussion
        res = await ez_page.evaluate("""() => {
            const h5 = Array.from(document.querySelectorAll('h5.discussion-title')).find(h => h.innerText.includes('CHANGE ME'));
            if (!h5) return 'h5 not found';
            const btn = h5.querySelector('button[title="Add to Discussion"]');
            if (btn) {
                btn.click();
                return 'clicked Add to Discussion';
            }
            return 'btn not found';
        }""")
        print("Click result:", res)
        await asyncio.sleep(2)
        
        await ez_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/ezlynx_add_note_opened.png')
        print("Saved ezlynx_add_note_opened.png")
        
        # Inspect composer inputs
        inputs = await ez_page.evaluate("""() => {
            return Array.from(document.querySelectorAll('textarea, [contenteditable="true"], input, button'))
                .filter(el => el.offsetParent !== null)
                .map(el => ({
                    tag: el.tagName,
                    id: el.id,
                    placeholder: el.placeholder || el.getAttribute('placeholder'),
                    text: el.innerText ? el.innerText.trim() : el.value,
                    className: el.className
                }));
        }""")
        print(f"Total visible inputs: {len(inputs)}")
        for inp in inputs:
            txt = (str(inp.get('placeholder')) + " " + str(inp.get('text')) + " " + str(inp.get('id')) + " " + str(inp.get('className'))).lower()
            if any(w in txt for w in ['note', 'reply', 'comment', 'editor', 'save', 'post', 'cancel', 'submit', 'type a note', 'add note']):
                print(" ", inp)

asyncio.run(main())
