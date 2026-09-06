import asyncio
from playwright.async_api import async_playwright
import json
import os
import urllib.request

VIDEOS_DIR = "/Users/carloferrara/Documents/antigravity/happy-fermi/data/videos"
os.makedirs(VIDEOS_DIR, exist_ok=True)

with open("/Users/carloferrara/Documents/antigravity/happy-fermi/data/loom_video_catalog.json") as f:
    catalog = json.load(f)

async def process_all():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        results = []
        for i, item in enumerate(catalog):
            lob = item.get("lob")
            sid = item.get("session_id")
            cat = item.get("category")
            safe_name = lob.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("&", "and")
            out_file = os.path.join(VIDEOS_DIR, f"{safe_name}.mp4")
            transcript_file = os.path.join(VIDEOS_DIR, f"{safe_name}_transcript.txt")
            
            print(f"\n--- [{i+1}/{len(catalog)}] Processing {lob} ({sid}) ---")
            
            if os.path.exists(out_file) and os.path.getsize(out_file) > 1000000:
                print(f"Already downloaded: {out_file} ({os.path.getsize(out_file)} bytes)")
                results.append({"lob": lob, "sid": sid, "status": "already_downloaded", "file": out_file})
                continue
                
            url = f"https://www.loom.com/share/{sid}"
            try:
                await page.goto(url)
                await asyncio.sleep(4)
                
                # Check Transcript Tab
                t_tab = await page.query_selector("[role='tab']:has-text('Transcript'), button:has-text('Transcript')")
                if t_tab:
                    await t_tab.click()
                    await asyncio.sleep(1.5)
                    gen_btn = await page.query_selector("button:has-text('Generate')")
                    if gen_btn:
                        print("  -> Found 'Generate' transcript button, clicking...")
                        await gen_btn.click()
                    else:
                        txt = await page.evaluate("() => document.querySelector(\"[role='tabpanel'], [class*='transcript']\")?.innerText")
                        if txt and len(txt) > 50:
                            with open(transcript_file, "w") as tf:
                                tf.write(txt)
                            print(f"  -> Extracted transcript: {len(txt)} chars")
                            
                # Check if transcoded-url is available directly
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
                        return {{ status: r.status, url: null }};
                    }} catch (e) {{
                        return {{ status: 500, error: e.toString() }};
                    }}
                }}""")
                
                if res.get("status") == 200 and res.get("url"):
                    dl_url = res["url"]
                    print(f"  -> Got direct MP4 URL! Downloading...")
                    urllib.request.urlretrieve(dl_url, out_file)
                    print(f"  -> Downloaded {os.path.getsize(out_file)} bytes to {out_file}!")
                    results.append({"lob": lob, "sid": sid, "status": "downloaded", "file": out_file})
                    continue
                    
                # If 204 or not ready, trigger via UI download modal
                print(f"  -> Transcode status {res.get('status')}. Triggering via UI download modal...")
                more_btn = await page.query_selector("[data-testid='toggleActions'], button:has-text('More actions')")
                if more_btn:
                    await more_btn.click()
                    await asyncio.sleep(1)
                    dl_item = await page.query_selector("button:has-text('Download video')")
                    if dl_item:
                        await dl_item.click()
                        await asyncio.sleep(1.5)
                        modal_dl = await page.query_selector("[role='dialog'] button:has-text('Download video'), div[class*='modal'] button:has-text('Download')")
                        if modal_dl:
                            await modal_dl.click()
                            print("  -> Clicked modal download button (triggered background transcoding)!")
                            results.append({"lob": lob, "sid": sid, "status": "transcode_triggered"})
                        else:
                            results.append({"lob": lob, "sid": sid, "status": "modal_dl_not_found"})
                    else:
                        results.append({"lob": lob, "sid": sid, "status": "dl_item_not_found"})
                else:
                    results.append({"lob": lob, "sid": sid, "status": "more_btn_not_found"})
                    
            except Exception as ex:
                print(f"  -> Error processing {lob}: {ex}")
                results.append({"lob": lob, "sid": sid, "status": "error", "error": str(ex)})
                
        await page.close()
        
        with open("/Users/carloferrara/Documents/antigravity/happy-fermi/data/batch_trigger_results.json", "w") as f:
            json.dump(results, f, indent=2)
        print("\nAll videos processed and triggered!")

if __name__ == "__main__":
    asyncio.run(process_all())
