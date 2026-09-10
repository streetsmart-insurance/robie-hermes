import asyncio
from playwright.async_api import async_playwright
import os

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        
        pdf_url = 'https://app.maple-tech.com/hmf/api/v1/docs/4430384/POLICY%20DEC%20PAGES,%20HONJ2025100027-26,%20Streetsmart%20Risk%20Managers%20Inc.,%20Streetsmart%20Risk%20Managers%20Inc..pdf'
        print('Fetching PDF via browser context API request...')
        response = await ctx.request.get(pdf_url)
        print('Status:', response.status)
        if response.status == 200:
            data = await response.body()
            out_path = '/opt/renewal-automation-system/data/carrier_downloads/HONJ2025100027_26_Renewal_Dec_Pages.pdf'
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, 'wb') as f:
                f.write(data)
            print(f'Successfully saved PDF to {out_path} ({len(data)} bytes)!')
        else:
            print('Failed with status:', response.status)

asyncio.run(run())
