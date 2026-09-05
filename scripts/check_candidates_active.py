import asyncio
from playwright.async_api import async_playwright

candidates = [
    ('150751441', 'Imperio Enterprises I LLC', 'EZXS3229906'),
    ('102351931', 'Advance Marble & Granite LLC', 'A422965'),
    ('157388228', 'Epoxy Concrete Coatings LLC', '3AA948407'),
    ('151382204', 'Ank Construction LLC', 'EZXS3220176'),
    ('99055770', 'Le Shawn Sneed', 'CUS062007927')
]

async def check():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = b.contexts[0]
        page = [pg for pg in ctx.pages if 'ezlynx.com' in pg.url][0]
        for app_id, name, target_pol in candidates:
            res = await page.evaluate(f'''async () => {{
                try {{
                    const r = await fetch('/PolicyAPI/v1/PolicyCard/GetPolicies?applicantId={app_id}');
                    const j = await r.json();
                    return (j.policyCards || []).map(p => ({{
                        num: p.policyNumber,
                        carrier: p.carrierName,
                        lob: p.lob,
                        statusId: p.policyStatusViewModelID,
                        cancelDate: p.cancellationDate,
                        expDate: p.expirationDate,
                        premium: p.premium
                    }}));
                }} catch(e) {{
                    return [{{'error': e.toString()}}];
                }}
            }}''')
            print(f'=== {name} ({app_id}) ===')
            for pol in res:
                is_active = (pol.get('statusId') == 1 and not pol.get('cancelDate'))
                status_str = 'ACTIVE' if is_active else 'CANCELLED/INACTIVE'
                num = pol.get('num') or ''
                match_str = ' <--- TARGET MATCH' if (target_pol and target_pol.lower() in num.lower()) else ''
                print(f"  [{status_str}] Pol: {num} | Carrier: {pol.get('carrier')} | LOB: {pol.get('lob')} | Exp: {pol.get('expDate')} | Prem: ${pol.get('premium')} | Cancel: {pol.get('cancelDate')}{match_str}")

asyncio.run(check())
