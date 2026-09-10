import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        ez_page = ctx.pages[0]
        
        url = 'https://app.ezlynx.com/web/account/21586424/policies'
        print(f'Navigating to {url}...')
        await ez_page.goto(url)
        await asyncio.sleep(5)
        
        # Check API policies
        api_policies = await ez_page.evaluate("""async () => {
            try {
                const res = await fetch('/PolicyAPI/v1/PolicyCard/GetPolicies?applicantId=21586424');
                return res.ok ? await res.json() : null;
            } catch (e) {
                return e.toString();
            }
        }""")
        print("API policies count:", len(api_policies) if isinstance(api_policies, list) else api_policies)
        if isinstance(api_policies, list):
            for p in api_policies:
                if '203193685900' in p.get('policyNumber', ''):
                    print("Matched policy:", {
                        'policyMasterID': p.get('policyMasterID'),
                        'policyNumber': p.get('policyNumber'),
                        'hasPendingChangeRequest': p.get('hasPendingChangeRequest'),
                        'status': p.get('status'),
                        'carrier': p.get('carrierName')
                    })
                    
        # Now let's see how change request is initiated in EZLynx
        # Look for buttons on the card
        card_buttons = await ez_page.evaluate("""() => {
            const cards = Array.from(document.querySelectorAll('.policy-card, .expansion-panel, mat-expansion-panel'));
            for (const c of cards) {
                if (c.innerText.includes('203193685900')) {
                    return Array.from(c.querySelectorAll('button, a'))
                        .map(b => ({
                            id: b.id,
                            tag: b.tagName,
                            text: b.innerText.trim(),
                            title: b.getAttribute('title'),
                            ariaLabel: b.getAttribute('aria-label')
                        }));
                }
            }
            return [];
        }""")
        print("Card buttons for 203193685900:")
        for b in card_buttons:
            print(" ", b)

asyncio.run(main())
