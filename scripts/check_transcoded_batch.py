import asyncio
from playwright.async_api import async_playwright
import json
import os
import urllib.request

VIDEOS_DIR = "/Users/carloferrara/Documents/antigravity/happy-fermi/data/videos"

with open("/Users/carloferrara/Documents/antigravity/happy-fermi/data/loom_video_catalog.json") as f:
    catalog = json.load(f)

async def check_all():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        ready = []
        pending = []
        
        for i, item in enumerate(catalog):
            lob = item.get("lob")
            sid = item.get("session_id")
            safe_name = lob.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("&", "and")
            out_file = os.path.join(VIDEOS_DIR, f"{safe_name}.mp4")
            
            if os.path.exists(out_file) and os.path.getsize(out_file) > 1000000:
                ready.append({"lob": lob, "file": out_file, "size_mb": round(os.path.getsize(out_file)/(1024*1024), 1)})
                continue
                
            # Check transcoded-url
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
                print(f"Ready: {lob}! Downloading...")
                try:
                    urllib.request.urlretrieve(res["url"], out_file)
                    sz = os.path.getsize(out_file)
                    print(f"  -> Downloaded {sz} bytes ({round(sz/(1024*1024), 1)} MB)")
                    ready.append({"lob": lob, "file": out_file, "size_mb": round(sz/(1024*1024), 1)})
                except Exception as ex:
                    print(f"  -> Download failed: {ex}")
            else:
                pending.append({"lob": lob, "status": res.get("status")})
                
        await page.close()
        
        print("\n=== SUMMARY ===")
        print(f"Ready ({len(ready)}):")
        for r in ready:
            print(f"  ✓ {r['lob']}: {r.get('size_mb')} MB")
        print(f"Pending/Transcoding ({len(pending)}):")
        for p_item in pending:
            print(f"  ⏳ {p_item['lob']}: {p_item['status']}")

if __name__ == "__main__":
    asyncio.run(check_all())
