import asyncio
from playwright.async_api import async_playwright
import json
import os
import urllib.request

VIDEOS_DIR = "/Users/carloferrara/Documents/antigravity/happy-fermi/data/videos"

with open("/Users/carloferrara/Documents/antigravity/happy-fermi/data/loom_video_catalog.json") as f:
    catalog = json.load(f)

async def check():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        for i, item in enumerate(catalog):
            lob = item.get("lob")
            sid = item.get("session_id")
            safe_name = lob.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("&", "and")
            out_file = os.path.join(VIDEOS_DIR, f"{safe_name}.mp4")
            transcript_file = os.path.join(VIDEOS_DIR, f"{safe_name}_transcript.txt")
            
            print(f"\n[{i+1}/{len(catalog)}] {lob} ({sid}):")
            
            # 1. Check Transcoded URL
            try:
                res = await page.evaluate(f"""async () => {{
                    try {{
                        const r = await fetch("https://www.loom.com/api/campaigns/sessions/{sid}/transcoded-url", {{
                            method: "POST",
                            headers: {{ "Content-Type": "application/json" }}
                        }});
                        if (r.status === 200) {{
                            const j = await r.json();
                            return {{ status: 200, url: j.url }};
                        }}
                        return {{ status: r.status }};
                    }} catch (e) {{
                        return {{ status: 500, error: e.toString() }};
                    }}
                }}""")
                
                if res.get("status") == 200 and res.get("url"):
                    print(f"  -> MP4 READY! Downloading...")
                    urllib.request.urlretrieve(res["url"], out_file)
                    print(f"  -> Downloaded {os.path.getsize(out_file)} bytes ({round(os.path.getsize(out_file)/(1024*1024), 1)} MB)")
                else:
                    print(f"  -> MP4 Status: {res.get('status')}")
            except Exception as e:
                print(f"  -> Error checking MP4: {e}")
                
            # 2. Check Transcript on page
            try:
                await page.goto(f"https://www.loom.com/share/{sid}")
                await asyncio.sleep(2.5)
                
                t_tab = await page.query_selector("[role='tab']:has-text('Transcript'), button:has-text('Transcript')")
                if t_tab:
                    await t_tab.click()
                    await asyncio.sleep(1.5)
                    txt = await page.evaluate("() => document.querySelector(\"[role='tabpanel'], [class*='transcript']\")?.innerText")
                    if txt and len(txt) > 200 and "Processing transcript" not in txt and "No transcript yet" not in txt:
                        with open(transcript_file, "w") as tf:
                            tf.write(txt)
                        print(f"  -> TRANSCRIPT EXTRACTED: {len(txt)} chars -> saved to {transcript_file}")
                    else:
                        print(f"  -> Transcript text snippet: {repr(txt[:60]) if txt else 'None'}")
            except Exception as e:
                print(f"  -> Error checking transcript: {e}")
                
        await page.close()

if __name__ == "__main__":
    asyncio.run(check())
