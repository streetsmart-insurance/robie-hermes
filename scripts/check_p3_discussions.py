import asyncio
from playwright.async_api import async_playwright
import json

targets = [
    {"name": "SAPP Construction", "query": "SAPP Construction"},
    {"name": "John Guarini", "query": "Guarini"},
    {"name": "Ferrara Organization", "query": "Ferrara Organization"},
    {"name": "Empower Group", "query": "Empower Group"},
    {"name": "Seacrest Sales", "query": "Seacrest Sales"}
]

async def check_all():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = ctx.pages[0]
        
        results = {}
        for t in targets:
            print(f"\n--- Searching: {t['name']} ---")
            search_url = f"https://app.ezlynx.com/applicantportal/Search/Index?searchPhrase={t['query'].replace(' ', '+')}"
            await page.goto(search_url, wait_until="domcontentloaded")
            await asyncio.sleep(4)
            
            # Find applicant overview link
            app_id = await page.evaluate('''() => {
                const anchors = Array.from(document.querySelectorAll("a[href*='/web/applicant/'], a[href*='/web/account/']"));
                for (let a of anchors) {
                    const match = a.href.match(/\/(?:applicant|account)\/([0-9]+)/);
                    if (match) return match[1];
                }
                return null;
            }''')
            
            if not app_id:
                print(f"Could not find applicant ID for {t['name']}")
                continue
                
            print(f"Found Applicant ID: {app_id} for {t['name']}")
            
            # Query discussions via API
            disc_data = await page.evaluate(f'''async () => {{
                try {{
                    const r = await fetch('/EZLynxPortalAPI/Discussions/GetPagedDiscussions?pageNumber=1&pageSize=5&applicantId={app_id}&applicantContext=true');
                    if (!r.ok) return {{ error: r.status }};
                    return await r.json();
                }} catch(e) {{
                    return {{ error: e.message }};
                }}
            }}''')
            
            discussions = disc_data.get("discussions", [])
            print(f"Retrieved {len(discussions)} discussions:")
            clean_discs = []
            for d in discussions[:3]:
                title = d.get("title", "")
                created = d.get("created", "")
                note = d.get("discussionNote", {}).get("note", "")[:150]
                print(f"  * [{created[:10]}] {title} -> {note.replace(chr(10), ' ')}")
                clean_discs.append({"title": title, "created": created, "note": note})
            results[t['name']] = {"app_id": app_id, "discussions": clean_discs}
            
        return results

if __name__ == "__main__":
    res = asyncio.run(check_all())
    with open("/Users/carloferrara/Documents/antigravity/happy-fermi/data/p3_check_results.json", "w") as f:
        json.dump(res, f, indent=2)
    print("\nSaved results to data/p3_check_results.json")
