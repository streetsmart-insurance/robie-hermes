import asyncio
from playwright.async_api import async_playwright
import json

sid = "dc0d74328429498183e342992fcb4aa7"

async def inspect():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        gql_responses = []
        async def on_resp(resp):
            if "graphql" in resp.url and resp.request.method == "POST":
                try:
                    data = await resp.json()
                    gql_responses.append({"url": resp.url, "data": data})
                except Exception:
                    pass
        page.on("response", on_resp)
        
        print("Navigating to BOP video...")
        await page.goto(f"https://www.loom.com/share/{sid}")
        await asyncio.sleep(6)
        
        print(f"Captured {len(gql_responses)} GraphQL responses.")
        for i, g in enumerate(gql_responses):
            d_str = json.dumps(g["data"])
            print(f"GQL {i}: {d_str[:200]}")
            if any(k in d_str for k in ["mp4", "download", "url", "video", "transcript", "summary"]):
                # Save interesting responses
                with open(f"/Users/carloferrara/Documents/antigravity/happy-fermi/data/gql_{i}.json", "w") as f:
                    json.dump(g["data"], f, indent=2)
                print(f"  -> Saved gql_{i}.json")
                
        await page.close()

asyncio.run(inspect())
