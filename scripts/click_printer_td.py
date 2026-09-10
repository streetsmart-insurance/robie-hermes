import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        aspire_page = [p for p in ctx.pages if 'maple-tech.com/hmf/directory' in p.url][0]
        main_frame = [f for f in aspire_page.frames if f.name == 'fraTocMain'][0]
        
        # Click printer icon via JS dispatch or coordinate click
        result = await main_frame.evaluate('''() => {
            const tds = Array.from(document.getElementsByTagName('td'));
            const ptd = tds.find(t => t.innerHTML.indexOf('printer.gif') !== -1);
            if (!ptd) return 'not found';
            // Click the element inside ptd
            const div = ptd.querySelector('div div') || ptd;
            div.click();
            return {
                id: ptd.id,
                colidx: ptd.getAttribute('colidx'),
                html: ptd.innerHTML
            };
        }''')
        print('JS Click Result:', result)
        await asyncio.sleep(5)
        
        await aspire_page.screenshot(path='/opt/renewal-automation-system/data/screenshots/hmf_after_js_click.png')
        
        # Check if docarea became visible or populated
        doc_html = await main_frame.evaluate('''() => {
            const da = document.getElementById('docarea');
            return da ? { style: da.getAttribute('style'), html: da.innerHTML } : null;
        }''')
        print('DocArea:', doc_html)

asyncio.run(run())
