import asyncio
from playwright.async_api import async_playwright
import json
import os
import urllib.request
import time

VIDEOS_DIR = "/Users/carloferrara/Documents/antigravity/happy-fermi/data/videos"
os.makedirs(VIDEOS_DIR, exist_ok=True)

with open("/Users/carloferrara/Documents/antigravity/happy-fermi/data/loom_video_catalog.json") as f:
    catalog = json.load(f)

async def run_daemon():
    print("Starting background video and transcript polling daemon...")
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        for cycle in range(60): # Run up to 60 cycles (~30-40 mins)
            print(f"\n--- [Cycle {cycle+1}] Checking pending videos & transcripts at {time.strftime('%H:%M:%S')} ---")
            downloaded_count = 0
            
            for i, item in enumerate(catalog):
                lob = item.get("lob")
                sid = item.get("session_id")
                safe_name = lob.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("&", "and")
                out_file = os.path.join(VIDEOS_DIR, f"{safe_name}.mp4")
                transcript_file = os.path.join(VIDEOS_DIR, f"{safe_name}_transcript.txt")
                
                # Check if video already exists
                if os.path.exists(out_file) and os.path.getsize(out_file) > 1000000:
                    downloaded_count += 1
                    continue
                    
                # Poll transcoded URL
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
                    print(f"  ✓ {lob} is READY! Downloading...")
                    try:
                        urllib.request.urlretrieve(res["url"], out_file)
                        sz_mb = round(os.path.getsize(out_file)/(1024*1024), 2)
                        print(f"  ✓ Successfully downloaded {out_file} ({sz_mb} MB)!")
                        downloaded_count += 1
                    except Exception as ex:
                        print(f"  ✗ Download error for {lob}: {ex}")
                elif res.get("status") == 204:
                    # Still transcoding
                    pass
                else:
                    print(f"  ⏳ {lob}: status {res.get('status')}")
                    
            print(f"Cycle {cycle+1} completed: {downloaded_count}/{len(catalog)} videos downloaded.")
            if downloaded_count >= len(catalog):
                print("All videos downloaded successfully!")
                break
                
            await asyncio.sleep(30)
            
        await page.close()

if __name__ == "__main__":
    asyncio.run(run_daemon())
