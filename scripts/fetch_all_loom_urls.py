import asyncio
from playwright.async_api import async_playwright
import json
import os
import re

loom_videos = [
    {"lob": "Commercial Auto Liability", "category": "Commercial", "url": "https://www.loom.com/share/0f1a1709f5264e15939b0c048cea6028"},
    {"lob": "Business Owner Policy (BOP)", "category": "Commercial", "url": "https://www.loom.com/share/dc0d74328429498183e342992fcb4aa7"},
    {"lob": "Commercial Package", "category": "Commercial", "url": "https://www.loom.com/share/50e461126ea5412fa87289ab06cd3469"},
    {"lob": "Commercial Property", "category": "Commercial", "url": "https://www.loom.com/share/48d74e22a77a481d8268fe30c6947376"},
    {"lob": "Commercial Umbrella", "category": "Commercial", "url": "https://www.loom.com/share/a491c3ab97dd4f33b3a3aa8e077f230c"},
    {"lob": "Crime", "category": "Commercial", "url": "https://www.loom.com/share/69a0c734285c466da1c11f9fcf889870"},
    {"lob": "Errors and Omissions (E&O)", "category": "Commercial", "url": "https://www.loom.com/share/5bc7644048f34c02a74ef8d4812c62bc"},
    {"lob": "Garage and Dealers Policy", "category": "Commercial", "url": "https://www.loom.com/share/2e53898e240a4036a0c8c63c2b796a9e"},
    {"lob": "General Liability", "category": "Commercial", "url": "https://www.loom.com/share/b507be54b3df45e3afcb95f37fa39015"},
    {"lob": "Inland Marine (Commercial)", "category": "Commercial", "url": "https://www.loom.com/share/421ff2e1099b4073b346d8a8e8fde402"},
    {"lob": "Workers Compensation", "category": "Commercial", "url": "https://www.loom.com/share/0b7a9f616b17456a84a4e4db2153d289"},
    {"lob": "Bond", "category": "Specialty", "url": "https://www.loom.com/share/c3f9239853414b9abeed93f432f3edfb"},
    {"lob": "Pollution Liability", "category": "Specialty", "url": "https://www.loom.com/share/72d720c220d9417e9adc552fe9423bcc"},
    {"lob": "Personal Auto", "category": "Personal", "url": "https://www.loom.com/share/efbd57168ba240a39f8823c614e6da1b"},
    {"lob": "Condominium", "category": "Personal", "url": "https://www.loom.com/share/d70cdb56e4b54d6fb8e957ccbc57c012"},
    {"lob": "Homeowners", "category": "Personal", "url": "https://www.loom.com/share/f05774171d434032811b2e3eb9fd5734"},
    {"lob": "Personal Umbrella", "category": "Personal", "url": "https://www.loom.com/share/0b0aca4db8f34f839872edc6949c25ba"},
    {"lob": "Inland Marine (Personal)", "category": "Personal", "url": "https://www.loom.com/share/8e6e52245c744b3189b6e73ef0eb6cb3"}
]

async def fetch_urls():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        # Navigate once to establish loom session
        await page.goto("https://www.loom.com/share/0f1a1709f5264e15939b0c048cea6028")
        await asyncio.sleep(2)
        
        catalog = []
        for item in loom_videos:
            session_id = item["url"].split("/")[-1]
            print(f"Fetching direct MP4 URL for {item['lob']} ({session_id})...")
            
            res = await page.evaluate(f"""async () => {{
                try {{
                    const r = await fetch("https://www.loom.com/api/campaigns/sessions/{session_id}/transcoded-url", {{
                        method: "POST",
                        headers: {{ "Content-Type": "application/json" }}
                    }});
                    if (!r.ok) return {{ error: r.status }};
                    return await r.json();
                }} catch(e) {{
                    return {{ error: e.message }};
                }}
            }}""")
            
            mp4_url = res.get("url") if isinstance(res, dict) else None
            catalog.append({
                "lob": item["lob"],
                "category": item["category"],
                "share_url": item["url"],
                "session_id": session_id,
                "mp4_url": mp4_url,
                "error": res.get("error") if isinstance(res, dict) and not mp4_url else None
            })
            print(f"  -> {'SUCCESS' if mp4_url else 'FAILED: ' + str(res)}")
            
        await page.close()
        
        os.makedirs("/Users/carloferrara/Documents/antigravity/happy-fermi/data", exist_ok=True)
        with open("/Users/carloferrara/Documents/antigravity/happy-fermi/data/loom_video_catalog.json", "w") as f:
            json.dump(catalog, f, indent=2)
        print("Saved full catalog to data/loom_video_catalog.json")

if __name__ == "__main__":
    asyncio.run(fetch_urls())
