import asyncio
from playwright.async_api import async_playwright
import os
import urllib.request

VIDEOS_DIR = "/Users/carloferrara/Documents/antigravity/happy-fermi/data/videos"

items = [
    ("condominium", "d70cdb56e4b54d6fb8e957ccbc57c012"),
    ("homeowners", "f05774171d434032811b2e3eb9fd5734"),
    ("personal_umbrella", "0b0aca4db8f34f839872edc6949c25ba"),
    ("inland_marine_personal", "8e6e52245c744b3189b6e73ef0eb6cb3"),
]

async def dl():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        for name, sid in items:
            out_file = os.path.join(VIDEOS_DIR, f"{name}.mp4")
            if os.path.exists(out_file) and os.path.getsize(out_file) > 1000000:
                print(f"{name} already downloaded.")
                continue
                
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
                print(f"Downloading {name}...")
                urllib.request.urlretrieve(res["url"], out_file)
                print(f"  -> Downloaded {os.path.getsize(out_file)} bytes to {out_file}!")
            else:
                print(f"{name} status: {res.get('status')}")
                
        await page.close()

if __name__ == "__main__":
    asyncio.run(dl())
