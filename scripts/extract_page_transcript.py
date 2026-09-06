import asyncio
from playwright.async_api import async_playwright
import os

sid = "dc0d74328429498183e342992fcb4aa7"

async def get_transcript():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        url = f"https://www.loom.com/share/{sid}"
        print(f"Loading {url}...")
        await page.goto(url)
        await asyncio.sleep(4)
        
        # Click on the Transcript tab
        t_tab = await page.query_selector("button:has-text('Transcript'), div:has-text('Transcript')[role='tab'], [role='tab']:has-text('Transcript')")
        if t_tab:
            print("Found Transcript tab! Clicking...")
            await t_tab.click()
            await asyncio.sleep(3)
            
            # Extract transcript text
            transcript = await page.evaluate('''() => {
                const els = document.querySelectorAll("[data-testid*='transcript'], [class*='transcript']");
                let text = '';
                for (let el of els) {
                    if (el.innerText && el.innerText.length > text.length) {
                        text = el.innerText;
                    }
                }
                return text || document.body.innerText;
            }''')
            print("=== Transcript Preview (first 1000 chars) ===")
            print(transcript[:1000])
            
            out_file = "/Users/carloferrara/Documents/antigravity/happy-fermi/data/bop_transcript.txt"
            with open(out_file, "w") as f:
                f.write(transcript)
            print(f"Saved {len(transcript)} chars to {out_file}")
            
        await page.close()

if __name__ == "__main__":
    asyncio.run(get_transcript())
