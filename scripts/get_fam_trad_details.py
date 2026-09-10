import asyncio
from playwright.async_api import async_playwright

async def download_doc():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(storage_state="/opt/renewal-automation-system/data/ezlynx_storage_state.json")
        page = await ctx.new_page()
        
        await page.goto("https://app.ezlynx.com/web/account/27904633/documents", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        
        # Click on the row or find the download link
        doc_info = await page.evaluate("""() => {
            const rows = Array.from(document.querySelectorAll('tr, .document-row, [class*="document-grid"] tr'));
            for (const r of rows) {
                if (r.innerText.includes('PolicyChangeRequest_Policy No CAPI082129')) {
                    const links = Array.from(r.querySelectorAll('a')).map(a => a.href);
                    return { text: r.innerText, links: links };
                }
            }
            return null;
        }""")
        print("Doc info:", doc_info)
        
        # Check task note details from Discussion 840757895
        await page.goto("https://app.ezlynx.com/web/account/27904633/notes", wait_until="domcontentloaded")
        await asyncio.sleep(2)
        note_text = await page.evaluate("""() => {
            const items = Array.from(document.querySelectorAll('.discussion-item, .note-item, [class*="timeline-item"], [class*="note"]'));
            for (const item of items) {
                if (item.innerText.includes('CAPI082129') || item.innerText.includes('CMcMahon')) {
                    return item.innerText;
                }
            }
            return null;
        }""")
        print("\nFull Discussion Note Text:\n", note_text)
        
        await b.close()

if __name__ == "__main__":
    asyncio.run(download_doc())
