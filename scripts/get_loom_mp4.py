import asyncio
from playwright.async_api import async_playwright
import re

url = "https://www.loom.com/share/0f1a1709f5264e15939b0c048cea6028"

async def capture_video_urls():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        media_urls = []
        transcript_urls = []
        
        def handle_response(response):
            r_url = response.url
            if any(ext in r_url for ext in [".mp4", ".m3u8", "transcoded", "videodelivery.net", "loom-cdn"]):
                print("MEDIA URL DETECTED:", r_url)
                media_urls.append(r_url)
            if "transcript" in r_url.lower():
                print("TRANSCRIPT URL DETECTED:", r_url)
                transcript_urls.append(r_url)
                
        page.on("response", handle_response)
        
        print(f"Navigating to {url}...")
        await page.goto(url)
        await asyncio.sleep(6)
        
        # Check if video tag has src
        video_src = await page.evaluate('''() => {
            const v = document.querySelector("video");
            return v ? v.src : null;
        }''')
        print("Video src in DOM:", video_src)
        
        # Look for download button
        download_btn = await page.query_selector("[aria-label*='Download'], button:has-text('Download')")
        if download_btn:
            print("Found download button!")
            
        print("Media URLs captured:", len(media_urls))
        for u in media_urls[:10]:
            print("  -", u)
            
        print("Transcript URLs captured:", len(transcript_urls))
        for u in transcript_urls[:10]:
            print("  -", u)
            
        await page.close()

if __name__ == "__main__":
    asyncio.run(capture_video_urls())
