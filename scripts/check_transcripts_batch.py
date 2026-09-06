import asyncio
from playwright.async_api import async_playwright
import json
import os

VIDEOS_DIR = "/Users/carloferrara/Documents/antigravity/happy-fermi/data/videos"

with open("/Users/carloferrara/Documents/antigravity/happy-fermi/data/loom_video_catalog.json") as f:
    catalog = json.load(f)

async def check_transcripts():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        extracted = []
        for i, item in enumerate(catalog):
            lob = item.get("lob")
            sid = item.get("session_id")
            safe_name = lob.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("&", "and")
            transcript_file = os.path.join(VIDEOS_DIR, f"{safe_name}_transcript.txt")
            
            if os.path.exists(transcript_file) and os.path.getsize(transcript_file) > 500:
                extracted.append(f"✓ {lob} (cached: {os.path.getsize(transcript_file)} bytes)")
                continue
                
            await page.goto(f"https://www.loom.com/share/{sid}")
            await asyncio.sleep(3)
            
            # Click transcript tab
            t_tab = await page.query_selector("[role='tab']:has-text('Transcript'), button:has-text('Transcript')")
            if t_tab:
                await t_tab.click()
                await asyncio.sleep(1.5)
                txt = await page.evaluate("() => document.querySelector(\"[role='tabpanel'], [class*='transcript']\")?.innerText")
                if txt and len(txt) > 200 and "Processing transcript" not in txt and "No transcript yet" not in txt:
                    with open(transcript_file, "w") as tf:
                        tf.write(txt)
                    extracted.append(f"✓ {lob} (extracted: {len(txt)} chars)")
                    print(f"Extracted transcript for {lob}: {len(txt)} chars")
                else:
                    status = "processing" if "Processing" in (txt or "") else "not_ready"
                    print(f"Transcript for {lob}: {status}")
                    
        await page.close()
        print("\nTranscripts summary:", extracted)

if __name__ == "__main__":
    asyncio.run(check_transcripts())
