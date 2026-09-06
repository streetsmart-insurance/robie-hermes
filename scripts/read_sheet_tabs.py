import asyncio
from playwright.async_api import async_playwright

async def inspect_tabs():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = b.contexts[0]
        page = await ctx.new_page()
        
        url = "https://docs.google.com/spreadsheets/d/1ZyX1rUzDmRLMLMGrRzS48BlwvUdfkBd060ekZqoKsvo/htmlview"
        await page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        
        links = await page.evaluate('''() => {
            const as = Array.from(document.querySelectorAll('a, li'));
            return as.map(a => ({ text: a.innerText.trim(), href: a.getAttribute('href') })).filter(x => x.text);
        }''')
        print("Tab links:", links[:10])
        
        # Click on 'Employees' or 'Street Smart Employees'
        for link in links:
            if link['text'] in ['Employees', 'Street Smart Employees']:
                print("Visiting:", link)
                if link['href']:
                    tab_url = "https://docs.google.com/spreadsheets/d/1ZyX1rUzDmRLMLMGrRzS48BlwvUdfkBd060ekZqoKsvo/htmlview" + link['href']
                    await page.goto(tab_url, wait_until="domcontentloaded")
                    await asyncio.sleep(2)
                    content = await page.inner_text("table")
                    print(f"Content of {link['text']}:")
                    for line in content.split("\n")[:30]:
                        if any(k in line.lower() for k in ['ashley', 'sandy', 'gabi', 'jake', 'email']):
                            print("  MATCH:", line)
        await page.close()

asyncio.run(inspect_tabs())
